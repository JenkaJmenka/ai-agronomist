# AI Agronomist

Multi-layer anomaly detection for greenhouse SCADA telemetry: deterministic rule
detectors, crop setpoint bands, an unsupervised LSTM-VAE trained per tenant,
semantic incident memory, and operator-driven active learning.

**📖 [User Guide](docs/USER_GUIDE.md)** — every page and screen, upload and training workflows
**🔍 [Anomaly Catalog](docs/ANOMALY_CATALOG.md)** — every anomaly type, how it's detected, what to do
**📐 [Framework spec](docs/anomaly_framework_spec.md)** — detector formulas and verification datasets

---

## What it detects

**30+ anomaly types** across five families:

| Family | Count | Examples |
|--------|-------|----------|
| **Physical sensor faults** | 9 | Spike, out-of-bounds, stuck/flatline, calibration drift, noise, bias offset, correlation breakdown, thermodynamic contradiction, diurnal deviation |
| **Crop setpoint breaches** | 8 | Temperature / humidity / VPD / CO₂, each high and low, day and night |
| **SCADA climate faults** | 6 | Under-heating, over-heating, dehumidification failure, saturation risk, thermal divergence, polling dropout |
| **Gas anomalies** | 6 channels | Ethylene, NO, NO₂, NOₓ, CO, gas CO₂ |
| **OT network anomalies** | 6 | RTT/jitter, IAT CV, TCP handshake, DHCP fingerprint, service latency, Isolation Forest threat score |

Plus multivariate implausibility (Mahalanobis), a learned LSTM-VAE detector, and
seven pattern classifications (drift, spike, dependency, lag, saturation, cyclic,
cascade).

Full descriptions in the **[Anomaly Catalog](docs/ANOMALY_CATALOG.md)**.

---

## Quick start

```bash
# 1. Backend services
docker compose up -d          # Postgres, RabbitMQ, Redis, API, worker

# 2. Frontend
cd web
corepack pnpm install
API_URL=http://localhost:8000 corepack pnpm dev
```

| Service | URL |
|---------|-----|
| **Operator UI (Next.js)** | http://localhost:3000 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| RabbitMQ management | http://localhost:15672 |
| Streamlit dashboard (legacy) | http://localhost:8501 |

> Agent/editor MCP tooling: `cp .mcp.example.json .mcp.json` — the local copy is
> gitignored so per-machine tweaks never land in a commit.

### Local development without Docker

```bash
python3 -m venv venv && source venv/bin/activate   # Python 3.11
pip install -r requirements.txt
pip install -r requirements-dev.txt           # locust load tests in tests/load/

export DATABASE_URL=sqlite+aiosqlite:///./greenhouse.db
alembic upgrade head

python -m uvicorn src.api.main:app --reload   # terminal 1
python -m src.workers.main                    # terminal 2 — required for uploads
```

> The worker is **not optional** for uploads that persist or train. The API
> accepts and queues them regardless; without a worker they sit in `preparing`
> forever.

### Tests

```bash
python -m pytest tests/ -q --override-ini="addopts="   # backend
cd web && corepack pnpm test                            # frontend unit
cd web && corepack pnpm exec playwright test            # e2e (needs a build)
```

---

## First run

1. **Configuration** — pick your crop, set the day/night window (**UTC**), set
   the CO₂ enrichment flag, review setpoint bands. Do this *before* uploading.
2. **Upload & Operations** — upload a CSV with *Ingest data + train model*.
3. **Job Monitoring** — wait for `completed`. Cold-start training takes minutes.
4. **Thresholds** — tune the cutoff against the score distribution.
5. **Anomaly Detection** — review findings.
6. **Operator Feedback** — label incidents to drive retraining.

Full walkthrough: **[User Guide § First-run checklist](docs/USER_GUIDE.md#19-first-run-checklist)**.

---

## Architecture

```
                    ┌──────────────────┐
  Browser ─────────►│  Next.js (web/)  │  URL-driven state, BFF proxy
                    │  localhost:3000  │  injects X-API-Key server-side
                    └────────┬─────────┘
                             │ /api/bff/*
                    ┌────────▼─────────┐
                    │  FastAPI (src/)  │  async SQLAlchemy, multi-tenant
                    │  localhost:8000  │
                    └───┬──────────┬───┘
                        │          │
              ┌─────────▼──┐  ┌────▼──────────┐
              │ PostgreSQL │  │   RabbitMQ    │
              │ + pgvector │  │  upload.*     │
              └────────────┘  │  retrain.*    │
                              └────┬──────────┘
                                   │
                            ┌──────▼───────┐
                            │    Worker    │  portioned CSV processing,
                            │ (src/workers)│  cold-start training, retrain
                            └──────────────┘
```

### Code layout

| Path | Contents |
|------|----------|
| `src/models/` | Detectors, LSTM-VAE, pattern/root-cause/recommendation stack, active learning |
| `src/models/network/` | OT network detectors (RTT, IAT, TCP, DHCP, service, Isolation Forest) |
| `src/api/routes/` | FastAPI route groups |
| `src/api/services/` | Comprehensive pipeline, upload orchestration, cold-start training |
| `src/live/` | Live SSE streaming, sources (mock, Priva, Hoogendoorn, fertigation) |
| `src/workers/` | RabbitMQ consumers: upload prepare, upload, training, retrain |
| `src/db/` | Models, migrations, incident memory, vector search |
| `src/config.py` | **Every** tunable constant — no magic numbers elsewhere |
| `web/app/` | Next.js App Router pages + BFF proxy |
| `web/features/` | One folder per page domain |
| `alembic/versions/` | **Authoritative** migrations |

### Detection pipeline

```
CSV / live tick
      │
      ├─► Short-circuit gates ──► polling_dropout · out_of_bounds
      │                            (no data / impossible value → stop)
      │
      ├─► Rule detectors (vote) ─► spike_mad · noise · bias_dual · correlation
      │                            diurnal · multivariate · change_point
      │                            SCADA heating/humidity · band breaches
      │
      ├─► LSTM-VAE ─────────────► reconstruction error + 8-dim latent embedding
      │
      ├─► Ensemble vote ────────► lstm_vae · isolation_forest · statistical
      │                            · correlation  →  agreement count
      │
      └─► Diagnosis ────────────► pattern type · root cause · confidence
                                  · recommendations · incident memory
```

Rule detectors **vote** rather than short-circuit: the LSTM/ensemble path always
runs, and the first-fired rule takes fault-type precedence. Details:
[Anomaly Catalog § Precedence](docs/ANOMALY_CATALOG.md#precedence).

---

## Operator UI pages

| Page | Purpose |
|------|---------|
| **Monitoring** | All channels, sensor tiles, live SSE streaming, anomaly bands |
| **Climate Data** | Temperature / humidity only |
| **Gas Monitoring** | Gas channels (exact match — never climate CO₂) |
| **Anomaly Detection** | Score timeline, top/all anomalies, server- or client-side grouping |
| **Thresholds** | Preview flagged-count impact before committing a cutoff |
| **System Metrics** | Detection counters, FP rate, pattern distribution |
| **Configuration** | Crop profile, day/night window, CO₂ flag, setpoint bands, season map |
| **Upload & Operations** | CSV upload, model registry, synthetic anomalies, data wipes |
| **Job Monitoring** | Background job progress (SSE with polling fallback) |
| **Operator Feedback** | Review queue, verdict labelling, retrain progress |

Detailed walkthroughs: **[User Guide § Page by page](docs/USER_GUIDE.md#6-home)**.

### Frontend conventions

- **URL is the state** — tenant, greenhouse, window, threshold, channel and live
  mode all live in the query string. Reload reproduces the exact view.
- **Two-step confirm** on every destructive or committing action.
- **Zod validation** at every BFF response boundary.
- **Job status is never cached** (`staleTime: 0`) — SSE first, polling fallback.
- Loading, error and explicit empty states on every data component.

---

## API reference

| Prefix | Purpose |
|--------|---------|
| `/health`, `/health/db` | Liveness and database check |
| `/api/upload` | CSV ingestion, synthetic anomaly injection, data wipes |
| `/api/inference/upload` | Synchronous analyze-only upload |
| `/api/inference/upload-async` | Queued persist/train upload (202) |
| `/api/inference/comprehensive` | Full diagnosis pipeline for one sequence |
| `/api/readings/*` | Windowed readings, bounds, greenhouses, raw rows |
| `/api/anomalies/enriched`, `/groups` | Enriched rows and server-side grouping |
| `/api/jobs/*` | Job status, SSE events, cancel |
| `/api/live/*` | Live source status, resume, SSE event stream |
| `/api/models/*` | Per-tenant model registry, train, activate, rollback |
| `/api/config/*` | Operational config (crop, setpoints, seasons, CO₂ flag) |
| `/api/feedback/*` | Operator verdicts, learning progress, statistics |
| `/api/incidents/*` | Incident memory, similarity search, fix suggestions |
| `/api/learning/*` | Online learning, drift |
| `/api/gas/*` | Gas summary and readings |
| `/api/network/*` | OT network detectors |
| `/api/baselines/*` | Seasonal baselines |
| `/api/monitoring/metrics` | Pipeline observability |

Interactive docs: http://localhost:8000/docs

---

## Configuration

Every tunable lives in **`src/config.py`**, most overridable by environment
variable. Key groups:

| Group | Examples |
|-------|----------|
| Model | `INPUT_DIM` 4, `SEQ_LEN` 20, `LATENT_DIM` 8, `HIDDEN_DIMS` [64,32], `BETA` 0.1 |
| Detection | `ANOMALY_THRESHOLD`, `SPIKE_MAD_*`, `CUSUM_*`, `STUCK_*`, `NOISE_*`, `BIAS_*`, `CORR_BREAKDOWN_*`, `DIURNAL_*` |
| Ensemble | `ENSEMBLE_DETECTOR_WEIGHTS`, `ENSEMBLE_ANOMALY_THRESHOLD` 0.5, `ENSEMBLE_MIN_AGREEMENT` 2 |
| Active learning | `ACTIVE_LEARNING_MIN_QUALITY_LABELS` 20, `EWC_LAMBDA` 0.4, `SI_LAMBDA` 0.4 |
| Bootstrap | `BOOTSTRAP_MIN_CLEAN_ROWS` 100, `BOOTSTRAP_IF_CONTAMINATION` 0.04, `BOOTSTRAP_THRESHOLD_PERCENTILE` 99.0 |
| Queue | `UPLOAD_PORTION_ROWS` 5000, `MQ_PREFETCH_COUNT`, `MQ_PREPARE_HANDLER_TIMEOUT_SECONDS` 1800 |
| Network | `RTT_BASELINE_MS`, `IAT_CV_THRESHOLD`, `TCP_BASELINE_MS`, `THREAT_SCORE_THRESHOLD` 65.0 |

### Environment variables

```bash
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
REDIS_URL=redis://localhost:6379/0

AUTH_ENABLED=true
TENANT_API_KEYS=tenant_1:secret_a,tenant_2:secret_b

# Frontend (server-side only — never NEXT_PUBLIC_*)
API_URL=http://localhost:8000
API_KEY=secret_a
```

See `.env.example`.

---

## Deployment

- **Docker:** [docker-compose.yml](docker-compose.yml)
- **Railway:** [RAILWAY_HOSTING.md](RAILWAY_HOSTING.md) — service topology, first
  deploy, cutover, rollback
- **Render:** [render.yaml](render.yaml)

### Migrations

Run `alembic upgrade head` **before or simultaneously with** the code deploy —
the app errors on a missing column if migrations lag. Verify a single head with
`alembic heads`.

---

## Documentation index

| Doc | Contents |
|-----|----------|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Complete operator guide — every page, upload and training workflows, troubleshooting |
| [docs/ANOMALY_CATALOG.md](docs/ANOMALY_CATALOG.md) | Every anomaly type: meaning, detection method, parameters, operator action |
| [docs/anomaly_framework_spec.md](docs/anomaly_framework_spec.md) | Reference formulas and verification datasets |
| [MIGRATION_PLAN.md](MIGRATION_PLAN.md) | Streamlit → Next.js migration plan |
| [RAILWAY_HOSTING.md](RAILWAY_HOSTING.md) | Railway deployment runbook |
| [web/AGENTS.md](web/AGENTS.md) | Frontend conventions and quality gates |
