# AI Agronomist

ML-powered greenhouse anomaly detection: unsupervised LSTM-VAE, semantic incident memory, operator-driven active learning, and online model adaptation.

**Release:** Phase 4.1 complete (June 2026) — **1029 tests passing**.  
**Release notes:** [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md)  
**Operator guide:** [docs/USER_GUIDE.md](docs/USER_GUIDE.md)  
**Deployment:** [docs/deployment/phase-4.1-deployment-checklist.md](docs/deployment/phase-4.1-deployment-checklist.md)

---

## What you can do today

| Goal | How |
|------|-----|
| **Ingest sensor data** | Upload CSV via API or use synthetic training data |
| **Detect anomalies** | Score 20-step sensor sequences (temp, humidity, CO₂, light) |
| **Full diagnosis pipeline** | Pattern type, root cause, confidence, recommendations in one call |
| **Remember past incidents** | Store embeddings; search similar cases; suggest fixes from history |
| **Close the loop with operators** | Label incidents in Streamlit or via API; real retrain with promote/rollback |
| **Ensemble detection (Gap 4)** | Four detectors with weighted vote and agreement in comprehensive API |
| **Explore visually** | Streamlit dashboard: anomalies, operator feedback, pipeline metrics |
| **Monitor pipeline health** | Metrics API + Streamlit Metrics page (detections, FP rate, pattern distribution) |
| **Deploy securely** | Optional API-key auth; PostgreSQL + pgvector; ops runbook + deployment checklist |

---

## Quick start (5 minutes)

```bash
cd ai-agronomist
source venv/bin/activate          # Python 3.11 required
pip install -r requirements.txt

export DATABASE_URL=sqlite+aiosqlite:///./greenhouse.db
venv/bin/alembic upgrade head

# Terminal 1 — API
venv/bin/python -m uvicorn src.api.main:app --reload

# Terminal 2 — Dashboard (optional)
venv/bin/python -m streamlit run src/dashboard/app.py
```

- **API docs:** http://127.0.0.1:8000/docs  
- **Dashboard:** http://localhost:8501  

Train a model if you don't have a checkpoint:

```bash
venv/bin/python scripts/train_lstm_vae.py
```

Run tests:

```bash
venv/bin/python -m pytest tests/ -q --override-ini="addopts="
```

---

## Typical workflows

### 1. Detect and diagnose an anomaly

Send one 20×4 sensor sequence to the **comprehensive pipeline** — detection, attribution, pattern analysis, root cause, and operator recommendations in a single response:

```bash
curl -s -X POST http://127.0.0.1:8000/api/inference/comprehensive \
  -H "Content-Type: application/json" \
  -d @fixtures/comprehensive_request.json
```

Returns: `is_anomaly`, `anomaly_score`, `confidence`, `pattern_type`, `root_cause`, `immediate_actions`, `incident_id`, `learning_progress`, and more.

Batch mode: `POST /api/inference/batch`

### 2. Operator feedback → active learning

When an operator confirms or rejects a detection, feedback is stored and can trigger retraining when enough high-quality labels accumulate:

```bash
curl -s -X POST http://127.0.0.1:8000/api/feedback/mark-anomaly \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "demo_tenant",
    "incident_id": "<from comprehensive response>",
    "is_true_anomaly": true,
    "operator_id": "alice"
  }'
```

Check progress: `GET /api/feedback/learning-progress/{customer_id}`  
Statistics: `GET /api/feedback/statistics/{customer_id}`

Learning state **persists across API restarts** (SQLite/Postgres `learning_states` table).

### 3. Incident memory — learn from history

Store incidents with latent embeddings, find similar past cases, and mine fix suggestions from resolved history:

| Endpoint | Purpose |
|----------|---------|
| `POST /api/incidents/store` | Save anomaly + embedding |
| `GET /api/incidents/similar` | k-nearest semantic search |
| `GET /api/incidents/suggest-fixes` | Root cause + actions from similar resolved cases |
| `POST /api/incidents/resolve/{id}` | Close incident (409 if already resolved) |
| `GET /api/incidents/statistics` | Tenant analytics |
| `GET /api/incidents/recurring-issues` | Repeated root causes |

### 4. Online learning (continuous adaptation)

Feed normal sequences to update the tenant model incrementally:

```bash
curl -s -X POST http://127.0.0.1:8000/api/learning/observe \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "demo_tenant", "greenhouse_id": "gh_1", "sequences": [[...]]}'
```

Also: `POST /api/learning/predict`, `GET /api/learning/statistics`, `GET /api/learning/drift`

### 5. Monitor detections

```bash
curl -s "http://127.0.0.1:8000/api/monitoring/metrics?hours=24"
```

Returns detection counts, false-positive rate, average confidence, true/false positive tallies, pattern and confidence distributions. The Streamlit **Metrics** page reads this endpoint live.

### 6. Production deployment

See [docs/deployment/phase-4.1-deployment-checklist.md](docs/deployment/phase-4.1-deployment-checklist.md) and [docs/deployment/ops-runbook.md](docs/deployment/ops-runbook.md).

```bash
# PostgreSQL + pgvector
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://postgres:postgres_dev_password@localhost:5432/greenhouse_monitoring
venv/bin/alembic upgrade head

# Enable API key auth
export AUTH_ENABLED=true
export TENANT_API_KEYS=demo_tenant:your_secret_key
# All requests need: -H "X-API-Key: your_secret_key"
```

See [docs/DOCKER_SETUP.md](docs/DOCKER_SETUP.md) for ivfflat index maintenance after loading incident data.

---

## Architecture overview

```
Sensor CSV / API          LSTM-VAE              Diagnosis stack           Memory & learning
     │                       │                        │                         │
     ▼                       ▼                        ▼                         ▼
 POST /upload          POST /inference      POST /inference/comprehensive   /api/incidents/*
                       (scores + embed)     (full pipeline)                 /api/feedback/*
                                            attribution · patterns ·         /api/learning/*
                                            root cause · recommendations     /api/monitoring/*
```

**ML (`src/models/`)** — LSTM-VAE detector, pattern detector (7 types), anomaly attributor, confidence scorer, root-cause analyzer, recommendation engine, active learner with auto-retrain orchestrator.

**API (`src/api/`)** — FastAPI, async SQLAlchemy, multi-tenant by `tenant_id` / `customer_id`.

**Database (`src/db/`)** — SQLite (dev) or PostgreSQL + pgvector (prod). Migrations `001`–`006`: sensor readings, incidents, learning logs, model registry, learning state, customer model config.

**Dashboard (`src/dashboard/`)** — Streamlit UI: sensor charts, highlighted anomaly table, operator feedback queue, pipeline metrics from `/api/monitoring/metrics`.

---

## API reference (all route groups)

| Prefix | Description |
|--------|-------------|
| `/health`, `/health/db` | Liveness and database check |
| `/api/upload` | CSV sensor ingestion |
| `/api/inference` | Batch inference + embeddings |
| `/api/inference/comprehensive`, `/batch` | Full diagnosis pipeline |
| `/api/incidents/*` | Incident memory |
| `/api/learning/*` | Online learning + drift |
| `/api/feedback/*` | Operator feedback + active learning |
| `/api/models/*` | Per-customer model registry |
| `/api/monitoring/metrics` | Pipeline observability |

Interactive docs: http://127.0.0.1:8000/docs

---

## Documentation index

| Doc | Contents |
|-----|----------|
| [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md) | Release history (Operator Feedback, Phase 4.1) |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Setup, migrations, curl examples, dashboard walkthrough |
| [docs/USER_GUIDE_VISUAL.md](docs/USER_GUIDE_VISUAL.md) | Illustrated dashboard guide with screenshots |
| [docs/deployment/phase-4.1-deployment-checklist.md](docs/deployment/phase-4.1-deployment-checklist.md) | Staging/production gates |
| [docs/deployment/ops-runbook.md](docs/deployment/ops-runbook.md) | On-call procedures |
| [docs/deployment/monitoring-dashboard-config.yaml](docs/deployment/monitoring-dashboard-config.yaml) | Metrics + alert thresholds |
| [docs/DOCKER_SETUP.md](docs/DOCKER_SETUP.md) | Docker stack, pgvector, REINDEX |
| [docs/incident_memory_schema.md](docs/incident_memory_schema.md) | Incident DB schema |
| [docs/bugs/README.md](docs/bugs/README.md) | Resolved issues (all closed) |
| [docs/plans/fix-deferred-items-plan.md](docs/plans/fix-deferred-items-plan.md) | Fix-plan completion record |

---

## What's next (roadmap)

Phase 1.9.1 and Phase 4.1 are **complete**. Future work (not started):

- Prometheus/Grafana export for pipeline metrics
- JWT auth, rate limiting, Redis cache for multi-worker deployments
- `_pipeline_cache` LRU eviction (NOTE-INT-001)
- Automated load test suite
