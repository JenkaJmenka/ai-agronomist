# BUG-005: No low-count reliability dampening for new operators

**Status:** Closed — 2026-06-15  
**Severity:** Low  
**Fixed in:** Phase B

## Summary

Added `ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES=5` and dampening in `get_operator_reliability()`: reliability blends toward default until outcome count reaches threshold.
