# Release Notes — AI Agronomist

**Latest release:** Dashboard UX Improvements (2026-06-18)  
**Test gate:** 132 passed, 1 xfailed  

---

## Dashboard UX Improvements (2026-06-18)

Operator-facing improvements across the Anomalies and Operator Feedback pages. No API or model changes.

### Anomaly table (page 2 — Anomaly Detection)

- **Removed redundant columns** — `hint_summary` and `ℹ` info-button columns are gone; their content moved into hover tooltip cards on highlighted sensor cells.
- **Hover tooltip cards** — hovering over any amber/orange/red sensor cell shows a floating card with pattern type, affected sensors, and a human-readable description. Tooltip appears after 80 ms and fades in over 120 ms to avoid flicker.
- **Friendly sensor descriptions** — hints now read "Unexpected temperature spike: 36.2°C detected, 6.2°C above normal (20–30°C)" instead of raw technical strings.
- **Heuristic pattern type** — when no matched incident exists, `pattern_type` is derived from sensor range violations: value above range → `spike`, below → `drop`, z-score flagged but in range → `deviation`.
- **Confirm / Reject on every row** — ✓/✗ buttons are shown for all rows, not only matched incident rows. Clicking a button on an unmatched row auto-registers the anomaly via `POST /api/inference/comprehensive`, then immediately submits the label. Requires the API server to be running.
- **Timestamp formatting** — anomaly timestamps display as `08 Jun 12:17` (no milliseconds).
- **Color legend** — three colored squares (red/orange/amber) with labels below the table.

### Sensor graphs (page 1 — Sensor Data)

- **Sync Graphs toggle** — sidebar toggle renders all four sensor charts as a single 2×2 Plotly subplot grid with a shared x-axis. Zooming or panning any panel instantly updates all others. Y-axes are locked to horizontal-only drag. Toggle OFF restores independent charts. Grid size and layout are identical in both modes.

### Operator Feedback page (page 6)

- **Review queue table** — 1-based row index; columns: `detected` (formatted), `score`, `pattern` (humanized from `root_cause_details`), `root cause` (snake_case → sentence case), `description` (from stored hint summary or reasoning), `sensor type` (affected sensors), `sensor value` (last sequence step for each affected sensor).
- **Select incident** — dropdown shows `1`, `2`, `3`, … matching the row index in the table above, not UUIDs.
- **Pending anomalies table** — formatted timestamps (`08 Jun 12:17`) and rounded sensor values (temp/humidity to 1 decimal, CO₂/light to 0 decimal).

### Bug fixes

- `diagnosis_card.py` no longer crashes with `TypeError` when an incident has `None` detector scores — shows `—` instead.

### Files changed

| File | Change |
|------|--------|
| `src/dashboard/components/anomaly_table.py` | Removed columns; added CSS tooltip card; always-on ✓/✗ buttons with timestamp routing |
| `src/dashboard/components/charts.py` | `_sensor_trace()` helper; `plot_synced_sensor_grid()` 2×2 subplot |
| `src/dashboard/pages/1_sensor_data.py` | Sync Graphs toggle; branched chart rendering |
| `src/dashboard/pages/2_anomalies.py` | Auto-register + mark flow in `handle_feedback` |
| `src/dashboard/services/diagnosis_enricher.py` | `_hint_for_sensor()` with human-friendly text; heuristic `pattern_type` fallback |
| `src/dashboard/pages/6_operator_feedback.py` | Queue columns; humanized labels; row-index selectbox |
| `src/dashboard/components/diagnosis_card.py` | Guard `None` detector scores |
| `src/config.py` | Added `SENSOR_UNITS` dict |

---

## Phase 4.1 — Integration + Deployment (2026-06-15)

Phase 4.1 closes the remaining deployment and observability gaps:

1. **Monitoring dashboard config** — [monitoring-dashboard-config.yaml](deployment/monitoring-dashboard-config.yaml) documents metrics, alert thresholds, and Streamlit panel mapping
2. **Live Metrics page** — Streamlit `4_metrics.py` reads `GET /api/monitoring/metrics` (detections, FP rate, pattern distribution)
3. **Expanded end-to-end tests** — 39 tests in `test_end_to_end.py` (75 combined with phase4 integration)
4. **Deployment artifacts** — [phase-4.1-deployment-checklist.md](deployment/phase-4.1-deployment-checklist.md), [ops-runbook.md](deployment/ops-runbook.md), smoke fixture `fixtures/comprehensive_request.json`

**Verification commands:**

```bash
venv/bin/python -m pytest tests/integration/test_phase4_integration.py \
  tests/integration/test_end_to_end.py -q --override-ini="addopts="
venv/bin/python -m pytest tests/ -q --override-ini="addopts="
```

---

## Operator Feedback + Ensemble (2026-06-15)

**Version label:** Operator Feedback + Gap 4 Ensemble  
**Test gate (at release):** 989 passed, 1 skipped, 1 xfailed  

This release closes the operator feedback loop with real ensemble detection, promote/rollback retrain, and Streamlit UI for labeling incidents without curl.

---

## Summary

Building on Phase 1.9.1, you can now:

1. **Score anomalies with a 4-detector ensemble** (LSTM-VAE, Isolation Forest, Mahalanobis, correlation/range)
2. **Persist full diagnosis snapshots** on incidents (attribution, pattern, detector_scores)
3. **Retrain for real** — grid-search thresholds + ensemble weights with promote/rollback
4. **Review and label in Streamlit** — open incident queue, sequence chart, diagnosis card
5. **See highlighted sensors** on the Anomalies dashboard with hover hints

---

## New features

### Gap 4 — Ensemble detector

- **`src/models/ensemble_detector.py`** — weighted vote + agreement count
- Comprehensive API response fields: `detector_scores`, `ensemble_score`, `ensemble_agreement`
- Config: `ENSEMBLE_DETECTOR_WEIGHTS`, `ENSEMBLE_ANOMALY_THRESHOLD`, `ENSEMBLE_MIN_AGREEMENT`

### Real Phase 1 retrain

- **`CustomerConfigStore`** + migration `006_add_customer_model_config`
- **`RetrainExecutor`** / **`RetrainEvaluator`** — threshold grid search + weight tuning
- **`ActiveLearner.trigger_retraining()`** — evaluate before/after, promote or rollback

### Operator dashboard

- **`pages/6_operator_feedback.py`** — review queue, label form, learning progress
- **`pages/2_anomalies.py`** — styled table with per-sensor highlighting
- **`GET /api/incidents/open`**, **`GET /api/incidents/{id}`**, **`GET /api/incidents/nearby`**

---

## Previous release — Phase 1.9.1

**Test gate (prior):** 474 passed, 0 xfailed  

This release completed the AI Agronomist **anomaly intelligence stack**: from raw sensor sequences to diagnosed incidents, operator feedback, and model improvement — all via REST API.

---

## Summary

You now have an end-to-end greenhouse monitoring system that can:

1. **Detect** environmental and infrastructure anomalies from 4-sensor time series  
2. **Explain** what went wrong (pattern, affected sensors, root cause, severity)  
3. **Recommend** immediate and follow-up actions for operators  
4. **Remember** past incidents and suggest fixes from similar historical cases  
5. **Learn** from operator labels and automatically retrain when enough feedback is collected  
6. **Adapt** models online as new normal data arrives  
7. **Run in production** with API-key auth and persistent learning state  

---

## New in Phase 1.9.1

### Comprehensive inference pipeline

**`POST /api/inference/comprehensive`** and **`POST /api/inference/batch`**

Single call runs the full stack:

| Stage | Output |
|-------|--------|
| LSTM-VAE detection | `is_anomaly`, `anomaly_score` |
| Anomaly attribution | `affected_sensors`, `attribution` |
| Pattern detector | `pattern_type`, `pattern_scores` (7 types) |
| Confidence scorer | `confidence`, `confidence_level` |
| Root cause analyzer | `root_cause`, `reasoning`, `alternative_causes` |
| Recommendation engine | `immediate_actions`, `follow_up_actions`, `severity`, `urgency` |
| Incident store | `incident_id` (auto-persisted on anomaly) |
| Active learning | `learning_progress` (samples toward next retrain) |

### Operator feedback & active learning

| Endpoint | Purpose |
|----------|---------|
| `POST /api/feedback/mark` | Record operator label (in-memory + DB persistence) |
| `POST /api/feedback/mark-anomaly` | Integrated: updates DB incident + triggers learning |
| `POST /api/feedback/record-outcome` | Update operator reliability (EMA) |
| `GET /api/feedback/statistics/{customer_id}` | Feedback and retrain stats |
| `GET /api/feedback/learning-progress/{customer_id}` | Progress toward auto-retrain threshold |

When enough high-quality feedback accumulates, **`RetrainingOrchestrator`** triggers a retrain automatically — no separate trigger endpoint.

### Production hardening (Phase D)

- **API key authentication** — `AUTH_ENABLED=true`, `TENANT_API_KEYS=tenant:key,...`, header `X-API-Key`  
  Disabled by default in dev so existing tests and local curl work without credentials.
- **Persistent learning state** — `learning_states` table (migration `005`); feedback and operator reliability survive API restarts.

### Incident memory improvements (Phase E)

- **Idempotent resolve** — second `POST /api/incidents/resolve/{id}` returns **409 Conflict**; no duplicate action rows.
- **PostgreSQL/pgvector CI** — opt-in tests (`pytest -m postgres`) and GitHub Actions workflow.

### Monitoring

**`GET /api/monitoring/metrics`** — in-process metrics: total detections, false-positive rate, average confidence, TP/FP counts over a configurable lookback window.

### Bug fixes & quality (Phases A–C)

- Pattern detector no longer crashes on single-sensor sequences  
- Active learning stats unified (removed dead `quality_score` path)  
- Operator reliability dampening for new operators  
- Root cause evidence for all 21 indicators  
- Atomic baseline assignment in anomaly attributor  

All tracked bugs (BUG-001 through BUG-011) are **closed**. See [bugs/README.md](bugs/README.md).

---

## What you can do — by role

### Greenhouse operator

1. Receive an alert or run comprehensive inference on a suspicious sensor window.  
2. Read `pattern_type`, `root_cause`, and `immediate_actions` from the API response.  
3. Confirm or reject via `POST /api/feedback/mark-anomaly`.  
4. Look up similar past incidents: `GET /api/incidents/suggest-fixes`.  
5. Resolve the incident when fixed: `POST /api/incidents/resolve/{id}`.

### Data / ML engineer

1. Train baseline model: `venv/bin/python scripts/train_lstm_vae.py`  
2. Register per-customer checkpoints via `/api/models/*`  
3. Monitor drift: `GET /api/learning/drift`  
4. Feed normal sequences: `POST /api/learning/observe`  
5. Review retrain triggers: `GET /api/feedback/learning-progress/{customer_id}`  
6. Run full test suite: `venv/bin/python -m pytest tests/ -q --override-ini="addopts="`

### DevOps / platform

1. Deploy API: `uvicorn src.api.main:app --host 0.0.0.0 --port 8000`  
2. Run migrations: `alembic upgrade head` (001 → 005)  
3. Use PostgreSQL + pgvector in production (`docker compose up -d postgres`)  
4. Enable auth: `AUTH_ENABLED=true`, configure `TENANT_API_KEYS`  
5. After bulk incident load, reindex ivfflat — see [DOCKER_SETUP.md](DOCKER_SETUP.md) §11  
6. CI: standard pytest on SQLite; pgvector tests in `.github/workflows/postgres-tests.yml`

### Analyst / dashboard user

1. Start Streamlit: `venv/bin/python -m streamlit run src/dashboard/app.py`  
2. Explore sensor time series and anomaly scores on **Anomalies**  
3. Label incidents on **Operator Feedback** (review queue from comprehensive inference)  
4. View pipeline metrics on **Metrics** (detections, FP rate, pattern distribution from API)  
5. Use Swagger (`/docs`) or curl for batch inference and incident memory APIs

---

## Database migrations

| Revision | Adds |
|----------|------|
| `001_initial` | Sensor readings |
| `002` | Incidents, incident_actions, pgvector index |
| `003` | Learning audit logs |
| `004` | Model registry |
| `005` | Active learning state persistence |
| `006` | Per-customer model config (retrain promote/rollback) |

```bash
venv/bin/alembic upgrade head
```

---

## Configuration (production)

Add to `.env` (see `.env.example`):

```env
DATABASE_URL=postgresql+asyncpg://postgres:password@host:5432/greenhouse_monitoring
AUTH_ENABLED=true
TENANT_API_KEYS=customer_a:secret_a,customer_b:secret_b
```

Development defaults: SQLite, auth off, in-memory metrics.

---

## Known limitations

| Item | Status |
|------|--------|
| Anomaly metrics | In-process only (Prometheus export documented as future hook in monitoring-dashboard-config.yaml) |
| Online learner state | In-memory per tenant (lost on restart; separate from active-learning state) |
| Auth | API keys only (no JWT/OAuth) |
| Load tests | Manual benchmarks in deployment checklist; no automated suite |
| `_pipeline_cache` | No LRU eviction (NOTE-INT-001) |

Non-blocking polish items live in [notes/README.md](notes/README.md).

---

## Upgrade from Phase 1.9 (Gaps 1–3 only)

If you were on the earlier Gaps 1–3 build:

1. Pull latest code  
2. `venv/bin/alembic upgrade head` (adds migrations 004, 005)  
3. New routes are registered automatically — check `/docs`  
4. Optional: enable `AUTH_ENABLED` before exposing to the internet  
5. Re-run tests: expect **1029 passed**

---

## Links

- [README](../README.md) — quick start and workflow index  
- [USER_GUIDE.md](USER_GUIDE.md) — detailed setup and curl walkthrough  
- [phase-4.1-deployment-checklist.md](deployment/phase-4.1-deployment-checklist.md) — staging/production gates  
- [ops-runbook.md](deployment/ops-runbook.md) — on-call procedures  
- [phase4_integration_review.md](review/phase4_integration_review.md) — integration review (PASS WITH NOTES)  
- [fix-deferred-items-plan.md](plans/fix-deferred-items-plan.md) — Phases A–E completion record  
