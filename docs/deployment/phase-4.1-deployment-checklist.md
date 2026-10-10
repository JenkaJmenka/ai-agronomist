# Phase 4.1 — Production Deployment Checklist

**Last updated:** 2026-06-15  
**Status:** Ready for staging → production

---

## Pre-deployment gates

| Gate | Command / check | Target |
|------|-----------------|--------|
| Unit + integration tests | `venv/bin/python -m pytest tests/ -q --override-ini="addopts="` | All green (1029+ passed) |
| Phase 4.1 integration | `venv/bin/python -m pytest tests/integration/test_phase4_integration.py tests/integration/test_end_to_end.py -q --override-ini="addopts="` | 36 + 39 passed |
| Migrations | `venv/bin/alembic upgrade head` | Through `006` |
| Model checkpoint | `models/checkpoints/` or active row in `model_registry` | Loadable by `AnomalyDetector` |
| Auth (if enabled) | `AUTH_ENABLED=true`, `TENANT_API_KEYS` set | 401 without key, 403 cross-tenant |

---

## Performance benchmarks

| Metric | Target | How to verify |
|--------|--------|---------------|
| Comprehensive inference latency | < 50 ms p95 (CPU, warm cache) | `tests/models/test_ensemble_detector.py` latency smoke |
| pgvector similar search | < 100 ms (PostgreSQL) | `tests/integration/test_pgvector_incident_memory.py` |
| Feedback mark-anomaly | < 10 ms (no retrain) | API load test / manual curl timing |
| Batch inference | Linear in batch size | POST `/api/inference/batch` with 10 sequences |

---

## Before deployment

- [ ] Code review: [docs/review/phase4_integration_review.md](../review/phase4_integration_review.md) — PASS WITH NOTES
- [ ] Environment variables documented in `.env.example`
- [ ] `DATABASE_URL` points to production PostgreSQL + pgvector
- [ ] Backup strategy for DB and model checkpoints
- [ ] Logging level `INFO` in production (`LOG_LEVEL=INFO`)
- [ ] Health checks: `GET /health`, `GET /health/db`
- [ ] Optional auth configured for tenant isolation

---

## Deployment steps

1. **Staging**
   - [ ] Build image: `docker compose build api`
   - [ ] Run migrations on staging DB
   - [ ] Deploy API + Postgres (see [docs/DOCKER_SETUP.md](../DOCKER_SETUP.md))
   - [ ] Smoke test:
     ```bash
     curl -s http://staging:8000/health
     curl -X POST http://staging:8000/api/inference/comprehensive \
       -H "Content-Type: application/json" \
       -d @fixtures/comprehensive_request.json
     ```
   - [ ] Monitor 1 hour — error rate < 1%

2. **Production**
   - [ ] Tag release in git
   - [ ] Deploy same image/config as staging
   - [ ] Run alembic upgrade
   - [ ] Verify active model per tenant in model registry
   - [ ] Enable monitoring alerts (see [monitoring-dashboard-config.yaml](./monitoring-dashboard-config.yaml))
   - [ ] Monitor 24 hours

---

## Rollback plan

- [ ] Previous model checkpoint retained in `models/checkpoints/` and `model_registry`
- [ ] If API error rate > 5% for 15 min → rollback to previous image tag
- [ ] If FP rate > 20% (from `/api/monitoring/metrics`) → pause auto-retrain, rollback config via `CustomerConfigStore`
- [ ] Notify on-call; document incident in ops runbook log

---

## Post-deploy validation

- [ ] `POST /api/inference/comprehensive` returns all schema fields
- [ ] `POST /api/feedback/mark-anomaly` closes loop (incident resolved + metrics updated)
- [ ] `GET /api/monitoring/metrics` returns non-zero after sample traffic
- [ ] Operator UI: web's `/anomalies`, `/feedback`, `/metrics` pages load (Next.js; the Streamlit dashboard this item originally checked no longer deploys to production — see `docs/deployment/railway.md`)
- [ ] Operator can label incident end-to-end without curl

---

## Success criteria (Phase 4.1)

- [x] Full pipeline integrated (`comprehensive_pipeline.py`)
- [x] Inference returns all fields (`inference_integrated.py`)
- [x] Feedback triggers learning (`feedback_integrated.py`)
- [x] Metrics collected (`anomaly_metrics.py`, `/api/monitoring/metrics`)
- [x] 36 Phase 4.1 integration tests + 39 end-to-end tests (75 combined)
- [x] Deployment checklist (this document)
- [x] Ops runbook
- [x] Monitoring dashboard config

**PHASE 4.1 COMPLETE — READY FOR PRODUCTION DEPLOYMENT**
