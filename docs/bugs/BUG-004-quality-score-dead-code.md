# BUG-004: quality_score field is dead code

**Status:** Closed — 2026-06-15  
**Severity:** Low (design gap)  
**Fixed in:** Phase B

## Summary

Removed `quality_score` field from `FeedbackCollector.collect()` and deleted unused `get_high_quality()`. Single quality concept via `FeedbackValidator.get_operator_reliability()`.
