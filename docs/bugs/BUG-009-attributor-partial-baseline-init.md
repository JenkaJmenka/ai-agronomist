# BUG-009: Partially-initialized state on baseline failure

**Status:** Closed — 2026-06-15  
**Severity:** Low  
**Fixed in:** Phase C

## Summary

`set_baseline()` now computes `baseline_error` before assigning `baseline_sequences`. On reconstruction failure, both attributes remain `None`.

## Verification

`test_should_raise_when_model_output_is_non_finite_at_baseline` asserts `baseline_sequences is None` after failed setup.
