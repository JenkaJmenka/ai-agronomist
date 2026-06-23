# BUG-001: PatternDetector single-sensor crash

**Status:** Closed — 2026-06-15  
**Severity:** Medium  
**Fixed in:** Phase A (fix-deferred-items-plan)  
**Source:** [pattern_detector_review.md](../review/pattern_detector_review.md) (F1)

## Summary

`PatternDetector` with a single sensor name crashed during `detect()`. `np.corrcoef` of one variable returned a 0-d array; `np.triu_indices_from` raised `ValueError`.

## Fix applied

`np.atleast_2d` wrapped around `np.nan_to_num` in `_safe_corrcoef` (`src/models/pattern_scores.py`).

## Verification

- `tests/models/test_pattern_detector.py`: **85 passed, 0 xfailed**
- xfail promoted; 2-sensor regression test added (`test_two_sensor_dependency_through_detect`)
