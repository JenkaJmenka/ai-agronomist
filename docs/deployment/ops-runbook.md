# AI Agronomist — Operations Runbook

**Audience:** On-call engineers, greenhouse IT  
**Pipeline version:** 1.0 (Phase 4.1)

---

## Architecture overview

```
Sensor CSV / API → Upload → sensor_readings
                         ↓
              POST /api/inference/comprehensive
                         ↓
    Detection → Attribution → Pattern → Confidence
         → Root Cause → Recommendations → Incident store
                         ↓
              Operator POST /api/feedback/mark-anomaly
                         ↓
              Active learning → retrain (promote/rollback)
```

---

## Service endpoints

| Service | Default URL | Health |
|---------|-------------|--------|
| FastAPI | `http://localhost:8000` | `GET /health` |
| Streamlit dashboard | `http://localhost:8501` | Page load |
| PostgreSQL | `DATABASE_URL` | `GET /health/db` |

### Key API routes

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/upload` | Ingest CSV sensor readings |
| POST | `/api/inference/comprehensive` | Full anomaly pipeline |
| POST | `/api/inference/batch` | Batch comprehensive |
| POST | `/api/feedback/mark-anomaly` | Operator label → learning |
| GET | `/api/monitoring/metrics` | FP rate, confidence, patterns |
| GET | `/api/incidents/open` | Operator review queue |

---

## Common operations

### Start stack (development)

```bash
export DATABASE_URL=sqlite+aiosqlite:///./greenhouse.db
venv/bin/alembic upgrade head
venv/bin/python -m uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
venv/bin/python -m streamlit run src/dashboard/app.py
```

### Run comprehensive inference (curl)

```bash
curl -X POST http://127.0.0.1:8000/api/inference/comprehensive \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{
    "customer_id": "tenant_1",
    "greenhouse_id": "gh_A",
    "sequence": [[25,65,400,500], ... 20 rows total ...]
  }'
```

### Operator feedback

```bash
curl -X POST http://127.0.0.1:8000/api/feedback/mark-anomaly \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{
    "customer_id": "tenant_1",
    "incident_id": "UUID",
    "is_true_anomaly": false,
    "operator_id": "alice"
  }'
```

### Check metrics

```bash
curl "http://127.0.0.1:8000/api/monitoring/metrics?hours=24"
```

---

## Incident response

### API returns 503 on inference

**Cause:** No loadable model checkpoint.  
**Fix:** Train or register model:
```bash
venv/bin/python scripts/train_lstm_vae.py
# or POST /api/models/{tenant_id}/train
```

### High false positive rate (> 20%)

1. Check `GET /api/monitoring/metrics`
2. Review operator labels in last 24h
3. If retrain recently promoted bad config → rollback via `CustomerConfigStore` snapshot
4. Temporarily raise `ANOMALY_THRESHOLD` in customer config

### Operator Feedback queue empty but Anomalies page shows data

**Cause:** Sensor readings ≠ incidents.  
**Fix:** Dashboard → Operator Feedback → **Register all for review**, or run comprehensive inference per anomaly.

### Database migration failure

```bash
venv/bin/alembic current
venv/bin/alembic upgrade head
```

For PostgreSQL + pgvector: ensure extension enabled (`docker/initdb/01-enable-pgvector.sql`).

### Auth 401 / 403

- 401: missing/invalid `X-API-Key` when `AUTH_ENABLED=true`
- 403: `customer_id` on request ≠ key tenant — use matching tenant

---

## Monitoring & alerts

See [monitoring-dashboard-config.yaml](./monitoring-dashboard-config.yaml).

| Alert | Condition | Action |
|-------|-----------|--------|
| High FP rate | `false_positive_rate > 0.20` | Review labels, consider rollback |
| Slow inference | p95 latency > 100 ms | Check model cache, CPU load |
| No feedback | 0 TP/FP in 7 days | Prompt operators via dashboard |
| API down | `/health` non-200 | Restart container, check logs |

Logs: `logs/app.log` (if configured) or container stdout.

---

## Backup & restore

- **Database:** nightly pg_dump / SQLite file copy
- **Models:** sync `models/checkpoints/` and `model_registry` table
- **Learning state:** `learning_states`, `customer_model_config` tables

---

## Escalation

1. Check this runbook + [USER_GUIDE.md](../USER_GUIDE.md)
2. Review [phase4_integration_review.md](../review/phase4_integration_review.md)
3. Roll back deployment per [phase-4.1-deployment-checklist.md](./phase-4.1-deployment-checklist.md)
