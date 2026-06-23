# BUG-007: Auth missing on feedback and inference endpoints

**Status:** Closed (2026-06-15)  
**Severity:** High (production blocker)  
**Source:** [phase4_integration_review.md](../review/phase4_integration_review.md) (N3), Phase 3.1 review

## Summary

All feedback and integrated inference endpoints are unauthenticated. Any caller can submit feedback, trigger retraining criteria, or run comprehensive inference.

## Affected routes

- `POST /api/feedback/mark`
- `POST /api/feedback/mark-anomaly`
- `POST /api/feedback/record-outcome`
- `GET /api/feedback/statistics/{customer_id}`
- `GET /api/feedback/learning-progress/{customer_id}`
- `POST /api/inference/comprehensive`
- `POST /api/inference/batch`

## Fix (implemented)

1. `src/api/auth.py` — `require_api_key` dependency + `enforce_tenant_access`
2. `AUTH_ENABLED` config flag (default `false` in dev)
3. `TENANT_API_KEYS` env mapping (`tenant:key,...`)
4. All feedback + integrated inference routes protected when auth enabled

## Acceptance criteria

- [x] Unauthenticated requests return 401
- [x] Cross-tenant access returns 403
- [x] Integration tests with auth fixtures (`tests/api/test_auth.py`)
- [ ] Documented in deployment runbook
