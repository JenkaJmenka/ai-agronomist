# BUG-003: get_statistics() operator list from buffer, not validator

**Status:** Closed — 2026-06-15  
**Severity:** Low (reporting-only)  
**Fixed in:** Phase B

## Summary

`_get_operator_ids()` now unions validator-tracked operators with current buffer IDs. Evicted operators remain in `operator_reliability` when validator has their EMA state.
