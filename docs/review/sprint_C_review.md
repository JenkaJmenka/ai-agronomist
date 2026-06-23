# Sprint C Code Review

**Date:** 2026-06-22
**Reviewer:** code-reviewer (Claude Opus)
**Sprint:** C
**Files reviewed:**
- `src/models/gas_anomaly_detector.py` (212 lines)
- `src/models/physics_validator.py` (176 lines)
- `src/api/services/comprehensive_pipeline.py` (666 lines)

---

## Guardrail Checks

### Red Flags (`.claude/guardrails/red-flags.md`) — ALL PASS

| # | Check | Result |
|---|-------|--------|
| 1 | File writing outside ownership | PASS — models write to `src/models/`, pipeline to `src/api/services/` |
| 2 | `os.system`, `subprocess(shell=True)`, `eval()` | PASS — none found |
| 3 | Hardcoded passwords/tokens/API keys | PASS — none found |
| 4 | DELETE/DROP without `tenant_id` filter | PASS — no SQL in these files |
| 5 | `model.save()` without metadata JSON | PASS — not applicable (no checkpoints) |
| 6 | Test file importing and modifying implementation | PASS — not applicable |
| 7 | Circular imports `src/models/` ↔ `src/api/` | PASS — models import only from `src.config`; no `src.api` imports in models |

### Universal Guardrails (`.claude/guardrails/universal-guardrails.md`)

| Check | Result |
|-------|--------|
| LSTM-VAE / API tight coupling | PASS — `PhysicsValidator` and `GasAnomalyDetector` are pure model-layer; no API imports |
| Hardcoded secrets | PASS — none found |
| Checkpoint without metadata | PASS — N/A |
| DB schema without `tenant_id` | PASS — no schema changes in Sprint C |
| Test coverage <80% on critical paths | **NOTE** — see Test Coverage section below |
| Major design change without ADR | PASS — Sprint C is a refactor, not a design change |

### ML Guardrails (`.claude/guardrails/ml-guardrails.md`)

| Check | Result |
|-------|--------|
| Hard-coded sensor ranges | PASS — `physics_validator.py` imports `TEMP_RANGE`, `HUMIDITY_RANGE` from `src.config` for diurnal rule; plausibility bounds (`_TEMP_LOW=5`, `_TEMP_HIGH=45`, `_CO2_LOW=200`, `_CO2_HIGH=2000`) are intentionally wider absolute physical limits, distinct from operating ranges |
| NaN/Inf in model outputs | PASS — `gas_anomaly_detector.py` checks `math.isfinite()` on every value in both `fit()` and `detect()` |
| Checkpoint without metadata | PASS — N/A |
| Training loop without early stopping | PASS — N/A |

### WARN Guardrails

| Check | Result |
|-------|--------|
| Hard-coded thresholds not in `config.py` | **WARN** — see W1, W2 below |
| Missing error handling | PASS — pipeline helpers wrap failures in try/except with logging |
| Code duplication | PASS — no 3+ identical blocks found |
| Silent failures (bare except, pass) | PASS — all except blocks log with `exc_info=True` |

---

## File-by-File Analysis

### 1. `src/models/gas_anomaly_detector.py` — Extracted helpers

**Sprint C changes:** `_collect_column_values`, `_build_baseline`, `_score_column` extracted from `fit()` and `detect()`.

#### Correctness
- **PASS.** All three helpers are behavior-preserving extractions from the public `fit()` and `detect()` methods. Logic is identical to pre-extraction code.
- `_collect_column_values`: correctly filters None, non-numeric, and non-finite values.
- `_build_baseline`: correctly computes mean, std, Q25, Q75, IQR via numpy.
- `_score_column`: correctly implements combined z-score + IQR detection with severity classification. Zero-std guard returns `is_anomaly=False` (safe default).
- `fit()` reduced to 18 lines, `detect()` to 28 lines. All helpers under 50 lines (`_score_column` = 47). **PASS.**

#### Security
- **PASS.** No external input directly processed without validation. All values go through `float()` conversion and `math.isfinite()` check.

#### Performance
- **PASS.** No regressions from extraction. Numpy operations unchanged.

#### Type Hints
- **PASS.** Return types explicit on all methods. Parameter types annotated. `dict[str, list[float]]`, `GasBaseline`, `GasAnomaly` all concrete.

#### Style
- **N1 (INFO, carried from Sprint B):** Class attribute `GAS_COLUMNS: list[str] = GAS_COLUMNS` at line 60 is unused by any internal method (all helpers reference the module-level `GAS_COLUMNS` import directly). Only exposed to external callers. Harmless but redundant.

---

### 2. `src/models/physics_validator.py` — Diurnal suppression + timezone

**Sprint C changes:** Rule 5 (diurnal suppression) added; `utc_offset_hours` parameter added to `validate()`.

#### Correctness
- **PASS.** Rule 5 logic is correct:
  - Both temp and humidity must be non-zero-trending AND co-moving (same direction) AND gradual (below thresholds) AND within config ranges.
  - Placed after all 4 confirmation rules — a real fault always takes priority.
  - Uses `TEMP_RANGE` and `HUMIDITY_RANGE` from `src.config` for range checks.
- `utc_offset_hours` arithmetic: `(timestamp.hour + utc_offset_hours) % 24` is correct for all valid offsets. Python's `%` operator handles negative dividends correctly (e.g., `(0 + (-5)) % 24 = 19`). Verified for offsets [-12, +14] across all hours.
- **W3 (WARN):** `utc_offset_hours` accepts any integer with no bounds validation. Real-world UTC offsets range from -12 to +14. Passing `utc_offset_hours=100` produces `(hour + 100) % 24` which is mathematically safe but semantically wrong. Consider adding a bounds check or at minimum documenting the expected range.

#### Security
- **PASS.** Stateless validator with no I/O, no external calls.

#### Performance
- **PASS.** Rule 5 adds minimal overhead: numpy operations on the already-computed `temp_trend`, `hum_trend` plus 4 numpy range checks (`np.all`). No new arrays allocated beyond what rules 1-4 already computed.

#### Type Hints
- **PASS.** `utc_offset_hours: int = 0` properly typed. Return type `ValidationResult` explicit. `frozen=True` on dataclass enforces immutability.

#### Style / Complexity
- **W4 (WARN):** `validate()` is 110 lines — significantly exceeds the 50-line function limit. It contains 5 sequential rules, each 10-20 lines. While the linear structure is readable, extracting each rule into a private method (e.g., `_check_psychrometric`, `_check_nocturnal_light`, `_check_co2_range`, `_check_temperature_range`, `_check_diurnal_suppression`) would conform to the function-size guideline and make individual rules independently testable.

---

### 3. `src/api/services/comprehensive_pipeline.py` — Decomposition + PipelineResult

**Sprint C changes:** `_run_with_components` decomposed into 5 helpers; `PipelineResult` TypedDict added.

#### Correctness
- **PASS.** All 5 extracted helpers are faithful extractions from the original monolithic method:
  - `_run_lstm_vae` (16 lines): forward pass → 4-tuple. Correct.
  - `_run_physics_check` (6 lines): denormalize + validate. Correct.
  - `_run_attribution_pattern` (14 lines): attribution + pattern. Correct.
  - `_run_ensemble_classify` (38 lines): ensemble + fault classification. Correct.
  - `_store_and_observe` (45 lines): incident store + online learner. Correct.
- `PipelineResult` TypedDict defines all 28 keys matching actual returns from both `_run_with_components` and `_suppressed_result`. Verified key-by-key. **PASS.**
- Both `_run_with_components` and `_suppressed_result` annotated with `-> "PipelineResult"`. **PASS.**

#### Correctness Finding
- **N2 (NOTE):** `_run_physics_check` (line 487) calls `PhysicsValidator.validate(denormed, now)` without passing `utc_offset_hours` — the new parameter from Sprint C-ML-3 is never wired through the pipeline. All pipeline users get UTC-only nocturnal light and diurnal suppression checks. This means:
  - Nocturnal light rule fires based on UTC hour, not local greenhouse time.
  - Diurnal suppression rule checks against UTC hour implicitly.
  - For greenhouses far from UTC, this can cause false positives (e.g., noon light flagged as "nocturnal" for UTC+12) or missed suppressions.
  - **Not a regression** (behavior unchanged from pre-Sprint-C), but the parameter was added in Sprint C specifically to fix this, and it's not yet consumed.

#### Security
- **PASS.** No new security surface. Pre-existing `_get_manager` private cross-module import (line 103) is unchanged and was noted in prior reviews.

#### Performance
- **PASS.** No performance regressions. Helper extraction adds no overhead (same code, reorganized into method calls).

#### Type Hints
- **W5 (WARN):** Three `Any` type annotations weaken type safety:
  - `_run_physics_check` returns `Any` (line 484) — should return `ValidationResult`.
  - `_run_ensemble_classify` accepts `physics_result: Any` (line 511) — should be `ValidationResult`.
  - `_run_ensemble_classify` returns `tuple[bool, str, Any]` (line 512) — the `Any` is the ensemble result; should import and use the concrete type.
  
  These `Any` annotations bypass static type checking and violate the "never use `any`" guardrail. The actual types are well-known and available for import.

#### Style / Complexity
- **W6 (WARN):** `_run_with_components` at 157 lines still exceeds the 50-line function limit. It IS now a thin orchestrator calling helpers in sequence (steps 1-9), which is a significant improvement from the pre-Sprint-C 338-line monolith. However, 157 lines of orchestration logic is still long. Consider extracting steps 7-8 (confidence + root cause + recommendations) into a `_run_scoring_and_analysis` helper.
- **W7 (WARN):** Three methods exceed the 3-parameter limit:
  - `_run_with_components`: 8 parameters (after self). Consider a `PipelineContext` dataclass.
  - `_run_ensemble_classify`: 6 parameters (after self). Consider a parameter object.
  - `_store_and_observe`: 11 parameters (after self). Should definitely use a parameter object or receive the already-built pipeline context.
- **W8 (WARN, pre-existing):** File at 666 lines — more than 2x the 300-line cap. The EWMA/POT tracker persistence methods (`_load_trackers`, `_save_trackers`, 58 lines total) and learning progress (`_learning_progress`, 15 lines) are natural candidates for extraction into a separate `tracker_persistence.py` module.

---

## Test Coverage Assessment

### Gas Anomaly Detector Helpers
- **Indirectly covered.** 7 tests in `TestGasAnomalyDetector` exercise `fit()` and `detect()`, which call all three extracted helpers. Coverage: baseline computation, zero-std guard, severity levels, missing columns, normal vs. extreme values. Sufficient for a behavior-preserving refactor.

### Diurnal Suppression Rule (Rule 5)
- **NOT TESTED.** No test in the repository creates a sequence that triggers `normal_diurnal_variation`. The existing `TestPhysicsValidator` tests (8 tests) cover rules 1-4 and the no-match/short-sequence paths, but none constructs a co-moving, gradual, in-range sequence. This is a new feature with zero direct test coverage.

### `utc_offset_hours` Parameter
- **NOT TESTED.** All existing `PhysicsValidator.validate()` calls in tests use the default `utc_offset_hours=0`. No test verifies that a non-zero offset shifts the nocturnal light boundary correctly.

### Pipeline Helpers
- **Indirectly covered.** 61 tests in `test_comprehensive_pipeline_sprint_a.py` exercise the full pipeline path, which calls all 5 extracted helpers. 3 tests specifically cover `_suppressed_result`. Sufficient for a behavior-preserving refactor.

### Pipeline `utc_offset_hours` Wiring
- **NOT APPLICABLE.** The pipeline does not yet pass `utc_offset_hours` to `PhysicsValidator.validate()` (see N2).

---

## Summary of Findings

### By Severity

| ID | Severity | File | Finding |
|----|----------|------|---------|
| W1 | WARN | `physics_validator.py` | Module-level physics constants (`_TEMP_TREND_THRESHOLD`, `_HUM_TREND_THRESHOLD`, `_NOCTURNAL_LIGHT_THRESHOLD`, `_NIGHT_HOURS`) not in `src/config.py` — reasonable for domain-specific physics but noted per guardrail |
| W2 | WARN | `gas_anomaly_detector.py` | `_Z_WARNING`, `_Z_CRITICAL`, `_IQR_FENCE` are standard statistical constants (Tukey's rule) kept as module constants — acceptable but noted per guardrail |
| W3 | WARN | `physics_validator.py` | `utc_offset_hours` accepts any integer with no bounds validation (expected: -12 to +14) |
| W4 | WARN | `physics_validator.py` | `validate()` at 110 lines exceeds 50-line function limit; 5 rules could be separate methods |
| W5 | WARN | `comprehensive_pipeline.py` | Three `Any` type annotations in `_run_physics_check` return and `_run_ensemble_classify` params/return lose type safety |
| W6 | WARN | `comprehensive_pipeline.py` | `_run_with_components` still 157 lines (was 338 pre-Sprint-C; improved but still 3x over limit) |
| W7 | WARN | `comprehensive_pipeline.py` | Three methods exceed 3-parameter limit: `_run_with_components` (8), `_store_and_observe` (11), `_run_ensemble_classify` (6) |
| W8 | WARN | `comprehensive_pipeline.py` | File at 666 lines (2.2x the 300-line cap); tracker persistence is extractable |
| N1 | INFO | `gas_anomaly_detector.py` | Unused `GAS_COLUMNS` class attribute (carried from Sprint B) |
| N2 | NOTE | `comprehensive_pipeline.py` | `_run_physics_check` does not pass `utc_offset_hours` to `PhysicsValidator.validate()` — new param from C-ML-3 is unwired |
| N3 | NOTE | tests | Diurnal suppression rule (Rule 5) has zero test coverage |
| N4 | NOTE | tests | `utc_offset_hours` parameter has zero test coverage |

### Counts

- **STOP WORK:** 0
- **WARN:** 8 (W1-W8)
- **NOTE/INFO:** 4 (N1-N4)
- **Security issues:** 0
- **Correctness bugs:** 0

---

## Positive Observations

1. **Refactoring goals achieved.** `_run_with_components` decomposed from 338 → 157 lines with 5 well-named, well-documented helpers. Each helper is under 50 lines.
2. **PipelineResult TypedDict** properly defines all 28 return keys, giving downstream consumers type-checked access. Both return paths (`_run_with_components` and `_suppressed_result`) are annotated.
3. **Gas detector helpers** are clean extractions with good docstrings and correct type annotations.
4. **Diurnal suppression rule** is well-designed: co-movement + gradual change + in-range checks, placed after all confirmation rules to avoid masking real faults.
5. **Backward compatibility** preserved: `utc_offset_hours=0` default, no call-site changes required.
6. **All guardrail STOP WORK conditions clear.** No security, coupling, or architectural violations.

---

## Recommendations for Next Sprint

1. **Add tests for diurnal suppression (N3) and `utc_offset_hours` (N4)** — these are new features with zero coverage.
2. **Wire `utc_offset_hours` through the pipeline (N2)** — add it to the `run()` API and pass to `_run_physics_check`.
3. **Replace `Any` with concrete types (W5)** — `ValidationResult` and the ensemble result type are available.
4. **Consider extracting tracker persistence (W8)** — `_load_trackers`/`_save_trackers` (~58 lines) into a `tracker_persistence.py` to bring the file closer to the 300-line cap.
5. **Add `utc_offset_hours` bounds check (W3)** — `if not (-12 <= utc_offset_hours <= 14): raise ValueError(...)`.

---

## Verdict

**PASS WITH NOTES**

All Sprint C refactoring objectives are met. No blocking issues, no security concerns, no correctness bugs. The 8 WARN-level findings are either pre-existing size/complexity issues (improved but not fully resolved) or minor type-safety gaps. The 4 NOTE-level findings flag missing test coverage for new features (diurnal suppression, UTC offset) and an unwired parameter — these should be addressed in the QA phase or next sprint but do not block the current Sprint C work.
