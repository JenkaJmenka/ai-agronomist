# Integration — Notes

**Source:** phase4_integration_review.md

## NOTE-INT-001: _pipeline_cache never evicted

`inference_integrated.py` caches `ComprehensiveInferencePipeline` per checkpoint path indefinitely. Acceptable for Phase 1 single-worker; add LRU or TTL for multi-tenant production.

---

## NOTE-INT-002: learning_progress requires prior feedback

See NOTE-AL-005. Document in API schema / OpenAPI description.

---

## NOTE-INT-003: Dashboard metrics wired — RESOLVED

**Resolved 2026-06-15.** Streamlit Metrics page (`src/dashboard/pages/4_metrics.py`) calls `GET /api/monitoring/metrics` via `api_client.get_monitoring_metrics()`. Panel mapping and alert thresholds are documented in [monitoring-dashboard-config.yaml](../deployment/monitoring-dashboard-config.yaml).

Operator Feedback and Anomalies pages remain the primary surfaces for root cause and recommendations.

---

## NOTE-INT-004: Deployment docs — RESOLVED (load tests deferred)

**Resolved 2026-06-15** for checklist and runbook deliverables:

- [phase-4.1-deployment-checklist.md](../deployment/phase-4.1-deployment-checklist.md)
- [ops-runbook.md](../deployment/ops-runbook.md)
- [monitoring-dashboard-config.yaml](../deployment/monitoring-dashboard-config.yaml)
- Smoke fixture: `fixtures/comprehensive_request.json`

Automated load test suite remains out of scope for Phase 4.1 (manual benchmarks in checklist).
