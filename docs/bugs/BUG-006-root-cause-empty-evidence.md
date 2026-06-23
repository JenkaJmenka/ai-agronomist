# BUG-006: _gather_evidence silently drops indicators

**Status:** Closed — 2026-06-15  
**Severity:** Medium  
**Fixed in:** Phase C

## Summary

Added `_INDICATOR_EVIDENCE` dict mapping all 18 indicator tokens to evidence strings. Unknown indicators fall back to humanized name. `compressor_failure` now returns non-empty `supporting_evidence`.
