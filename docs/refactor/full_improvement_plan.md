# Full Improvement Plan — SCADA Anomaly Detection

**Date:** 2026-06-21
**Reviewer:** Tech Lead (Claude Opus)
**Scope:** 5 phases + 1 hotfix of SCADA anomaly detection implementation
**Tests baseline:** 43/43 passing (`tests/unit/test_scada_detectors.py`)

---

## Finding Status — Prior Review (F1–F10)

| ID | Description | Status |
|----|-------------|--------|
| F1 | EWMA never updated in `detect()` | **STILL PRESENT** |
| F2 | `logger.debug` for DB persistence failures | **STILL PRESENT** |
| F3 | Duplicate `GAS_COLUMNS` vs `GAS_SENSOR_COLUMNS` | **STILL PRESENT** |
| F4 | `_run_with_components` oversized (now 338 lines) | **STILL PRESENT — WORSE** |
| F5 | `setattr`/`getattr` dynamic caching bypasses type system | **STILL PRESENT** |
| F6 | `db.rollback()` in `_save_trackers` may affect other work | **STILL PRESENT** |
| F7 | `fit()` 53 lines, `detect()` 72 lines (limit: 50) | **STILL PRESENT** |
| F10 | Silent fallback to fresh trackers | **STILL PRESENT** |

---

## New Findings (N1–N8)

### N1 — `physics_result.suppressed` branch is dead code
**File:** `src/api/services/comprehensive_pipeline.py`, lines 263–273
**Impact:** Low (no runtime effect; wasted cognitive overhead)

`PhysicsValidator.validate()` never sets `suppressed=True` — every path returns either `confirmed=True` or `suppressed=False, confirmed=False`. The `if physics_result.suppressed:` branch in `_run_with_components` and the `_suppressed_result()` helper method are unreachable dead code. The `suppressed` field in `ValidationResult` exists as a placeholder but has no triggering rule.

**Recommendation:** Either add a suppression rule (e.g., expected temperature rise during ventilation cycle) or remove the dead branch and add a `# TODO` for when suppression rules are implemented.

### N2 — `np.array_equal` on every `detect()` call is O(n)
**File:** `src/models/ensemble_detector.py`, lines 306–309
**Impact:** Medium (performance)

```python
if self._baseline_cache is None or not np.array_equal(
    self._baseline_cache, baseline
):
    self.fit_baseline(baseline)
```

Every call to `EnsembleAnomalyDetector.detect()` does an element-wise comparison of potentially large baseline arrays. For a baseline of shape `(10, 20, 4)` this is 800 comparisons per inference call. Should use identity check (`baseline is not self._baseline_cache`) or a hash/checksum for invalidation.

### N3 — Python-level nested loop in `CorrelationDetector.score()`
**File:** `src/models/ensemble_detector.py`, lines 208–214
**Impact:** Medium (performance)

```python
for t_idx in range(sequence.shape[0]):
    for s_idx, sensor in enumerate(SENSOR_NAMES[: sequence.shape[1]]):
        lo, hi = _SENSOR_BOUNDS[sensor]
        val = float(sequence[t_idx, s_idx])
        if val < lo or val > hi:
            range_violations += 1
```

This double loop counts range violations one element at a time in Python. Should be vectorized:
```python
mins = np.array([_SENSOR_BOUNDS[s][0] for s in SENSOR_NAMES[:ncols]])
maxs = np.array([_SENSOR_BOUNDS[s][1] for s in SENSOR_NAMES[:ncols]])
range_violations = int(np.sum((sequence < mins) | (sequence > maxs)))
```

### N4 — Cross-module import of private symbol `_get_manager`
**File:** `src/api/services/comprehensive_pipeline.py`, line 103
**Impact:** Low (fragile coupling)

```python
from src.api.routes.learning import _get_manager
```

The pipeline imports a private function from a route module. This inverts the dependency direction (service → route) and will break if the route module is refactored. The manager singleton should be injected into the pipeline or exposed through a public service module.

### N5 — Unused `Optional` import in `gas_anomaly_detector.py`
**File:** `src/models/gas_anomaly_detector.py`, line 11
**Impact:** Trivial (lint)

`from typing import Optional` is imported but never used anywhere in the file.

### N6 — Duplicate result-dict key sets between normal and suppressed paths
**File:** `src/api/services/comprehensive_pipeline.py`, lines 414–455 vs 462–492
**Impact:** Medium (maintenance trap)

`_run_with_components` returns a 30-key dict. `_suppressed_result` manually constructs a dict with the same 30 keys set to defaults. Any key added to the normal return must also be added to `_suppressed_result` — there is no compile-time or runtime check that they stay in sync. Should use a shared `TypedDict` or `@dataclass` as the return type.

### N7 — `LearningStateStore.save()` uses shallow `dict.update()`
**File:** `src/db/learning_state_store.py`, line 52
**Impact:** Medium (data loss risk)

```python
merged = dict(row.state_json or {})
merged.update(state)
```

If both the existing `state_json` and the new `state` contain the same top-level key with nested dict values, the new one completely replaces the old. The hotfix added merge semantics to prevent overwriting `ewma_pot_trackers`, but the merge is only one level deep. A `deep_merge` utility or explicit key-aware merge would be safer.

### N8 — `_observe_online_learner` parameter `anomaly_score` is unused
**File:** `src/api/services/comprehensive_pipeline.py`, lines 90–111
**Impact:** Trivial (dead parameter)

The `anomaly_score` parameter is accepted, logged in a `logger.debug` call, but never passed to `manager.observe()`. The docstring says "retained as a parameter for observability and future extension" — but a debug-level log is not observability. Either use it or remove it.

---

## Prioritised Improvement Plan

### P1 — Critical / High (Correctness Bugs)

#### P1.1 — Fix F1: EWMA tracker never updated during `detect()`
**File:** `src/models/gas_anomaly_detector.py`, lines 168–171
**What:** After calling `ewma_tracker.is_anomaly(z_score)`, also call `ewma_tracker.update(z_score)` so the dynamic threshold adapts to the live data stream.
**Why:** Without `update()`, the EWMA tracker is frozen at the distribution seen during `fit()`. It provides no adaptive thresholding — it's compute overhead for zero value. This is a logic bug: the whole point of EWMA is online adaptation.
**Change:**
```python
# After line 171
ewma_tracker = self._ewma_trackers.get(col)
if ewma_tracker is not None:
    ewma_anomaly = ewma_tracker.is_anomaly(z_score)
    ewma_tracker.update(z_score)  # ADD THIS LINE
```
**Tests:** Add a test that calls `detect()` N times and verifies the EWMA threshold changes between calls.

#### P1.2 — Fix F6: `db.rollback()` scope in `_save_trackers`
**File:** `src/api/services/comprehensive_pipeline.py`, line 555
**What:** Replace bare `db.rollback()` with `db.begin_nested()` savepoint around the tracker save operation.
**Why:** A bare `rollback()` undoes ALL pending work on the session — not just the failed tracker save. If another operation (e.g., incident storage) was committed in the same session, this is safe, but if any work is pending, it's lost silently.
**Change:**
```python
async def _save_trackers(self, customer_id, ewma, pot, db):
    ...
    try:
        async with db.begin_nested():
            # existing upsert logic
            ...
            # commit happens automatically on __aexit__
    except Exception:
        logger.warning("EWMA/POT DB save failed (non-critical)", exc_info=True)
        # savepoint rolled back automatically; outer transaction untouched
```

#### P1.3 — Fix F2 + F10: Upgrade silent DB failures to `logger.warning`
**File:** `src/api/services/comprehensive_pipeline.py`, lines 306, 341, 518, 554
**What:** Change all four `logger.debug(...)` calls around DB failures to `logger.warning(...)`. Add a counter or flag so that after N consecutive failures, a single `logger.error(...)` fires with an actionable message.
**Why:** `logger.debug` is invisible in production. DB persistence failures mean adaptive thresholds are silently broken — operators need to know. The GAR-1 architecture review already flagged this.
**Change:** Replace `logger.debug` → `logger.warning` at all four sites. Optionally add a `_tracker_db_failure_count` attribute to track consecutive failures and escalate.

---

### P2 — Medium (Maintainability / Performance)

#### P2.1 — Fix F3: Deduplicate `GAS_COLUMNS`
**File:** `src/models/gas_anomaly_detector.py`, line 19
**Config source:** `src/config.py`, lines 564–571
**What:** Remove the local `GAS_COLUMNS` list from `gas_anomaly_detector.py`. Import `GAS_SENSOR_COLUMNS` from `src.config` and alias it: `GAS_COLUMNS = GAS_SENSOR_COLUMNS` (preserving the public name for backward compat).
**Why:** Two identical lists of six strings that must be kept in sync manually. One source of truth.
**Change:**
```python
# gas_anomaly_detector.py
from src.config import GAS_SENSOR_COLUMNS

GAS_COLUMNS: list[str] = list(GAS_SENSOR_COLUMNS)
```

#### P2.2 — Fix F5: Replace `setattr`/`getattr` dynamic cache with typed dict
**File:** `src/api/services/comprehensive_pipeline.py`, lines 500–523, 534
**What:** Replace the dynamic `setattr(self, f"_trackers_{customer_id}", ...)` pattern with a properly typed `dict[str, tuple[EWMATracker, POTThresholdFitter]]` instance attribute.
**Why:** Dynamic attributes bypass type checking, are invisible to IDE autocompletion, and make the object's shape unpredictable. A typed dict is equally simple and fully visible.
**Change:**
```python
# In __init__:
self._tracker_cache: dict[str, tuple[EWMATracker, POTThresholdFitter]] = {}

# In _load_trackers:
cached = self._tracker_cache.get(customer_id)
if cached is not None:
    return cached
...
self._tracker_cache[customer_id] = (ewma, pot)

# In _save_trackers:
self._tracker_cache[customer_id] = (ewma, pot)
```

#### P2.3 — Fix F4: Extract `_run_with_components` into focused helpers
**File:** `src/api/services/comprehensive_pipeline.py`, lines 234–455 (338 lines)
**What:** Break the method into 5–6 focused helpers, each under 50 lines:
1. `_run_lstm_vae(seq_array)` → `(raw_score, anomaly_score, embedding, uncertainty)`
2. `_run_physics_validation(seq_array, now)` → `ValidationResult`
3. `_run_attribution_and_pattern(seq_array, attributor, pattern_detector)` → `(attribution_result, pattern_result)`
4. `_run_ensemble_and_classify(seq_array, baseline, pattern_type, bias_flag, physics_result)` → `(is_anomaly, fault_type, ensemble_result)`
5. `_build_result_dict(...)` → `Dict[str, Any]`
6. `_store_and_observe(...)` → `incident_id`
**Why:** 338 lines in one method with 8 logical sections interleaved. Each section is independently testable once extracted.

#### P2.4 — Fix F7: Extract helpers from `fit()` and `detect()`
**File:** `src/models/gas_anomaly_detector.py`
**What:** Extract from `fit()`:
- `_collect_column_values(readings)` → `dict[str, list[float]]`
- `_build_baseline(col, vals)` → `GasBaseline`

Extract from `detect()`:
- `_score_column(value, baseline, ewma_tracker)` → `GasAnomaly`
**Why:** Both methods exceed the 50-line limit. The column-scoring logic in `detect()` (lines 153–195) is a single iteration body that's perfect for extraction.

#### P2.5 — Fix N2: Replace `np.array_equal` with identity check
**File:** `src/models/ensemble_detector.py`, lines 306–309
**What:** Replace `not np.array_equal(self._baseline_cache, baseline)` with `baseline is not self._baseline_cache`. Callers reuse the same baseline array object; identity comparison is O(1).
**Why:** Element-wise comparison on every inference call is wasteful. If a caller constructs a new array with the same values, `fit_baseline` refits — which is the safe default.

#### P2.6 — Fix N3: Vectorize range-violation counting
**File:** `src/models/ensemble_detector.py`, lines 207–216
**What:** Replace the nested Python loop with numpy broadcasting.
**Why:** O(seq_len × sensors) in Python vs O(1) numpy call. For seq_len=20 and 4 sensors, this is 80 Python iterations eliminated.

#### P2.7 — Fix N6: Introduce a result `TypedDict` or `@dataclass`
**File:** `src/api/services/comprehensive_pipeline.py`
**What:** Define a `PipelineResult(TypedDict)` with all 30 keys typed. Both `_run_with_components` and `_suppressed_result` return this type. Any key mismatch is caught by mypy/pyright.
**Why:** Two manually synchronized 30-key dicts are a maintenance trap. Adding a new field to one but not the other is an easy, silent bug.

#### P2.8 — Fix N7: `LearningStateStore.save()` shallow merge
**File:** `src/db/learning_state_store.py`, line 52
**What:** Replace `merged.update(state)` with a one-level-deep merge that preserves existing nested dict keys not present in `state`. Or document explicitly that `state` must contain the complete top-level namespace for each key it touches.
**Why:** The hotfix made `save()` merge-aware, but `dict.update()` is still a top-level replacement. If a caller passes `{"ewma_pot_trackers": {...}}`, it replaces the entire tracker blob even if only one field changed.

---

### P3 — Low (Style / Polish)

#### P3.1 — Fix N5: Remove unused `Optional` import
**File:** `src/models/gas_anomaly_detector.py`, line 11
**What:** Delete `from typing import Optional`.
**Why:** Unused import.

#### P3.2 — Fix N4: Inject online learner manager instead of importing private symbol
**File:** `src/api/services/comprehensive_pipeline.py`, line 103
**What:** Accept `online_learner_manager` as an optional `__init__` parameter. Remove the deferred import of `_get_manager`.
**Why:** The current import inverts the dependency (service → route). Injection makes the dependency explicit and testable.

#### P3.3 — Fix N8: Remove or use `anomaly_score` parameter in `_observe_online_learner`
**File:** `src/api/services/comprehensive_pipeline.py`, lines 90–111
**What:** Remove the `anomaly_score` parameter if it has no planned use. If it will be used, promote the log to `logger.info`.
**Why:** Dead parameters add confusion. The docstring claims "retained for future extension" but YAGNI.

#### P3.4 — Fix N1: Document or remove suppression dead code
**File:** `src/api/services/comprehensive_pipeline.py`, lines 263–273 + 457–492
**What:** Add a `# TODO(tech-lead): no suppression rules yet — 2026-06-21` comment, or remove the branch entirely if no suppression rules are planned.
**Why:** Dead code is a maintenance trap. Readers spend time understanding code that can never execute.

#### P3.5 — Modernize typing imports
**Files:** `src/models/ensemble_detector.py`, `src/api/services/comprehensive_pipeline.py`
**What:** Replace `Dict`, `List`, `Optional`, `Sequence`, `Tuple` from `typing` with built-in generics (`dict`, `list`, `tuple`) and `X | None` for optionals. The project already uses `from __future__ import annotations` in some files.
**Why:** Consistency with modern Python style (3.10+). Several files already use lowercase generics.

---

## Recommended Execution Order

1. **P1.1 (F1)** — Critical correctness bug, smallest change, highest impact
2. **P1.3 (F2+F10)** — Visibility fix, 4 lines changed
3. **P1.2 (F6)** — Savepoint scope fix
4. **P2.1 (F3)** — Trivial dedup
5. **P2.2 (F5)** — Type-safe cache
6. **P2.5 + P2.6 (N2+N3)** — Performance quick wins
7. **P2.4 (F7)** — Extract gas detector helpers
8. **P2.3 (F4)** — Big refactor of `_run_with_components`
9. **P2.7 (N6)** — TypedDict for result
10. **P3.x** — Polish items, any order

---

## Risk Notes

- **P2.3 (F4)** is the highest-risk change — 338 lines of async orchestration being split. Must be done incrementally with full test suite runs between each extraction.
- **P1.1 (F1)** changes detection behavior — the EWMA threshold will now adapt, which could change anomaly classification for existing tenants. Roll out with monitoring.
- **P1.2 (F6)** requires testing with real async session behavior — unit tests may not expose the savepoint interaction.
