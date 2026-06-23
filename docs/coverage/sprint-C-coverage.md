# Sprint C Coverage Report

**Date:** 2026-06-22
**Agent:** qa-engineer
**Sprint:** C
**Run command:** `./venv/bin/pytest tests/ --cov=src --cov-fail-under=80 -q`

---

## Summary

| Metric | Value |
|--------|-------|
| Total coverage | **88.61%** |
| Coverage floor (80%) | ✅ PASSED |
| Total tests | 1222 passed, 12 skipped, 1 xfailed |
| Pre-existing failures | 4 (unchanged) |
| New failures | **0** |
| New tests added | 17 |

---

## Sprint C Changed Files — Coverage

| File | Stmts | Miss | Coverage | Uncovered lines |
|------|-------|------|----------|-----------------|
| `src/models/gas_anomaly_detector.py` | 89 | 0 | **100.00%** | — |
| `src/models/physics_validator.py` | 48 | 0 | **100.00%** | — |
| `src/api/services/comprehensive_pipeline.py` | 235 | 0 | **99.63%** | 608→616 (EWMA/POT DB-save branch) |

---

## Sprint C Changes Tested

### `src/models/gas_anomaly_detector.py`
Helpers decomposed from `fit()` and `detect()`:

| New Test | Covers |
|----------|--------|
| `test_collect_column_values_skips_non_convertible_string` | `_collect_column_values` ValueError path (lines 85–86) |
| `test_collect_column_values_skips_none_values` | `_collect_column_values` None skip (line 81) |
| `test_collect_column_values_skips_inf_float` | `_collect_column_values` non-finite branch (line 87→79) |
| `test_detect_skips_non_convertible_string_in_values` | `detect()` ValueError path (lines 207–208) |
| `test_detect_skips_non_finite_value` | `detect()` non-finite skip (line 210) |
| `test_score_column_iqr_only_anomaly_uses_iqr_method` | `_score_column` IQR-only anomaly (`method="iqr"`) |
| `test_score_column_iqr_zero_does_not_produce_iqr_anomaly` | `_score_column` iqr==0 branch (line 135→140) |
| `test_build_baseline_computes_correct_statistics` | `_build_baseline` statistics |

### `src/models/physics_validator.py`
Diurnal suppression rule (Rule 5) + `utc_offset_hours` parameter:

| New Test | Covers |
|----------|--------|
| `test_diurnal_suppression_fires_for_co_moving_gradual_trends` | Rule 5: suppressed=True, reason="normal_diurnal_variation" |
| `test_diurnal_suppression_not_fired_when_trends_oppose` | Rule 5: co-moving=False → not suppressed |
| `test_diurnal_suppression_not_fired_when_temp_constant` | Rule 5: temp constant → is_temp_changing=False |
| `test_diurnal_suppression_not_fired_when_temp_out_of_greenhouse_range` | Rule 5: temp_in_range=False |
| `test_diurnal_suppression_has_lower_priority_than_confirmation_rules` | Rule 5 never fires when Rule 3 precedes it |
| `test_utc_offset_hours_shifts_local_hour_into_night` | `utc_offset_hours` shifts hour into NIGHT_HOURS |
| `test_utc_offset_hours_shifts_local_hour_out_of_night` | `utc_offset_hours` shifts hour out of NIGHT_HOURS |
| `test_utc_offset_zero_default_matches_explicit` | default offset==0 == explicit 0 |

### `src/api/services/comprehensive_pipeline.py`
`_run_with_components` helpers + `PipelineResult` TypedDict: already at 99.63% from prior tests.
The 5 new helpers (`_run_lstm_vae`, `_run_physics_check`, `_run_attribution_pattern`,
`_run_ensemble_classify`, `_store_and_observe`) are exercised via the existing
comprehensive pipeline integration tests in `tests/api/test_comprehensive_pipeline_sprint_a.py`.

---

## Integration Test Fix

**File:** `tests/integration/test_end_to_end.py`
**Test:** `TestBatchAndMonitoring::test_metrics_pattern_distribution_after_batch`

**Root cause:** Sprint C's diurnal suppression rule (Rule 5 in `PhysicsValidator`) fires for
`sample_sequence(seed=11)` — that seed produces a sequence where temperature and humidity
mean diffs are co-moving (both positive), gradual (`< 0.5°C/step`, `< 1.0%/step`), and
all values remain within greenhouse physical ranges. The sequence is suppressed before
`metrics.record_detection()` is reached, causing `total_detections=1` instead of `>=2`.

**Fix:** Replaced `sample_sequence(11)` with `sample_sequence(12)` (verified non-suppressed).

---

## Bug Report

**Bug:** Physically-suppressed sequences are silently dropped from `total_detections` metrics.

When `PhysicsValidator` triggers the diurnal suppression rule, `_run_with_components` returns
early via `_suppressed_result()` **before** reaching the `metrics.record_detection()` call
(line 388 in `comprehensive_pipeline.py`). This means:
- A caller submitting N sequences to `/api/inference/batch` may see `total_detections < N`
  in `/api/monitoring/metrics`, with no indication of how many were suppressed.
- The `total_detections` counter represents "sequences that passed physics validation and
  were fully scored", **not** "sequences submitted".

**Severity:** Low — the behavior is intentional (suppressed anomalies are not real detections)
but is undocumented and caused a test regression when Sprint C introduced the new rule.

**Recommendation (implementation change, not applied here):**
Add a separate `suppressed_count` metric alongside `total_detections` so operators can
distinguish pipeline throughput from anomaly detection volume.

---

## Pre-existing Failures (unchanged)

| Test | Status |
|------|--------|
| `tests/api/test_upload.py::test_upload_invalid_csv_format` | FAILED (pre-existing) |
| `tests/api/test_upload.py::test_upload_nan_values` | FAILED (pre-existing) |
| `tests/api/test_upload.py::test_upload_out_of_range` | FAILED (pre-existing) |
| `tests/integration/test_end_to_end.py::test_error_recovery` | FAILED (pre-existing) |
