# Gap Fix Review: Gas Anomaly Detector + Comprehensive Pipeline EWMA/POT Changes

Tech Lead review of recent gap-fix modifications to `src/models/gas_anomaly_detector.py` and `src/api/services/comprehensive_pipeline.py`.

Date: 2026-06-20

---

## 1. Correctness Concerns

### 1a. EWMA warm-up strategy in `GasAnomalyDetector.fit()`

The warm-up feeds `abs(v - baseline_mean) / baseline_std` (absolute z-scores of historical values) into the EWMA tracker. This is sound in principle -- it initializes the tracker's running mean and variance on the same distribution it will see at detection time.

**Subtle bias**: the warm-up uses the *same* data that was used to compute `baseline_mean` and `baseline_std`. This means the z-scores are slightly biased toward lower values compared to truly out-of-sample data (in-sample standard deviations are smaller than out-of-sample). For large `n` (>30) this bias is negligible, but for small sample sizes near `_MIN_BASELINE_SAMPLES=5`, the EWMA's mean will be biased low, making the dynamic threshold slightly too tight (more false positives). Not a critical issue given the minimum 10-observation warm-up in `EWMATracker._MIN_OBS`, but worth noting.

**Recommendation (P3, non-blocking)**: Consider leave-one-out z-scores for warm-up when `n < 30`, or simply document the known bias.

### 1b. `is_anomaly(z_score)` in `detect()` -- correct input?

The EWMA tracker in `detect()` receives the z-score as input (`ewma_tracker.is_anomaly(z_score)`), while in `comprehensive_pipeline.py` the separate EWMA tracker receives the reconstruction error (`_ewma_tracker.update(anomaly_score)`). These are two different EWMA trackers serving different purposes:

- **Gas detector**: EWMA on z-scores of gas sensor columns (per-column adaptive threshold)
- **Pipeline**: EWMA on LSTM-VAE reconstruction errors (per-tenant adaptive threshold)

The gas detector's usage is internally consistent -- it warms up on z-scores and detects on z-scores. However, `is_anomaly(z_score)` does NOT call `update()`, so the gas detector's EWMA tracker never adapts after `fit()`. Each call to `detect()` checks against a static threshold frozen at fit-time. This is inconsistent with the EWMA's purpose (adaptive thresholding).

**Recommendation (P2)**: Call `ewma_tracker.update(z_score)` in `detect()` after checking `is_anomaly()`, so the tracker continues to adapt to the data stream. Without this, the EWMA in the gas detector is functionally a static threshold computed from training data, which is just a more expensive version of the existing z-score threshold.

---

## 2. Statefulness Risk

`GasAnomalyDetector` is now stateful via `_ewma_trackers`. In `gas_anomalies.py`, a **new** `GasAnomalyDetector()` is instantiated per request (line 76: `detector = GasAnomalyDetector()`). This means:

- No statefulness risk across requests -- each request gets a fresh instance
- The EWMA trackers are populated during `fit()` and used during `detect()` within the same request lifecycle
- On the next request, trackers are rebuilt from scratch

This is safe but wasteful. The EWMA's value comes from accumulating observations over time, but here it is rebuilt from the same historical window each time. Given the current per-request instantiation pattern, the EWMA adds detection capability during a single request's lifetime (the warm-up from `fit()` calibrates it), but it cannot adapt across requests.

**Recommendation (P3, non-blocking)**: This is acceptable for now. If cross-request adaptation is desired, the detector should be cached per-tenant (similar to how `ComprehensiveInferencePipeline` caches its EWMA/POT trackers).

---

## 3. DB Persistence Pattern (Race Condition Analysis)

### 3a. `_load_trackers` / `_save_trackers` race condition

`_save_trackers` performs:
1. `SELECT LearningState WHERE customer_id = ?` (read)
2. `INSERT` or `UPDATE` (write)
3. `await db.commit()`

Two concurrent pipeline runs for the same `customer_id` could both read the same state at step 1, then both write at step 2, with the last commit winning (lost update). The SQLite backend serializes writes so the worst case is a stale-read overwrite. On PostgreSQL, both would succeed but one tracker update would be lost.

**Severity**: Low. EWMA/POT trackers are probabilistic and self-correcting -- a single lost update shifts the running average by a negligible amount. However, the pattern is fragile.

**Recommendation (P3)**: For correctness under concurrency, use `SELECT ... FOR UPDATE` on PostgreSQL (not available on SQLite). Alternatively, use a single SQL `INSERT ... ON CONFLICT UPDATE` (upsert) to collapse the read+write into one atomic operation. The SQLAlchemy `merge()` method would also work.

### 3b. `await db.rollback()` scope

The `except` block in `_save_trackers` calls `await db.rollback()`. The `db: AsyncSession` is the same session used by the entire pipeline run (passed from `_run_with_components`). If `_save_trackers` fails after the pipeline has already committed other work (e.g., the incident via `IncidentMemory.store_incident()` at line 379), the rollback could undo the incident commit.

**Critical assessment**: Looking at the call order in `_run_with_components`:
1. Line 379: `incident_id = await memory.store_incident(...)` -- this calls `await self._session.commit()` internally (incident_memory.py line 103)
2. Line 339: `await self._save_trackers(...)` -- runs AFTER the incident store

Since `store_incident` has already committed, the subsequent `rollback()` in `_save_trackers` only rolls back the tracker save attempt, not the incident. This is safe **only because** `store_incident` commits eagerly. If that commit order ever changes, this becomes a data-loss bug.

**Recommendation (P2)**: Use a nested transaction (savepoint) for the tracker save: `async with db.begin_nested():` around the tracker upsert. This scopes the rollback to just the tracker operation regardless of surrounding transaction state.

---

## 4. Error Handling

### 4a. `logger.debug` for tracker load/save failures

Both `_load_trackers` (line 518) and `_save_trackers` (line 554) catch all exceptions with `logger.debug`. The `_run_with_components` caller also catches tracker errors at lines 306 and 341 with `logger.debug`.

`logger.debug` is the wrong level for these. Debug messages are typically filtered out in production logging configs. If the database is misconfigured, the trackers will silently fall back to fresh state on every request, and the adaptive thresholding will never accumulate -- the system degrades to a static threshold with no operational visibility.

**Recommendation (P1)**: Change `logger.debug` to `logger.warning` for:
- `_load_trackers` DB failure (line 518) -- operator should know persistence is broken
- `_save_trackers` DB failure (line 554) -- operator should know state is being lost

Keep `logger.debug` for the outer catch in `_run_with_components` (lines 306, 341) since those log a generic "skipped" message and the inner methods already log details.

### 4b. Silent fallback to fresh trackers

When `_load_trackers` fails to read from DB, it creates fresh `EWMATracker` and `POTThresholdFitter` with default config. Fresh trackers return `float("inf")` from `get_threshold()` until `_MIN_OBS` (10) or `_MIN_BUFFER_FILL` (20) observations accumulate. Since `_effective_threshold` is `max(ewma_threshold, pot_threshold)`, a fresh tracker means the dynamic threshold is `inf`, so `is_lstm_anomaly` is always `False` (no score exceeds infinity). Detection falls back entirely to the ensemble detector.

This is safe -- it fails open (under-detection, not over-detection). However, if the DB is persistently unavailable, the system never builds adaptive thresholds, which defeats the purpose of the EWMA/POT integration.

**Recommendation (P2)**: After N consecutive load failures (configurable), log at `WARNING` level with a message like "Adaptive thresholding disabled for tenant {customer_id} due to persistent DB failures". A simple counter on the pipeline instance would suffice.

---

## 5. Function Complexity / Line Count

### `_run_with_components` (lines 234-455): 221 lines

This method is **far** over the 50-line limit from the coding rules. It is the single largest function in the codebase. While it is essentially a sequential pipeline (each step feeds the next), the length makes it difficult to review and test individual stages.

**Recommendation (P2)**: Extract into named sub-methods:
- `_detect_anomaly(seq_array, baseline)` -- LSTM-VAE forward pass + physics validation
- `_classify_fault(ensemble_result, pattern_result, physics_result)` -- fault classification logic
- `_load_and_apply_dynamic_threshold(customer_id, db)` -- EWMA/POT load + threshold computation
- `_build_result_dict(...)` -- the final dict assembly (lines 414-455)

Each would be under 50 lines and independently testable.

### `_suppressed_result` (lines 457-492): 35 lines

Within limit, but the 23-key dict literal is fragile -- any new key added to the normal result dict must also be added here or consumers will break on missing keys. Consider a shared `_empty_result()` factory.

### `fit()` in `gas_anomaly_detector.py` (lines 69-122): 53 lines

Slightly over the 50-line limit. The EWMA warm-up block (lines 116-120) could be extracted into a private `_warm_up_ewma(col, vals, baseline_mean, baseline_std)` method.

### `detect()` in `gas_anomaly_detector.py` (lines 124-196): 72 lines

Over the 50-line limit. The per-column scoring body (lines 153-195) could be extracted into `_score_column(value, baseline, ewma_tracker) -> GasAnomaly`.

---

## 6. Import Hygiene

### `gas_anomaly_detector.py`

- `from src.config import EWMA_ALPHA, EWMA_K` -- both used in `fit()` line 117. Clean.
- `from src.models.ewma_tracker import EWMATracker` -- used in `__init__` and `fit()`. Clean.
- **Circular import risk**: `gas_anomaly_detector -> ewma_tracker -> (none)` and `gas_anomaly_detector -> config -> (none)`. No circular dependency. Safe.

### `comprehensive_pipeline.py`

- `from sqlalchemy import select` -- used in `_load_trackers` line 506 and `_save_trackers` line 538. Clean.
- `from src.db.models import LearningState` -- used in both tracker methods. Clean.
- **Circular import risk**: `comprehensive_pipeline -> db.models -> config -> (none)`. No cycle. Safe.
- **Unused import check**: All imports at the top of the file are used. The `Dict`, `List`, `Optional` from `typing` could be replaced with modern `dict`, `list`, `Optional` syntax (Python 3.10+), but this is a pre-existing style inconsistency, not introduced by these changes.

---

## 7. Other Debt

### 7a. `setattr`/`getattr` for dynamic caching (`_load_trackers`)

Lines 501-502 and 515 use `setattr(self, cache_key, ...)` / `getattr(self, cache_key, None)` with dynamic attribute names like `_trackers_{customer_id}`. This pattern:
- Bypasses type checking -- the attribute is invisible to type checkers and IDE tooling
- Pollutes the instance namespace with an unbounded number of attributes (one per tenant)
- Makes the caching behavior non-obvious to readers

**Recommendation (P2)**: Replace with an explicit `dict[str, tuple[EWMATracker, POTThresholdFitter]]` attribute on `__init__`, e.g. `self._tracker_cache: dict[str, tuple[EWMATracker, POTThresholdFitter]] = {}`. This is type-safe, bounded (can add LRU eviction), and self-documenting.

### 7b. Duplicate `GAS_COLUMNS` definition

`GAS_COLUMNS` is defined both in `src/config.py` (as `GAS_SENSOR_COLUMNS`, line 564) and in `src/models/gas_anomaly_detector.py` (as `GAS_COLUMNS`, line 19). The values are identical. This violates the "config values in one place" rule.

**Recommendation (P1)**: Remove `GAS_COLUMNS` from `gas_anomaly_detector.py` and import `GAS_SENSOR_COLUMNS as GAS_COLUMNS` from config (or just use `GAS_SENSOR_COLUMNS` directly). The route file `gas_anomalies.py` imports `GAS_COLUMNS` from the detector, so it would need updating too.

### 7c. `_run_with_components` return dict has 28 keys

The return dict (lines 414-455) has 28 keys, making it a de facto data class without type safety. Any consumer must know the exact key names, and there is no IDE autocomplete or type checking.

**Recommendation (P3)**: Define a `PipelineResult` dataclass or TypedDict to replace the raw dict. This is a larger refactor that should be coordinated with the API response schemas.

### 7d. `store_incident()` now receives `fault_type` -- good

The `fault_type=fault_type` kwarg addition to `store_incident()` (line 389) correctly wires the fault taxonomy into incident storage. This was a missing piece that is now properly connected.

### 7e. `Optional` from `typing` vs `X | None` syntax

Both files use `from typing import Optional` and `Optional[X]` annotations. The codebase is Python 3.11+ (per venv notes). Modern syntax `X | None` is preferred but this is a pre-existing style choice, not introduced by these changes.

---

## Summary Table

| ID | Finding | Severity | File | Recommendation |
|----|---------|----------|------|----------------|
| F1 | EWMA in gas detector never updates after `fit()` -- functionally a static threshold | P2 | gas_anomaly_detector.py | Call `tracker.update(z_score)` in `detect()` |
| F2 | `logger.debug` hides DB persistence failures in production | P1 | comprehensive_pipeline.py | Change to `logger.warning` |
| F3 | Duplicate `GAS_COLUMNS` / `GAS_SENSOR_COLUMNS` definition | P1 | gas_anomaly_detector.py + config.py | Single source of truth in config.py |
| F4 | `_run_with_components` is 221 lines (limit: 50) | P2 | comprehensive_pipeline.py | Extract sub-methods |
| F5 | `setattr`/`getattr` dynamic caching bypasses type system | P2 | comprehensive_pipeline.py | Replace with typed dict attribute |
| F6 | `db.rollback()` scope could affect other work if commit order changes | P2 | comprehensive_pipeline.py | Use `db.begin_nested()` savepoint |
| F7 | `fit()` is 53 lines, `detect()` is 72 lines (limit: 50) | P2 | gas_anomaly_detector.py | Extract helpers |
| F8 | EWMA warm-up uses in-sample z-scores (biased for small n) | P3 | gas_anomaly_detector.py | Document known bias |
| F9 | 28-key raw dict return from pipeline | P3 | comprehensive_pipeline.py | Future: TypedDict or dataclass |
| F10 | Silent fallback to fresh trackers with no escalation | P2 | comprehensive_pipeline.py | Log warning after N consecutive failures |
| F11 | SELECT then UPDATE without transaction lock (lost-update risk) | P3 | comprehensive_pipeline.py | Use upsert or SELECT FOR UPDATE |
