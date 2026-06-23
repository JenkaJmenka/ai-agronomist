# Priority A Algorithm Compliance Review

**Date:** 2026-06-22
**Reviewer:** code-reviewer (Claude Opus)
**Verdict:** **NEEDS FIXES** (1 critical, 1 high, 3 medium, 3 low)

---

## Scope

Review of three new fault detection algorithms (CUSUM drift, polling dropout, spike velocity+reversal) and their integration into the comprehensive inference pipeline, per Priva SCADA specification.

### Files Reviewed

| File | Lines | Status |
|------|-------|--------|
| `src/models/cusum_drift_detector.py` | 130 | New |
| `src/models/dropout_detector.py` | 60 | New |
| `src/models/drift_detector.py` | 215 | Modified (CUSUM stub) |
| `src/models/pattern_scores.py` | 372 | Modified (velocity+reversal) |
| `src/models/fault_classifier.py` | 79 | Modified (POLLING_DROPOUT) |
| `src/config.py` | 597 | Modified (new constants) |
| `src/api/services/comprehensive_pipeline.py` | 792 | Modified (dropout+CUSUM wiring) |
| `src/db/learning_state_store.py` | 89 | Modified (CUSUM persistence) |
| `alembic/versions/012_add_polling_dropout_fault_type.py` | 64 | New |

### Tests Reviewed

| File | Coverage |
|------|----------|
| `tests/models/test_dropout_detector.py` | Thorough (empty, threshold, partial, edge cases) |
| `tests/models/test_cusum_drift_detector.py` | Thorough (construction, update, reset, serialization) |
| `tests/models/test_fault_classifier.py` | Thorough (priority chain, enum, existing rules) |
| `tests/api/test_comprehensive_pipeline_sprint_a.py` | Thorough (all wiring paths) |

---

## Critical Findings

### C-1: Pipeline imports CUSUM stub, not the real implementation (CRITICAL)

**Severity:** CRITICAL
**Files:** `src/api/services/comprehensive_pipeline.py:42`, `src/models/drift_detector.py:106-174`

The pipeline imports `CUSUMDriftDetector` from the **stub** module:

```python
# comprehensive_pipeline.py line 42
from src.models.drift_detector import CUSUMDriftDetector
```

The `drift_detector.py` `CUSUMDriftDetector.update()` (lines 150-174) is a **stub that always returns `False`**:

```python
def update(self, sensor: str, residual: float) -> bool:
    # TODO(ml-engineer): replace with real CUSUM recurrence - 2026-06-22
    s_pos, s_neg = self._stats.get(sensor, (0.0, 0.0))
    _ = residual  # consumed once real logic is in place
    self._stats[sensor] = (s_pos, s_neg)
    return False
```

The **real implementation** exists in `src/models/cusum_drift_detector.py` with correct two-sided CUSUM recurrence, but **nothing in the pipeline imports it**. The test file `test_cusum_drift_detector.py` tests the real module, so tests pass, but production drift detection is completely non-functional.

**Impact:** CUSUM calibration drift detection is a no-op. The `cusum_drift_alarm` flag in the pipeline will never be `True`. The CUSUM override at step 5a (line 391) will never fire. Persisted CUSUM state is all zeros.

**Fix:** Change the import in `comprehensive_pipeline.py` line 42 from:
```python
from src.models.drift_detector import CUSUMDriftDetector
```
to:
```python
from src.models.cusum_drift_detector import CUSUMDriftDetector
```

Then remove or deprecate the stub class in `drift_detector.py`.

**Serialization caveat:** The two implementations have **incompatible `to_dict`/`from_dict` formats** (see M-1). Any state persisted while the stub was active will silently produce empty accumulators when loaded by the real implementation. This is acceptable (graceful degradation to fresh start) but should be documented.

---

### H-1: Dropout detector unreachable through current API contract (HIGH)

**Severity:** HIGH
**Files:** `src/api/services/comprehensive_pipeline.py:292-310`, `src/models/dropout_detector.py`

The dropout detector checks for rows where `all(v is None for v in row.values())`. However, in the pipeline wiring (lines 295-298):

```python
_seq_as_dicts = [
    {name: val for name, val in zip(SENSOR_NAMES[: len(row)], row)}
    for row in sequence_raw
]
```

`sequence_raw` arrives as `List[List[float]]` from `run()`, or `seq_array.tolist()` from `run_batch()`. Neither path can produce Python `None` values in the dicts:
- `run()` accepts `sequence: List[List[float]]` -- type annotation excludes `None`
- `np.asarray(sequence, dtype=np.float32)` converts `None` to `np.nan`, but `.tolist()` produces `float('nan')`, not `None`
- The shape check `seq_array.shape != (SEQ_LEN, INPUT_DIM)` runs before the dropout check, and sequences with missing rows wouldn't pass it

The dropout detector thus never fires in the current pipeline flow. The code comment acknowledges this ("future extension point"), but the feature is wired end-to-end (migration, classifier rule, result dict, tests) without being reachable.

**Fix options:**
1. Accept `Optional[List[Optional[List[Optional[float]]]]]` and handle None at API boundary, OR
2. Check for `np.nan` instead of (or in addition to) `None` in `_is_all_none()`, OR
3. Add the dropout check at the API route level before numpy conversion, using the raw JSON payload

---

## Medium Findings

### M-1: Incompatible CUSUM serialization formats between stub and real implementation (MEDIUM)

**Severity:** MEDIUM
**Files:** `src/models/cusum_drift_detector.py:103-129`, `src/models/drift_detector.py:189-214`

The two `CUSUMDriftDetector` implementations serialize to different JSON schemas:

| Key | `cusum_drift_detector.py` (real) | `drift_detector.py` (stub) |
|-----|----------------------------------|---------------------------|
| Accumulator state | `"s_pos": {...}, "s_neg": {...}` | `"stats": {...}, "alarms": [...]` |
| Per-sensor format | `{sensor: float}` | `{sensor: [s_pos, s_neg]}` |

When C-1 is fixed (switching the import), any CUSUM state persisted in `LearningState.state_json["cusum_state"]` under the stub format will be silently ignored by `from_dict()` of the real implementation (it falls back to empty defaultdicts). This is safe but means accumulated CUSUM history is lost on upgrade.

**Recommendation:** Add a one-time migration log message in `from_dict()` when it detects an unrecognized format, or add format-version key to the serialized dict.

### M-2: Velocity limits duplicated between config.py and pattern_scores.py (MEDIUM)

**Severity:** MEDIUM
**Files:** `src/config.py:577-582`, `src/models/pattern_scores.py:48-53`

Identical velocity limits are defined in two places:

```python
# src/config.py line 577
VELOCITY_LIMITS: dict[str, float] = {
    "temperature": 2.0, "humidity": 5.0, "co2": 50.0, "light": 200.0,
}

# src/models/pattern_scores.py line 48
_SPIKE_VELOCITY_LIMITS: dict[str, float] = {
    "temperature": 2.0, "humidity": 5.0, "co2": 50.0, "light": 200.0,
}
```

`pattern_scores.py` does not import from `config.py`. If either is updated independently, the values diverge silently.

**Fix:** Replace the local `_SPIKE_VELOCITY_LIMITS` with an import:
```python
from src.config import VELOCITY_LIMITS as _SPIKE_VELOCITY_LIMITS
```

### M-3: DropoutDetector threshold not sourced from config.py (MEDIUM)

**Severity:** MEDIUM
**Files:** `src/models/dropout_detector.py:30`, `src/config.py:589`

The detector hardcodes its threshold as a class variable:
```python
# dropout_detector.py line 30
PERSISTENCE_THRESHOLD: float = 0.25
```

While `config.py` defines the same value:
```python
# config.py line 589
DROPOUT_PERSISTENCE_THRESHOLD: float = 0.25
```

The detector does not read from config. These could diverge if someone tunes the config constant without updating the class.

**Fix:** Import and use the config constant:
```python
from src.config import DROPOUT_PERSISTENCE_THRESHOLD
# ...
PERSISTENCE_THRESHOLD: float = DROPOUT_PERSISTENCE_THRESHOLD
```

---

## Low Findings

### L-1: Spike velocity boost constant not in config.py (LOW)

**Severity:** LOW
**File:** `src/models/pattern_scores.py:56`

`_SPIKE_VELOCITY_BOOST = 1.25` is a magic number defined locally. Per project convention, tunables belong in `config.py`.

### L-2: run_batch() loses None values from sequence_raw (LOW)

**Severity:** LOW
**File:** `src/api/services/comprehensive_pipeline.py:269`

`run_batch()` passes `seq_array.tolist()` as `sequence_raw`, which converts NaN back to `float('nan')`, never `None`. If a future caller relies on None-based dropout detection, `run_batch()` would silently convert None -> NaN -> float, bypassing the detector. Minor inconsistency with `run()` which preserves the original list.

### L-3: CUSUM update uses only the last timestep (LOW)

**Severity:** LOW
**File:** `src/api/services/comprehensive_pipeline.py:327`

```python
_seq_last = seq_array[-1]  # most-recent timestep for residual computation
```

Only one data point per pipeline call updates the CUSUM accumulators. For a 20-step sequence arriving every few minutes, this means 19/20 data points are discarded from drift tracking. This is a valid design choice (CUSUM operates on sequential observations, and using only the latest avoids double-counting overlapping windows), but it means detection latency scales with the pipeline call interval, not the sensor sampling rate. Document this choice.

---

## Review Focus Area Analysis

### 1. CUSUM Algorithm Correctness

**Real implementation (`cusum_drift_detector.py`):** CORRECT

```python
self._s_pos[sensor] = max(0.0, self._s_pos[sensor] + standardised_residual - self.k)
self._s_neg[sensor] = max(0.0, self._s_neg[sensor] - standardised_residual - self.k)
return self._s_pos[sensor] > self.h or self._s_neg[sensor] > self.h
```

- S+ = max(0, S+ + residual - k) -- matches spec
- S- = max(0, S- - residual - k) -- matches spec
- Alarm when S+ > h or S- > h -- matches spec (strict inequality, correct per Hawkins & Olwell)
- Input validation: k >= 0 and h >= 0 enforced in `__init__`
- Per-sensor independence via `defaultdict(float)` -- correct

**Stub implementation (`drift_detector.py`):** NON-FUNCTIONAL (see C-1)

### 2. Dropout Detector Edge Cases

| Case | Handling | Status |
|------|----------|--------|
| Empty sequence | `if total == 0: return False` | CORRECT |
| All valid | All values non-None, ratio = 0 | CORRECT |
| All null | All values None, ratio = 1.0 | CORRECT |
| Exactly at threshold | `>=` comparison, returns True | CORRECT |
| Empty dict row | `if not row: return True` | CORRECT |
| Partial nulls | `all(v is None)` returns False if any non-None | CORRECT |
| Mixed sensor counts | Works per-row regardless of key count | CORRECT |
| **Pipeline reachability** | **Never receives None values** | **UNREACHABLE (see H-1)** |

### 3. Spike Velocity + Reversal Check

The implementation in `_has_velocity_reversal()` (pattern_scores.py:163-190) is **correct**:

- Guard: `sequence.shape[0] < 3` returns False (need >= 2 diffs for reversal check)
- Velocity exceeding: `abs_diffs[t] > velocity_limit`
- Reversal: `col_diffs[t] * col_diffs[t + 1] < 0` (sign change in consecutive diffs)
- Boundary safety: `sensor_idx >= len(sensor_names)` guard prevents IndexError
- Unknown sensors: `velocity_limit is None` guard skips sensors not in limits dict
- Zero-velocity edge case: product is 0, not < 0, correctly not detected as reversal
- Applied only to already-identified spike sensors (`affected_indices`)

### 4. Pipeline Wiring

| Check | Status |
|-------|--------|
| Dropout check before LSTM-VAE | CORRECT (line 299 vs 313) |
| Dropout short-circuits (returns early) | CORRECT (line 310) |
| CUSUM after LSTM-VAE | CORRECT (line 316) |
| CUSUM baseline stats from `baseline.mean/std(axis=(0,1))` | CORRECT |
| CUSUM `_baseline_std.clip(min=NUMERICAL_EPSILON)` | CORRECT (prevents div-by-zero) |
| CUSUM `if _std > 0:` guard | CORRECT (redundant with clip, but safe) |
| CUSUM override respects physics priority | CORRECT (`not physics_result.confirmed` guard) |
| CUSUM state cached per tenant | CORRECT (`_cusum_cache` dict) |
| CUSUM state restored from DB on tracker load | CORRECT (line 722-724) |
| CUSUM state saved alongside EWMA/POT | CORRECT (line 759) |
| **But: imports stub, not real implementation** | **BROKEN (C-1)** |

### 5. CUSUM State Persistence (to_dict/from_dict)

**Real implementation (`cusum_drift_detector.py`):**
- `to_dict()`: Converts `defaultdict` to plain `dict` via `dict(self._s_pos)` -- JSON-safe
- `from_dict()`: Restores `defaultdict(float, ...)` -- new sensors auto-initialize to 0.0
- Round-trip tested and passing in `test_cusum_drift_detector.py`
- Falls back to defaults on missing keys (`d.get("k", 0.5)`)
- **No data loss on round-trip**

**Stub (`drift_detector.py`):**
- Different serialization format (see M-1)
- Serializes `_alarms` list that is never populated (always empty)
- `from_dict()` has defensive `isinstance(pair, (list, tuple)) and len(pair) == 2` guard

### 6. Migration (012)

The migration is **correct**:

- CHECK constraint expression: `fault_type IS NULL OR fault_type IN ('calibration_drift', 'bias_offset', 'stuck_fault', 'accuracy_decline', 'spike_fault', 'unknown', 'polling_dropout')` -- includes all `FaultType` enum values plus NULL
- Uses `batch_alter_table` for SQLite compatibility (required for constraint changes)
- Clean upgrade: drops old constraint, adds new one with `polling_dropout`
- Clean downgrade: restores original constraint without `polling_dropout`
- Constraint name `ck_incidents_fault_type` is consistent
- Revision chain: `012` -> `011` is correct

### 7. Security & Correctness

| Check | Status |
|-------|--------|
| Division by zero in dropout detector | Guarded (`if total == 0: return False`) |
| Division by zero in CUSUM residual | Guarded (`_baseline_std.clip(min=NUMERICAL_EPSILON)` + `if _std > 0:`) |
| No `any` type usage | Clean -- uses `dict[str, Any]` only for JSON dicts |
| No hardcoded secrets | Clean |
| No eval/exec | Clean |
| SQL injection | N/A (uses SQLAlchemy ORM) |
| Error swallowing | All `except` blocks log with context |
| Type hints on public APIs | Present and accurate |
| Input validation | Shape check at entry, ValueError for bad input |

---

## Test Quality Assessment

### test_dropout_detector.py -- EXCELLENT

- Covers: empty, all-valid, all-null, threshold boundary (exact and just-below), partial nulls, empty dicts, mixed sensor counts
- Each test has descriptive name and single assertion concept
- Threshold value explicitly verified

### test_cusum_drift_detector.py -- EXCELLENT

- Covers: construction validation, sustained positive/negative drift, independent sensors, accumulator reset mechanics, exact alarm step calculation, serialization round-trip
- Includes computed assertion: "S+ at step 5 = 5.0 (not >), step 6 = 6.0 (>5.0)" -- verifies strict inequality
- `from_dict` with empty dict fallback tested

### test_fault_classifier.py -- GOOD

- Covers: enum membership, priority chain, case sensitivity, all existing rules unchanged
- Missing: no test for `polling_dropout` + high noise (would still return `polling_dropout` due to priority)

### test_comprehensive_pipeline_sprint_a.py -- GOOD

- Covers: dropout result schema, CUSUM wiring, EWMA/POT failure paths, save/load
- Missing: no test that CUSUM actually fires in the pipeline (would require non-stub import)
- Missing: no test for `_dropout_result()` schema completeness (similar to `_suppressed_result` test at line 447)

---

## Verdict: NEEDS FIXES

### Must fix before merge:
1. **C-1:** Switch CUSUM import from stub to real implementation

### Should fix before merge:
2. **H-1:** Make dropout detection reachable (accept None or check NaN)
3. **M-2:** Deduplicate velocity limits (import from config)
4. **M-3:** Source dropout threshold from config

### Can fix post-merge:
5. **M-1:** Add format-version to CUSUM serialization
6. **L-1, L-2, L-3:** Low-priority cleanup

---

## Appendix: CUSUM Recurrence Verification

For reference, the two-sided CUSUM spec (Hawkins & Olwell, 1998):

```
S+(t) = max(0, S+(t-1) + z(t) - k)
S-(t) = max(0, S-(t-1) - z(t) - k)

where z(t) = (x(t) - mu_0) / sigma_0
alarm when S+(t) > h or S-(t) > h
```

**`cusum_drift_detector.py` implementation matches exactly.** The stub in `drift_detector.py` does not implement this recurrence.
