# Refactor R1, R3, R4, R5, R6 -- 2026-06-16

## R1: Extract data dicts from root_cause_analyzer.py to diagnosis_rules.py

**Problem:** `root_cause_analyzer.py` was 426 lines, exceeding the 300-line cap. The excess was pure data: `DIAGNOSIS_RULES`, `_INDICATOR_EVIDENCE`, `_HIGH_SEVERITY_PATTERNS`, `_MEDIUM_SEVERITY_PATTERNS`.

**Change:**
- Created `src/models/diagnosis_rules.py` (128 lines) with all four data items moved verbatim.
- `root_cause_analyzer.py` imports them from the new module; `DIAGNOSIS_RULES` remains a class attribute via assignment.
- `root_cause_analyzer.py` reduced to ~261 lines (under 300-line cap).

**Backward compatibility:** `RootCauseAnalyzer.DIAGNOSIS_RULES` still accessible as a class attribute. All tests import `RootCauseAnalyzer`, not the dicts directly.

---

## R3: Extract duplicate z-score expression in pattern_scores.py

**Problem:** The expression `np.abs(sequence - baseline.mean) / (baseline.std + NUMERICAL_EPSILON)` was duplicated in `score_spike` (line 112) and `score_cascade` (line 229).

**Change:**
- Added `_z_scores(sequence, baseline)` helper function after the `PatternBaseline` dataclass.
- Both `score_spike` and `score_cascade` now call `_z_scores(sequence, baseline)`.

**Backward compatibility:** Pure internal refactor. No public API change.

---

## R4: Expose hardcoded `// 4` lag-window divisor as config constant

**Problem:** `_is_pair_lagged` used `len(x) // 4` with a magic number.

**Change:**
- Added `PATTERN_LAG_WINDOW_FRACTION: int = 4` to `src/config.py` in the `PATTERN_*` block.
- `_is_pair_lagged` now uses `len(x) // PATTERN_LAG_WINDOW_FRACTION`.

**Backward compatibility:** Default value is 4, preserving existing behavior exactly.

---

## R5: Cache AnomalyAttributor + PatternDetector per batch request

**Problem:** `ComprehensiveInferencePipeline.run()` created a new `AnomalyAttributor` and `PatternDetector` on every call. In the batch endpoint, they were recreated for every sequence even though the baseline is the same.

**Change:**
- Extracted core pipeline logic from `run()` into `_run_with_components()` private method.
- `run()` creates attributor/pattern_detector and delegates to `_run_with_components()`.
- Added `run_batch()` method that creates components once, calls `set_baseline` once, then loops `_run_with_components()` for each sequence.
- Updated `inference_integrated.py` batch endpoint to use `pipeline.run_batch()`.

**Backward compatibility:** `run()` public API unchanged. `run_batch()` is a new method (not a public API change -- it is additive). Batch endpoint behavior unchanged.

---

## R6: Set writeable=False on PatternBaseline numpy arrays

**Problem:** `PatternBaseline` is `@dataclass(frozen=True)` but its numpy array fields are mutable objects. A caller could silently corrupt shared state via `baseline.mean[0] = 999.0`.

**Change:**
- Added `_freeze(array)` helper that sets `array.flags.writeable = False`.
- All numpy arrays in `compute_baseline()` are frozen before being passed to `PatternBaseline(...)`.
- Fields affected: `mean`, `std`, `correlations`, `min_values`, `max_values`, `dominant_frequency_bins`, `dominant_power_fractions`.

**Backward compatibility:** Any code that accidentally mutated baseline arrays will now raise `ValueError: assignment destination is read-only`. This is intentional -- such mutation was always a bug.

---

## Files changed

| File | Action |
|------|--------|
| `src/models/diagnosis_rules.py` | Created (128 lines) |
| `src/models/root_cause_analyzer.py` | Modified (426 -> ~261 lines) |
| `src/models/pattern_scores.py` | Modified (R3 + R4 + R6) |
| `src/config.py` | Modified (added PATTERN_LAG_WINDOW_FRACTION) |
| `src/api/services/comprehensive_pipeline.py` | Modified (R5 refactor) |
| `src/api/routes/inference_integrated.py` | Modified (R5 batch endpoint) |
