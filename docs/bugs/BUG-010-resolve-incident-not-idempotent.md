# BUG-010: resolve_incident not idempotent

**Status:** Closed (2026-06-15)  
**Severity:** Low  
**Source:** [gap2_incident_memory_review.md](../review/gap2_incident_memory_review.md) (W5)

## Summary

Re-resolving an already-resolved incident silently overwrites root cause and appends duplicate `IncidentAction` rows. No 409 or guard.

## Fix (implemented)

1. `IncidentAlreadyResolvedError` raised when `incident.resolved` is already true.
2. `POST /api/incidents/resolve/{id}` returns **409 Conflict** on double-resolve.

## Acceptance criteria

- [x] Second resolve on same incident returns 409
- [x] No duplicate action rows on double-submit
- [x] API test added (`tests/api/test_incident_api.py`)
