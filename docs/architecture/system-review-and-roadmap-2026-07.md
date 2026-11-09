# System Review, Improvement Plan & Development Roadmap

> **Historical — point-in-time record.** Written 2026-07-11. Status: partially implemented — a roadmap written before the Next.js cutover and the auth gap closure; the test counts, branch names and open items are as of that date. Not maintained against current code.

**Author:** Architect / Tech-Lead review · **Date:** 2026-07-11
**Scope:** `main` (backend, workers, MQ, ML) + `fe/nextjs-migration` (Next.js web app)
**Method:** direct code reading, test-suite collection (`main`: 2063 tests collect cleanly), live FE gate run (`tsc` clean, 22/22 vitest green), cross-check against `MIGRATION_PLAN.md`, `REFACTORING_PLAN.md` (2026-06-30), `RAILWAY_HOSTING.md`.

---

## 1. Executive summary

The system is in the best shape it has ever been. The two big debts called out in the June refactoring plan are **paid**: the legacy parallel stack (`api/`, `core/`, `detectors/`, `engines/`…) is gone from `main`, and the test suite collects and runs (2063 tests, Postgres CI active). The FE migration is **built, gated, and live-verified** — only the Railway cutover remains.

The three most important things to do next, in order:

1. **Close the auth gap** — 13 of 17 API route modules have **no** `require_api_key` dependency. Flipping `AUTH_ENABLED=true` today would still leave readings, anomalies, jobs, upload, models and learning endpoints open to anyone. This must land before the web app gets a public hostname.
2. **Finish the cutover** (§0.3 of MIGRATION_PLAN) and retire Streamlit after the parallel run.
3. **Live SCADA monitoring** — decision: **SSE over a Redis pub/sub event bus** (rationale in §5). The Priva client is still a stub; the plan below takes it from stub → real poller → streaming inference → live UI.

---

## 2. What is good (keep doing this)

| Area | Evidence |
|---|---|
| **Async job pipeline** | RabbitMQ topology with TTL retry queues + DLQs per queue (`src/mq/topology.py`), attempt-count headers, idempotent declaration. Textbook. |
| **Vendor abstraction** | `DataSourceAdapter` (`src/vendors/base.py`) with a canonical schema, typed exception hierarchy, and template-method orchestration. The live-SCADA work slots straight into it. |
| **SSE precedent** | `GET /api/jobs/{id}/events` (G5) + BFF relay + `useJobProgress` with automatic polling fallback — a proven, end-to-end streaming path through the exact production proxy chain. |
| **FE engineering quality** | zod validation at every BFF boundary, TanStack Query with per-domain staleTimes, nuqs URL-state (shareable views), virtualized tables, uPlot instead of Plotly, perf budgets asserted as failing CI gates. |
| **Secrets discipline** | API key lives only in the BFF server env; browser never sees it. Tenant keys compared with `secrets.compare_digest`. |
| **Observability base** | Request-ID middleware, structured logging, Prometheus `/metrics`, job orphan recovery on startup. |
| **Ops resilience details** | Startup DB timeout guard, stale-job reaper, heavy-result stripping on job list endpoint (documented OOM fix). |

---

## 3. Code review findings

Severity: **P0** = fix before public exposure / blocks cutover safety · **P1** = fix in the next 2–4 weeks · **P2** = scheduled debt.

### 3.1 Backend (`main`)

| # | Sev | Finding | Where |
|---|---|---|---|
| B1 | **P0** | **Auth is not enforced on most routes.** Only `feedback`, `feedback_integrated`, `incidents`, `inference_integrated` depend on `require_api_key`. `readings`, `anomalies`, `jobs`, `upload` (incl. `DELETE /upload/data`!), `models`, `learning`, `gas_anomalies`, `network_anomalies`, `monitoring`, `bootstrap` have no auth dependency at all. `enforce_tenant_access` is likewise unused there. Fix: attach the dependency at **router registration** (`app.include_router(..., dependencies=[Depends(require_api_key)])`) so no future router can be added unauthenticated, and thread the resolved tenant into each tenant-scoped query. | `src/api/main.py:278-294`, `src/api/auth.py` |
| B2 | **P0** | **SSE job stream holds one DB connection per viewer for up to 1 h.** `stream_job_events` keeps its injected `AsyncSession` (and its pooled connection) alive across the whole stream, sleeping between polls (`JOB_EVENTS_MAX_SECONDS=3600`). A handful of open job pages can exhaust the asyncpg pool and take the whole API down. Fix: open a short-lived session per poll tick inside the generator (`async with AsyncSessionLocal() as db:`), not via `Depends(get_db)`. | `src/api/routes/jobs.py:93-137` |
| B3 | P1 | `POST /jobs/{id}/cancel` only flips the DB row to `failed`; consumers already processing portions are not signalled and keep burning CPU, then may overwrite the status via `set_job_result`. Fix: consumers re-check job status before each portion; treat `failed`+`error="cancelled"` as a stop signal (or add a `cancelled` status). | `src/api/routes/jobs.py:140-148`, `src/workers/upload_consumer.py` |
| B4 | P1 | **Config sprawl regressions:** `REDIS_URL` read inline in `main.py:108` instead of `src/config.py`; CORS origins hardcoded to localhost in `main.py:143-150` (production web domain will need it configurable); vendor physical bounds (`co2 ≤ 1000`, `light ≤ 2000`) hardcoded in `base.py:209-214` and inconsistent with `src/config.py` sensor ranges. One config source, env-overridable. | `src/api/main.py`, `src/vendors/base.py` |
| B5 | P1 | **Startup hard-depends on Redis.** `FastAPICache.init` runs unconditionally; if Redis is unreachable the first cached endpoint hit fails. Wrap init in try/except with an in-memory backend fallback and a loud warning (same spirit as the DB timeout guard). | `src/api/main.py:108-111` |
| B6 | P2 | `src/config.py` is 871 lines and `src/models/` is ~60 flat modules. Split config by domain (`config/ml.py`, `config/mq.py`, `config/api.py` re-exported from `src/config`) and group models into packages (`detectors/`, `learning/`, `registry/`, `explain/`). Mechanical, low-risk, big navigability win. | `src/config.py`, `src/models/` |
| B7 | P2 | Repo-root litter: `fix_cold_run.py`, `fix_csv_persist.py`, `fix_inference.py`, `test_jobs.py`, `test_inference_batch.py`, three orchestrator generations under `scripts/`, and a dozen planning MDs at root. Move keepers to `scripts/`/`docs/`, delete the rest (git has history). Same for ~15 stale local branches and `worktree-agent-*` refs. | repo root |
| B8 | P2 | Dual dependency manifests (`requirements.txt` + `pyproject.toml` + `uv.lock`). Make `pyproject.toml`+`uv.lock` the single source; generate `requirements.txt` only if Railway needs it. | root |
| B9 | P2 | Six remaining `except Exception: pass` blocks (down from 20+ — good progress). Each needs at least a `logger.warning` with context. | `grep -rn "except Exception" src` |

### 3.2 Frontend (`fe/nextjs-migration`, `web/`)

| # | Sev | Finding | Where |
|---|---|---|---|
| F1 | **P0** | **The web app itself has no login.** The BFF injects `API_KEY` for every visitor, so whoever can reach the hostname owns the tenant's data — auth-by-obscurity once deployed. Before the hostname switch: add a session gate (NextAuth/Auth.js with credentials or OIDC) in `middleware.ts`, and map the session → tenant → per-tenant API key. This pairs with B1; neither is sufficient alone. | `web/app/api/bff/*` |
| F2 | P1 | BFF `catch {}` swallows the upstream error entirely — a 502 with no server-side log line makes prod incidents blind. Log `err` with target URL (never the key) before returning 502. Same in the SSE relay. | `web/app/api/bff/[...path]/route.ts:38`, `.../events/route.ts:45` |
| F3 | P1 | Hardcoded default scope `tenant_1`/`gh_A` in hooks (`useReadings.ts:34-35`, `useScope.ts`). Fine for the demo; must become session-derived once F1 lands (a logged-in customer must never default into another tenant's scope). | `web/lib/useScope.ts` |
| F4 | P1 | **Type-drift risk:** `openapi.json` + `api-types.gen.ts` are committed snapshots; nothing fails when `main` changes a schema. Add a CI step in the *backend* workflow that regenerates the OpenAPI spec and diffs against `web/openapi.json` (or a nightly job that opens an issue). | `web/openapi.json`, `.github/workflows/` |
| F5 | P2 | Unit-test surface is thin (22 tests, mostly validators). The e2e suite carries the weight, but the flagship anomalies feature (severity mapping, score-timeline windowing, filter reducers) deserves component/unit tests that run in ms. | `web/features/anomalies` |
| F6 | P2 | Six one-page features share near-identical `error.tsx`/`loading.tsx`; extract shared boundary components when touching them next (composition rule, not urgent). | `web/app/*` |

### 3.3 Verdict on `fe/nextjs-migration`

**PASS WITH NOTES.** Gates verified green locally during this review. Architecture (BFF isolation, server-component shells + client islands, generated API types, URL-state) is exactly what the migration plan promised and is measurably better than the Streamlit baseline. The notes (F1–F4) are all about *productionizing*, not rework. Merge-to-main strategy: keep shipping on the branch until cutover completes, then fold `web/` into `main` and make `web-ci.yml` the permanent gate.

---

## 4. Improvement plan (prioritized, with sequencing)

### Wave 1 — "Safe to expose" (≈1 week, blocks public cutover)
1. **B1** router-level auth + tenant enforcement on every tenant-scoped endpoint; e2e test: request with wrong tenant key → 403.
2. **F1** session login in the Next app (credentials provider is enough for v1), session→tenant mapping, `middleware.ts` gate on all pages + BFF routes.
3. **B2** per-tick sessions in the SSE generator.
4. **F2** BFF error logging.
5. Execute the cutover runbook (`RAILWAY_HOSTING.md`): redeploy API from `main`, connect the repo to the `web` service, set real `API_KEY`, `AUTH_ENABLED=true`, 1-week parallel run, hostname switch.

### Wave 2 — Config & ops hardening (≈1 week)
6. **B4** move `REDIS_URL`, CORS origins, vendor bounds into `src/config.py`; add `WEB_ORIGIN` env.
7. **B5** Redis-optional cache init.
8. **B3** cooperative job cancellation in consumers.
9. **F4** OpenAPI-drift CI gate.
10. Retire `src/dashboard/` (1.8 MB, 9 views) after the parallel run; delete the Railway Streamlit service.

### Wave 3 — Debt reduction (continuous, timeboxed ~20% of capacity)
11. **B6** config split + `src/models` package grouping (pure moves, keep import shims for one release).
12. **B7/B8** repo hygiene: root scripts, stale branches, single dependency manifest.
13. **B9** silent-except elimination; **F5** anomalies unit tests; **F6** shared error/loading boundaries.

---

## 5. Live SCADA monitoring integration

### 5.1 Transport decision: **SSE (Server-Sent Events), not WebSockets**

The traffic is strictly one-directional — sensors → server → browser. Operator actions (feedback, thresholds, job control) are ordinary request/response POSTs and stay that way. Given that:

| Criterion | SSE | WebSocket |
|---|---|---|
| Fits one-way telemetry push | ✅ exactly | ✅ but bidirectionality is unused |
| Passes through the existing **Next.js BFF relay** (API key stays server-side) | ✅ proven in prod path today (G5 job stream) | ❌ App-Router route handlers can't upgrade WS; needs a custom Node server or a direct browser→FastAPI socket that re-opens the CORS/key problem |
| Reconnect + resume | ✅ built into `EventSource` (`retry:`, `Last-Event-ID`) | manual heartbeat + backoff code |
| Proxy/Railway friendliness | ✅ plain HTTP; buffering already solved (`X-Accel-Buffering: no`) | extra infra care (idle timeouts, sticky upgrade) |
| Fallback story | ✅ polling fallback pattern already shipped (`useJobProgress`) | needs its own |
| Scale target (tens of operator dashboards per tenant, not thousands) | ✅ ample | overkill |

**Decision: SSE.** One multiplexed stream per browser tab (not per widget) to stay inside HTTP/1.1 connection limits. WebSockets get reconsidered only if the product later needs server-push *commands* to on-site equipment consoles — a different problem with different security requirements.

Key upgrade over today's G5 pattern: the job stream polls the DB per connection. The live stream must **not** — it fans out from Redis pub/sub (Redis is already in the stack for fastapi-cache), so N viewers cost one publish, zero DB polls, and it works across multiple API instances.

### 5.2 Target architecture

```
Priva / SCADA cloud API
      │  (pull: poller worker; later push: POST /api/ingest)
      ▼
┌─────────────────┐   RabbitMQ    ┌─────────────────────┐
│ scada-poller     │ ──ingest.──▶ │ live-inference       │
│ (new worker)     │  readings    │ consumer (new)       │
│ PrivaClient.real │              │ sliding 20-step win, │
│ normalize→canon  │              │ detector ensemble,   │
└────────┬────────┘              │ persist scores       │
         │ bulk upsert           └──────────┬──────────┘
         ▼                                  │ publish events
   Postgres (sensor_readings,               ▼
   anomaly scores, incidents)        Redis pub/sub
                                     live:{tenant}:{gh}
                                            │
                              ┌─────────────┴─────────────┐
                              │ FastAPI  GET /api/live/    │
                              │ events?gh=…&topics=…  (SSE)│
                              └─────────────┬─────────────┘
                                            │ text/event-stream
                              Next.js BFF relay (auth’d, key stays server-side)
                                            │
                              EventSource → useLiveEvents() → live tiles,
                              streaming uPlot charts, anomaly toasts
```

**Event contract** (one stream, typed events, monotonic `id:` for resume):

```
event: reading            data: {gh, ts, temperature, humidity, co2, light}
event: anomaly            data: {gh, ts, score, severity, channels, fault_label, incident_id?}
event: job                data: {job_id, status, progress_pct}      // absorbs G5 later
event: health             data: {gh, source: "priva", lag_seconds, status: "ok"|"stale"|"down"}
: keep-alive every 15 s
```

### 5.3 Delivery phases

**Phase L1 — Real ingestion (backend, ~1–1.5 weeks)**
- Implement real `PrivaClient` (OAuth token flow, greenhouse discovery, measurement fetch) behind the existing `DataSourceAdapter` contract; keep the stub as `PrivaStubClient` for tests/demo mode (`SCADA_MODE=stub|live`).
- New `scada-poller` worker (`src/workers/scada_poller.py`): per (tenant, greenhouse) schedule from `customer_model_config`, poll interval `SCADA_POLL_SECONDS` (default 60 s), watermark-based incremental fetch (last ingested ts per gh), bulk upsert via existing `db/upsert.py`, publish batch pointer to new MQ queue `ingest.readings` (reuse retry/DLQ topology).
- `health` event bookkeeping: publish source lag/staleness; alert when a greenhouse goes silent (out of `dropout_detector` territory — this is *pipeline* health, not sensor anomaly).
- Optional same-phase quick win: `POST /api/ingest/readings` (authed, canonical schema, 10k-row cap) so push-capable sources and test rigs can inject without the poller.

**Phase L2 — Streaming inference (backend, ~1–1.5 weeks)**
- `live-inference` consumer on `ingest.readings`: maintains per-greenhouse rolling window (last `SEQ_LEN=20` normalized steps, hydrate from DB on cold start), runs the existing detector ensemble/LSTM-VAE scoring path (reuse `detector_cache`, `inference.py` batch=1 path), persists scores + incidents through the existing services.
- Publish `reading` + `anomaly` events to Redis pub/sub channel `live:{tenant}:{gh}` after persist (persist-then-publish; the stream is a notification layer, DB stays the source of truth).
- Throughput note: one reading/min/greenhouse is trivial; design headroom for 1-second cadence later by batching window updates.

**Phase L3 — SSE endpoint + BFF + FE (full-stack, ~1.5–2 weeks)**
- `GET /api/live/events` in FastAPI: auth → tenant check → subscribe Redis channels for requested topics → yield SSE with `id:` (Redis stream offset or ts), keep-alive comments, `LIVE_EVENTS_MAX_SECONDS` guard. No DB session held (lesson from B2).
- BFF relay `web/app/api/bff/live/events/route.ts` — clone of the proven jobs relay.
- FE `useLiveEvents(topics)` hook: EventSource lifecycle, zod-validated events, dispatch into TanStack Query caches (`setQueryData` append for readings; invalidate for anomalies), degrade to existing polling when the stream errors (pattern already exists).
- Monitoring page goes live-first: "LIVE" badge with transport state, streaming chart appends (uPlot `setData` incremental), anomaly toast + row highlight; Jobs page migrates from per-job streams to `job` topic events.
- Playwright e2e with a mocked SSE relay (fixture streams scripted events); perf budget: event→paint < 200 ms.

**Phase L4 — Live ops (≈1 week)**
- Prometheus counters: events published/delivered, connected clients, source lag histogram.
- Alerting on `health: down/stale` → email/webhook (reuse incident pipeline).
- Retention: raw readings partitioning + rollup policy once live data accumulates (decide Postgres-native partitions vs pg_cron rollups when volume is real).

**Explicit non-goals for v1:** WebSockets, MQTT broker ingestion (revisit if a customer's SCADA can push MQTT), per-second charting, control/actuation commands.

---

## 6. Roadmap (12-week view)

| Weeks | Theme | Contents | Exit criterion |
|---|---|---|---|
| 1 | **Wave 1 — safe to expose** | B1, B2, F1, F2 + start cutover parallel run | wrong-tenant request → 403 in e2e; login wall on prod web |
| 2 | **Cutover complete** | hostname switch, Streamlit retired, Wave 2 items 6–9 | Streamlit service deleted; OpenAPI drift gate red on schema change |
| 3–4 | **L1 real ingestion** | real PrivaClient, scada-poller, ingest queue, health events | live readings from a real (or sandboxed) Priva tenant landing in Postgres |
| 5–6 | **L2 streaming inference** | live-inference consumer, Redis event bus | anomaly written + event published < 5 s after reading ingest |
| 7–8 | **L3 live UI** | /api/live/events, BFF relay, live monitoring page, jobs-topic migration | operator sees a reading appear without refresh; e2e green |
| 9 | **L4 live ops** | metrics, staleness alerting, retention decision | alert fires when poller silenced in staging |
| 10–12 | **Wave 3 debt + product next** | config/model-package split, repo hygiene, anomalies unit tests; then: fleet multi-greenhouse overview, surfacing `recommendation_engine` output in UI, feedback-loop analytics (close the online-learning loop with operator verdicts) | fleet view shipped or explicitly re-scoped |

Standing risks to track: (1) Priva API access/credentials lead time — request sandbox access **now**, it gates weeks 3–4; (2) Railway SSE idle-timeout behavior under real traffic — validate during L3 with a 1-hour soak; (3) single Redis is now on the critical path for live UX (not data integrity) — acceptable, documented fallback is polling.

---

*Cross-references: `MIGRATION_PLAN.md` §0.3 (cutover runbook), `RAILWAY_HOSTING.md`, `REFACTORING_PLAN.md` (2026-06-30 baseline — items 1, 2 resolved; 3, 4 partially; this doc supersedes its open items), `docs/online_learning_design.md`.*
