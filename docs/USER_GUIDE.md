# AI Agronomist — Operator User Guide

Practical guide for running and exercising the platform.

**Status (2026-06-18):** Dashboard UX improvements complete — **132 dashboard + API tests passing**.  
**Visual walkthrough (screenshots):** [USER_GUIDE_VISUAL.md](USER_GUIDE_VISUAL.md)  
See [RELEASE_NOTES.md](RELEASE_NOTES.md) for what's new and [README.md](../README.md) for workflow index.  
Operator Streamlit UI (Anomalies, Operator Feedback, Metrics), ensemble detection, promote/rollback retrain, deployment checklist + ops runbook.

---

## 1. Prerequisites

- **Python 3.11** with project venv (`venv/bin/python`) — system Python 3.14 lacks torch/scipy.
- **Model checkpoint** for live inference/learning routes (train or use existing under `models/checkpoints/`).
- **Docker** (optional) for PostgreSQL + pgvector — SQLite works for dev/tests.

```bash
cd /Users/eugenesidorov/Projects/ai-agronomist
source venv/bin/activate   # or: venv/bin/python ...
pip install -r requirements.txt   # if venv is fresh
```

---

## 2. Quick start

### Database migrations

```bash
# SQLite (default dev)
export DATABASE_URL=sqlite+aiosqlite:///./greenhouse.db
venv/bin/alembic upgrade head
```

Alembic chain: `001_initial` → `002` (incidents) → `003` (learning_logs) → `004` (model registry) → `005` (learning state) → `006` (customer_model_config).

### Start API

```bash
venv/bin/python -m uvicorn src.api.main:app --reload --host 127.0.0.1 --port 8000
```

Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

Registered route groups: `/health`, `/api/upload`, `/api/inference`, `/api/inference/comprehensive`,
`/api/incidents`, `/api/learning`, `/api/feedback`, `/api/monitoring`, `/api/models`.

### Start dashboard (optional)

```bash
venv/bin/python -m streamlit run src/dashboard/app.py
```

Opens at [http://localhost:8501](http://localhost:8501).

**Pages:** Overview · Sensor Data · Anomalies · Metrics · Operator Feedback.

If API auth is enabled, enter your tenant API key in the sidebar (`X-API-Key`).

### Sensor Data page (page 1)

Displays all four sensor channels over the selected date range. A **Sync Graphs** toggle in the sidebar switches between:

- **Off (default)** — four independent Plotly charts, each with its own zoom/pan.
- **On** — a single 2×2 subplot grid with a shared x-axis. Zooming or panning any panel instantly updates all others. Y-axes are locked to horizontal drag only so vertical scale stays fixed.

Layout and chart size are the same in both modes.

### Anomalies page (page 2)

Shows the top-20 anomalies by score for the selected date range.

**Anomaly table columns:** Timestamp · Score · Pattern · Sensors · Temp · Humidity · CO₂ · Light · Action

- Sensor cells with highlighted backgrounds (red = range violation, orange = strong attribution ≥ 0.25, amber = contributing sensor) show a **hover tooltip card** with the pattern type, affected sensors, and a human-readable description, e.g. *"Unexpected temperature spike: 36.2 °C detected, 6.2 °C above normal (20–30 °C) — part of spike pattern affecting temperature, co2"*.
- **Pattern** is derived from matched incident diagnosis; when no incident is matched (most common when inference has not been run), it falls back to a heuristic from sensor range violations: `spike` / `drop` / `deviation`.
- **Action column** — every row has ✓ (confirm) and ✗ (reject) buttons:
  - Row has a matched incident → button submits feedback directly.
  - Row has no matched incident → button first auto-registers the anomaly via `POST /api/inference/comprehensive` (requires API server), then submits feedback. A spinner is shown during registration.
  - After a button is clicked the cell shows **✓** or **✗** for the remainder of the session.

### Metrics page (page 4)

Calls `GET /api/monitoring/metrics` and shows:

- Total detections, false-positive rate, true/false positive counts
- Average confidence
- Pattern and confidence distributions

If the API is unreachable the page shows a caption with a link to the [ops runbook](deployment/ops-runbook.md). Sensor coverage from the local DB is shown as a secondary panel.

### Operator Feedback page (page 6)

Two workflows depending on whether open incidents exist:

**No incidents yet — Pending anomalies view**

Shows a table of sensor readings above the threshold that have not been registered as incidents. Columns: timestamp (formatted), score, temp, humidity, CO₂, light (all rounded). Click **Register all for review** to run comprehensive inference on each anomaly and create open incidents. The page reloads into the review queue view.

**Incidents exist — Review queue view**

| Column | Content |
|--------|---------|
| (index) | 1-based row number |
| detected | Formatted detection time (`08 Jun 12:17`) |
| score | Anomaly score (3 decimals) |
| pattern | Humanized pattern from `root_cause_details` (e.g. `Spike`, `Deviation`) |
| root cause | Humanized root cause (e.g. `Unknown anomaly`, `Ventilation failure`) |
| description | Stored hint summary or model reasoning (truncated to 120 chars) |
| sensor type | Affected sensor names |
| sensor value | Sensor readings from the last step of the stored sequence |

The **Select incident** dropdown uses the same 1-based numbers as the table. Selecting an incident shows:

- Sequence chart (4-sensor time series at detection)
- Diagnosis card (pattern, attribution progress bars, detector scores, root cause)
- Learning progress panel
- Label form — submit **True anomaly** or **False positive** with optional pattern/root-cause overrides and operator confidence

Equivalent curl for feedback:

```bash
curl -X POST http://127.0.0.1:8000/api/feedback/mark-anomaly \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{"customer_id":"tenant_1","incident_id":"INCIDENT_UUID","is_true_anomaly":false,"operator_id":"alice"}'
```

### Train a model (if no checkpoint)

```bash
venv/bin/python scripts/train_lstm_vae.py
```

Checkpoints land in `models/checkpoints/`. Without a loadable checkpoint, `/api/inference`
and `/api/learning/*` return **503**.

---

## 3. Run tests

### Targeted suites (fast, no shutdown hang)

```bash
venv/bin/python -m pytest tests/models/test_anomaly_embedding.py -q --no-cov
venv/bin/python -m pytest tests/db/test_incident_memory.py tests/api/test_incident_api.py -q --no-cov
venv/bin/python -m pytest tests/models/test_replay_buffer.py tests/models/test_online_learner.py \
  tests/db/test_learning_logs.py tests/api/test_learning_api.py -q --no-cov
```

### Full suite

```bash
venv/bin/python -m pytest tests/ -q --override-ini="addopts="
```

Expected: **1029 passed**, 1 skipped, 1 xfailed.

Phase 4.1 integration only:

```bash
venv/bin/python -m pytest tests/integration/test_phase4_integration.py \
  tests/integration/test_end_to_end.py -q --override-ini="addopts="
```

Expected: **75 passed** (36 + 39).

**Known issue:** pytest may hang at interpreter shutdown **after** printing results (~55s).
Press Ctrl+C (SIGINT) once the summary line appears — coverage totals are still valid.

---

## 4. API walkthrough

All examples use `tenant_id=demo_tenant`, `greenhouse_id=gh_1`.
Sequences are **20 timesteps × 4 features** `[temperature, humidity, co2, light]`.

### 4.1 Inference (Gap 1 — embeddings)

```bash
curl -s -X POST http://127.0.0.1:8000/api/inference \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "demo_tenant",
    "greenhouse_id": "gh_1",
    "sequences": [[
      [24.0, 60.0, 400.0, 800.0],
      [24.1, 60.5, 401.0, 810.0],
      [24.2, 61.0, 402.0, 820.0],
      [24.3, 61.5, 403.0, 830.0],
      [24.4, 62.0, 404.0, 840.0],
      [24.5, 62.5, 405.0, 850.0],
      [24.6, 63.0, 406.0, 860.0],
      [24.7, 63.5, 407.0, 870.0],
      [24.8, 64.0, 408.0, 880.0],
      [24.9, 64.5, 409.0, 890.0],
      [25.0, 65.0, 410.0, 900.0],
      [25.1, 65.5, 411.0, 910.0],
      [25.2, 66.0, 412.0, 920.0],
      [25.3, 66.5, 413.0, 930.0],
      [25.4, 67.0, 414.0, 940.0],
      [25.5, 67.5, 415.0, 950.0],
      [25.6, 68.0, 416.0, 960.0],
      [25.7, 68.5, 417.0, 970.0],
      [25.8, 69.0, 418.0, 980.0],
      [25.9, 69.5, 419.0, 990.0]
    ]]
  }' | python3 -m json.tool
```

Response includes `anomaly_scores`, `embeddings` (8-dim), `uncertainties`, `embedding_dim`.

### 4.2 Incident memory (Gap 2)

**Store** an incident (use embedding from inference response):

```bash
curl -s -X POST http://127.0.0.1:8000/api/incidents/store \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "demo_tenant",
    "greenhouse_id": "gh_1",
    "embedding": [1,0,0,0,0,0,0,0],
    "anomaly_score": 0.85,
    "sequence": [[24,60,400,800],[24.1,60.5,401,810],[24.2,61,402,820],[24.3,61.5,403,830],
      [24.4,62,404,840],[24.5,62.5,405,850],[24.6,63,406,860],[24.7,63.5,407,870],
      [24.8,64,408,880],[24.9,64.5,409,890],[25,65,410,900],[25.1,65.5,411,910],
      [25.2,66,412,920],[25.3,66.5,413,930],[25.4,67,414,940],[25.5,67.5,415,950],
      [25.6,68,416,960],[25.7,68.5,417,970],[25.8,69,418,980],[25.9,69.5,419,990]],
    "embedding_uncertainty": 0.12,
    "tags": [],
    "notes": "Manual test incident"
  }'
```

**Find similar** (repeat `embedding` query param 8 times for LATENT_DIM=8):

```bash
curl -s "http://127.0.0.1:8000/api/incidents/similar?tenant_id=demo_tenant&k=5\
&embedding=1&embedding=0&embedding=0&embedding=0&embedding=0&embedding=0&embedding=0&embedding=0"
```

**Suggest fixes**, **statistics**, **recurring issues** — see Swagger `/docs` under `incidents`.

Errors on 404 use `{"message": "..."}` (not FastAPI's default `detail`).

### 4.3 Online learning (Gap 3)

**Observe** a batch (feeds tenant learner + drift detector + audit log):

```bash
curl -s -X POST http://127.0.0.1:8000/api/learning/observe \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "demo_tenant",
    "greenhouse_id": "gh_1",
    "sequences": [[
      [24.0, 60.0, 400.0, 800.0],[24.1, 60.5, 401.0, 810.0],[24.2, 61.0, 402.0, 820.0],
      [24.3, 61.5, 403.0, 830.0],[24.4, 62.0, 404.0, 840.0],[24.5, 62.5, 405.0, 850.0],
      [24.6, 63.0, 406.0, 860.0],[24.7, 63.5, 407.0, 870.0],[24.8, 64.0, 408.0, 880.0],
      [24.9, 64.5, 409.0, 890.0],[25.0, 65.0, 410.0, 900.0],[25.1, 65.5, 411.0, 910.0],
      [25.2, 66.0, 412.0, 920.0],[25.3, 66.5, 413.0, 930.0],[25.4, 67.0, 414.0, 940.0],
      [25.5, 67.5, 415.0, 950.0],[25.6, 68.0, 416.0, 960.0],[25.7, 68.5, 417.0, 970.0],
      [25.8, 69.0, 418.0, 980.0],[25.9, 69.5, 419.0, 990.0]
    ]]
  }'
```

**Predict** (single sequence), **statistics**, **history**, **drift** — Swagger tag `learning`.

Requires loaded checkpoint (same as inference). Learner state is **in-memory** (lost on API restart).

### 4.4 Comprehensive inference + feedback (Phase 4.1)

**Smoke test** with the deployment fixture:

```bash
curl -s -X POST http://127.0.0.1:8000/api/inference/comprehensive \
  -H "Content-Type: application/json" \
  -d @fixtures/comprehensive_request.json | python3 -m json.tool
```

Returns full diagnosis: `pattern_type`, `root_cause`, `immediate_actions`, `incident_id`, `detector_scores`, `ensemble_score`, etc.

**Operator feedback** on the returned `incident_id`:

```bash
curl -s -X POST http://127.0.0.1:8000/api/feedback/mark-anomaly \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "tenant_1",
    "incident_id": "<from response>",
    "is_true_anomaly": true,
    "operator_id": "alice"
  }'
```

**Pipeline metrics:**

```bash
curl -s "http://127.0.0.1:8000/api/monitoring/metrics?hours=24" | python3 -m json.tool
```

See [deployment/phase-4.1-deployment-checklist.md](deployment/phase-4.1-deployment-checklist.md) and [deployment/monitoring-dashboard-config.yaml](deployment/monitoring-dashboard-config.yaml).

---

## 5. Docker Postgres + pgvector

See [DOCKER_SETUP.md](DOCKER_SETUP.md) for full stack (Jupyter, TensorBoard, Adminer).

```bash
docker compose up -d postgres
docker compose exec postgres psql -U postgres -d greenhouse_monitoring -c "SELECT 1;"
```

**First-time cluster:** `docker/initdb/01-enable-pgvector.sql` runs automatically.

**Existing `postgres_data` volume:** extension may be missing. Either:

```bash
docker compose down -v   # destroys data, re-inits with pgvector
```

or manually:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Point the app at Postgres:

```bash
export DATABASE_URL=postgresql+asyncpg://postgres:postgres_dev_password@localhost:5432/greenhouse_monitoring
venv/bin/alembic upgrade head
```

Add this as a **comment** in `.env.example` (manual edit — agents cannot touch `.env*`):

```
# DATABASE_URL=postgresql+asyncpg://postgres:postgres_dev_password@localhost:5432/greenhouse_monitoring
```

---

## 6. Deployment (staging / production)

Follow [deployment/phase-4.1-deployment-checklist.md](deployment/phase-4.1-deployment-checklist.md):

1. Run full test gate (`1029+` passed)
2. `venv/bin/alembic upgrade head` (through migration `006`)
3. Smoke: `POST /api/inference/comprehensive` with `fixtures/comprehensive_request.json`
4. Label via Operator Feedback or `POST /api/feedback/mark-anomaly`
5. Confirm Metrics page shows live `/api/monitoring/metrics` data

On-call procedures: [deployment/ops-runbook.md](deployment/ops-runbook.md).

---

## 7. Manual checklist

| Task | Why |
|------|-----|
| Add pgvector `DATABASE_URL` comment to `.env.example` | Documents Postgres path for incident memory |
| `git remote add origin <url>` + push | Most of repo still uncommitted locally |
| Commit Phase 4.1 + operator feedback changes | Clean history before staging deploy |

---

## 8. Next steps (post Phase 4.1)

Phase 1.9.1 and Phase 4.1 are **complete**. Suggested follow-ups:

- Staging deploy per deployment checklist
- Prometheus export for in-process metrics (config documents future hook)
- Multi-worker: LRU for `_pipeline_cache`, Redis orchestrator store
- Automated load tests

---

## 9. Known gotchas

| Topic | Detail |
|-------|--------|
| HTTP errors | Custom handler returns `{"message": ...}` on 404/500; 422 keeps `detail` too |
| VAE scores | Stochastic in eval mode — never expect exact score equality between runs |
| pytest shutdown | Full suite hangs after results — SIGINT is OK |
| Learning routes | 503 without model checkpoint |
| Cross-gap flow | Comprehensive inference auto-stores incidents; feedback closes the learning loop |
| Operator Feedback empty | Anomalies from upload are sensor readings — register via comprehensive inference or use review queue bridge |
| CSV upload format | Columns: `timestamp,temperature,humidity,co2,light` (see synthetic data under `data/synthetic_customers/`) |
| `init_db` | Startup does not create incident/learning tables — use alembic |
| Tenant scope | Always pass `tenant_id`; all memory/learning queries are tenant-isolated |
| Action buttons on page 2 | Buttons require the API server to be running for rows without a pre-matched incident. If the API is unreachable, a warning is shown and no state changes |
| Incident timestamp mismatch | Incidents are created with `detected_at = now()` (server time), not the historical reading timestamp. The ±15 min matching window in `_nearest_incident` will not match historical seeded data unless inference runs close in time to the readings |
| `unknown_anomaly` pattern | The root cause analyzer falls back to `unknown_anomaly` when confidence is low. This is expected with synthetic/seeded data that lacks the multi-sensor correlation patterns the model uses for specific causes |

---

## 10. Design docs

- Gap 1: [embeddings_architecture.md](embeddings_architecture.md)
- Gap 2: [incident_memory_schema.md](incident_memory_schema.md)
- Gap 3: [online_learning_design.md](online_learning_design.md)
- Reviews: [docs/review/](review/)
