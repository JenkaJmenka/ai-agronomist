# Performance Refactoring Plan — AI Agronomist

> **Historical — point-in-time record.** Written 2026-07-03. Status: partially implemented. First drafted under a wrong Vercel-serverless hosting assumption, then rewritten the same day for the real Railway + Supabase topology (the TL;DR below records the correction). The dashboard it plans for is the Streamlit one, since replaced by `web/`. Not maintained against current code.

**Author:** Architect
**Date:** 2026-07-03
**Status:** Proposal (design only — no implementation code)
**Revised 2026-07-03 for Railway + Supabase topology; supersedes the earlier Vercel-based draft.**
**Scope:** (a) CSV upload/ingestion, (b) anomaly inference, (c) dashboard load. Plus: verified code findings, Phase 0 deploy verification, message-broker evaluation, caching strategy.

---

## TL;DR — read this first

The earlier draft of this document reasoned about **Vercel serverless** and blamed an ephemeral-SQLite database and serverless cold starts. **That hosting assumption was wrong.** The Vercel artifacts in the working tree (`.vercel/`, `.env.local`, stray `fix_*.py`) were leftover CLI files and have been deleted. The real topology, verified 2026-07-03:

- **API (FastAPI)** and **dashboard (Streamlit)** each run as **long-lived containers on Railway** (warm; no cold starts unless Railway app-sleeping is enabled).
- **Database is Supabase Postgres**, reached through the Supavisor **session pooler** (`aws-1-eu-central-1.pooler.supabase.com:5432`), which is compatible with asyncpg prepared statements.
- The Supabase schema is **healthy** (13 tables, alembic revision 013). All tables are currently **0 rows** — because no traffic ever reached the API, not because writes vanished.

**Verified root cause of "empty DB / zero traffic" — found and fixed:** `docker/start-api.sh` bound uvicorn to `0.0.0.0` (IPv4-only), while the dashboard calls the API over the Railway private domain (`*.railway.internal`, which is **IPv6-only**). No request could ever reach the API, so nothing was ever persisted. The fix (`--host ::`, dual-stack) is committed on branch `perf/upload-path`. **Pending: Railway redeploy + smoke test.** Until that is confirmed, no latency number in this document has been measured against real traffic.

**What this means for optimization work:** the ~510 MB flat memory is **normal PyTorch + `.pt` resident memory in a warm container** — not a leak, and not a cold-start problem. There is no "split ML off serverless" problem to solve. The real, code-verified performance risks (below) only start to bite **once traffic actually flows**: pipeline-cache thrashing under `MAX_PIPELINE_CACHE_SIZE=1`, a fresh Supabase pooler connection on every dashboard query (`NullPool`), and O(rows) memory accumulation on large uploads. Phase 0 (redeploy + baseline capture) must complete before any of these is tuned, so we optimize against measured numbers rather than guesses.

---

## 1. Verified code-level findings

All line numbers verified against branch `perf/upload-path` on 2026-07-03.

| # | Finding | Location | Why it matters (Railway/Supabase terms) |
|---|---------|----------|------------------------------------------|
| F1 | `MAX_PIPELINE_CACHE_SIZE=1` on the Railway API service. Detector cache is `@lru_cache(maxsize=MAX_PIPELINE_CACHE_SIZE)`; the comprehensive pipeline cache evicts at the same bound. | Config: `src/config.py:207`. Detector: `src/api/routes/inference.py:36`. Pipeline: `src/api/routes/inference_integrated.py:49-63` (eviction `popitem(last=False)` at `:61`). | With ≥2 tenants (or ≥2 distinct checkpoints), alternating requests **evict and rebuild the detector on every call** — a full `AnomalyDetector(...)` construction incl. `.pt` load. In a warm container this is the single largest avoidable inference latency once traffic flows. |
| F2 | `NullPool` — a fresh asyncpg connection per DB call. | `src/db/session.py:59` (rationale comment `:55-58`). | Workaround for Streamlit's per-call event loop (`charts.py:22-25` does `asyncio.run(coro)` per query). Consequence: **every** dashboard query pays a full Supabase pooler TLS + connect handshake, and each open connection consumes a Supavisor client slot. On a network round-trip to `eu-central-1` this dominates dashboard read latency. |
| F3 | `attribute_batch` still loops per sequence. | `src/models/anomaly_attributor.py:216-226`. | `attribute_sequence` is batched **within** one sequence (all masked-sensor rows in a single `encode`/`decode`, `:168-179`), but `attribute_batch` calls it in a Python `for` loop, so a batch of N anomalies is N separate forward passes. Fine at low anomaly counts; scales poorly on anomaly-dense uploads. |
| F4 | `cold_run_upload` accumulates O(rows) arrays for the whole file. | `src/api/services/cold_run_upload.py`: `raw_readings` (`:96`,`:131`), `stats_parts` (`:95`,`:108`,`:136`), `anomaly_scores`/`embeddings`/`uncertainties` (`:142-144`,`:181-183`). | The two-pass streaming design bounds the *DataFrame* copy but **not** these per-sequence lists. `embeddings` is `LATENT_DIM`-wide per sequence, `raw_readings` is one dict per row. The ADR's "flat ~200 MB regardless of file size" claim is **overstated** — memory is still O(rows). This counts against the Railway service memory limit; check headroom before it OOMs on a large CSV. |
| F5 | Dashboard reads bypass the API and hit Supabase directly. | `charts.py:19,68-90` uses `AsyncSessionLocal` directly; `query_sensor_readings` (`:69`) does `SELECT ... ORDER BY timestamp` with **no LIMIT / no window**. | The Monitoring landing path (`pages/1_Active_Monitoring/1_Monitoring.py:177-179` → `monitoring_jump.load_monitoring_readings:115`) pulls the **entire** `RawReading` history for the tenant/greenhouse on first load, over a fresh pooler connection (F2). This is the "opening the monitoring dashboard is slow" path. |
| F6 | Two model caches for the same objects. | `get_cached_detector` `@lru_cache` (`inference.py:36`) **and** `AnomalyDetector._model_cache` (LRUDict, inside `src/models/inference.py`). | Redundant caching of the same `.pt`-backed detectors; two eviction policies to reason about. Low urgency but simplifies F1's fix. |

**Note on the ADR:** the merged optimizations (`implementation plan/Phase refactoring/performance-adr-report.md`) — LRU bounding, NumPy-vectorized stuck detection, batched attribution *within* a sequence, `MAX_OBSERVE_BATCH` micro-batching, `timestamp IN (...)` dedup — are real and correctly implemented. Keep them. The only claim to walk back is "flat memory ~200 MB" (see F4). The Vercel/ephemeral-SQLite root-cause narrative in the ADR-audit section of the prior draft is **obsolete** and removed.

---

## 2. Phase 0 — Redeploy verification + baseline capture (MANDATORY, do first)

The IPv6 bind fix is committed but not yet live. **Do not tune anything until the app is confirmed serving traffic and a real baseline exists.** Each step has a pass/fail signal.

**0.1 — Confirm Railway redeploys with the IPv6 bind fix.**
- The fix (`docker/start-api.sh`: `--host ::`) is on branch `perf/upload-path`. Check which branch the Railway **API service** tracks (Railway → API service → Settings → Source).
- If Railway deploys `main`, the fix is **not live** — merge `perf/upload-path` → `main` (or repoint the service) and trigger a deploy.
- Pass signal: a deploy whose build includes commit `dd23b49` (or later) of `docker/start-api.sh`, and API logs show `Starting API on [::]:8000 (dual-stack)`.

**0.2 — Smoke test dashboard → API over the private domain.**
- From the Streamlit dashboard, upload a small CSV with `persist_data=true`.
- Watch the Railway **API service → Requests** panel (should go from "no metrics" to showing the request) and the Supabase `sensor_readings` (and `raw_readings`) row count (should go from 0 to non-zero).
- Pass signal: request appears in the Railway panel AND rows appear in Supabase AND the Monitoring page renders them.
- Fail signal: still zero requests → the dashboard's `API_URL` is not pointing at the API's `*.railway.internal` domain (see `api_client.py:14,25` — default is `http://localhost:8000`). Fix the env var, not the code.

**0.3 — Capture the real baseline BEFORE optimizing anything.**
Record, from the now-live system, so later phases can be measured against it:
- Upload latency for a representative CSV (small, medium, large) — wall-clock for `POST /api/inference/upload` with `persist_data=true`.
- Inference latency for `POST /api/inference` and `POST /api/inference/comprehensive`, both single-tenant and **alternating two tenants** (to expose F1 cache thrash).
- Dashboard first-load time for the Monitoring page against a non-trivial dataset (exercises F2 + F5).
- Peak Railway service memory during a large upload (exercises F4) vs the plan's memory limit.

**Exit criterion:** a short written baseline table (the four metrics above) plus confirmation that the DB is being written and the Requests panel is non-empty. Only then start Phases 1–3. Every "expected impact" below is a hypothesis until measured against this baseline.

---

## 3. Phase 1 — Flow A: CSV upload / ingestion path

Prerequisite: Phase 0 complete (traffic flowing, baseline captured).

Current path: `POST /api/inference/upload` (`inference.py:149`) → stream to a temp file → `run_cold_run_upload` (two-pass) → optional `persist_csv_file` (chunked upsert with `IN (...)` dedup).

| Item | Change | Expected impact | Effort |
|------|--------|-----------------|--------|
| A1 | **Bound the O(rows) accumulation (F4).** Don't retain `embeddings`/`uncertainties`/`raw_readings` for the whole file. Compute anomaly hits per chunk and discard raw embeddings unless a caller explicitly requests them; stream statistical detection over `raw_readings` instead of buffering every row. | Makes memory genuinely bounded; removes the real OOM risk against the Railway memory limit on large CSVs. Verify against the 0.3 peak-memory baseline. | M |
| A2 | **Native Postgres upsert for persistence.** Replace per-row `iterrows()` in `persist_csv_file` with vectorized dict extraction + `insert(...).on_conflict_do_update` per chunk keyed on `(tenant_id, greenhouse_id, timestamp)`. Supabase Postgres supports `ON CONFLICT` natively (the current `timestamp IN (...)` dedup is a Python-side approximation). | One statement/chunk instead of select + N object mutations; removes the read-modify-write round-trip to `eu-central-1`. | M |
| A3 | **Add the unique index** `(tenant_id, greenhouse_id, timestamp)` on `SensorReading` and `RawReading` via a new Alembic revision (current head 013). A2's upsert depends on it; it also accelerates the existing `IN (...)` dedup and the read path (F5/C1). | Turns dedup into a DB constraint; large speedup on re-uploads and window reads. | S |
| A4 | **Async upload when persisting** (see §5 job-table). For large files, return a `job_id` immediately and process in the background so the HTTP request isn't held open through torch inference + N chunk commits. On a single Railway container, FastAPI `BackgroundTasks` is the minimal form. | Removes upload request timeouts; decouples slow ingest from the response. | M |
| A5 | **Collapse to a single pass once A1 lands.** The two-pass design (stats pass, then inference pass over the same temp file) exists to bound memory; with A1 fixed, stats + inference can be computed per chunk in one pass. Keep two-pass only if A1 proves insufficient. | Halves disk reads + CSV parse cost. | S |

---

## 4. Phase 2 — Flow B: anomaly inference path

Current: `POST /api/inference` (`inference.py:43`) and `POST /api/inference/comprehensive` (`inference_integrated.py:106`). Model resolved per tenant via `ModelRegistry`; detector fetched through `get_cached_detector` (`@lru_cache`, `inference.py:36`); comprehensive path wraps it in `_pipeline_cache` (`inference_integrated.py:36`).

| Item | Change | Expected impact | Effort |
|------|--------|-----------------|--------|
| B1 | **Raise `MAX_PIPELINE_CACHE_SIZE` above 1 (F1).** In a warm Railway container the in-process cache persists across requests, so a bound of 1 thrashes the moment a second tenant/checkpoint appears. **Size it to `(active distinct checkpoints you expect concurrently)` and validate against container memory:** each cached detector ≈ resident model size (~tens of MB on top of the ~510 MB torch floor). Suggested start: 2–4; measure resident memory after warming N tenants and keep headroom under the Railway plan limit. Set via the existing `MAX_PIPELINE_CACHE_SIZE` env var — **config change, not code.** | Eliminates repeated `.pt` reloads under multi-tenant traffic — likely the biggest inference win once traffic flows (confirm vs the 0.3 alternating-tenant baseline). | S |
| B2 | **Collapse the double model cache (F6).** `get_cached_detector` (`@lru_cache`) and `AnomalyDetector._model_cache` (LRUDict) cache the same detectors. Keep one — prefer the LRUDict (it has eviction hooks) — so there is a single, memory-aware eviction policy that B1's sizing applies to. | Lower memory; one clear policy; makes B1 predictable. | S |
| B3 | **Batch `attribute_batch` (F3).** Replace the per-sequence Python loop (`anomaly_attributor.py:226`) with a single batched forward pass over all N anomalies (extend the existing intra-sequence batching at `:168-179` to the batch dimension). | Cuts attribution cost on anomaly-dense uploads from N forward passes to ~1. | M |
| B4 | **Harden score-persistence query.** The timestamp branch already uses `IN (...)` (`inference.py:100-104`) — good. The fallback (`:115-118`) does `ORDER BY timestamp DESC LIMIT N` with blind index matching — fragile against Supabase and prone to mis-attributing scores. Prefer always sending timestamps and drop the blind fallback (or make it an explicit error). | Correctness; avoids a large ordered scan on the pooler. | S |
| B5 | **Optional: server-side inference result cache** keyed on `sha256(payload) + model_version`, mirroring the dashboard's Layer 1 (`inference_cache.py`). Only if the 0.3 baseline shows repeated identical inferences (retries/duplicates). | Skips recompute on duplicate requests. | M |

---

## 5. Phase 3 — Flow C: dashboard load / read queries

The FE is the **Streamlit dashboard** (`src/dashboard/`) — there is no React/Next app in the repo. The landing page `home.py` is static; the first data-heavy page is **Monitoring** (`pages/1_Active_Monitoring/1_Monitoring.py`), which on load calls `load_monitoring_readings` → `query_sensor_readings` (`charts.py:69`). That query pulls the **entire** `RawReading` history for the tenant/greenhouse with **no LIMIT and no time window** (F5), over a **fresh Supabase pooler connection** (F2). This is the concrete "opening the monitoring dashboard is slow" path.

| Item | Change | Expected impact | Effort |
|------|--------|-----------------|--------|
| C1 | **Index-back the read path.** The `(tenant_id, greenhouse_id, timestamp)` index from A3 turns the Monitoring `ORDER BY timestamp` scan and the anomaly-window queries into range scans instead of full-table scans. | Faster first paint once data volume grows; measure vs the 0.3 dashboard baseline. | S |
| C2 | **Bound the first-load query.** `query_sensor_readings` (`charts.py:69`) should default to a recent window / row cap rather than the full history, matching what the initial chart actually renders. The windowed path (`query_sensor_readings_window`) already exists in `monitoring_jump.py:98` — make the default landing load use a bounded window too. | Smaller payload, fewer rows over the wire from `eu-central-1`; directly attacks F5. | S |
| C3 | **Fix the per-call connection cost (F2) — pick one path:** (a) give the dashboard a **dedicated event-loop thread** so asyncpg connections can be reused (removing `NullPool` for the dashboard and eliminating the per-query TLS+connect handshake), **or** (b) **route dashboard reads through the API** (which already holds a warm session) and drop the dashboard's direct `AsyncSessionLocal` usage in `charts.py`. Option (b) also removes Supavisor client-slot pressure from having two DB clients and centralizes query logic. Recommend (b) as the cleaner long-term shape; (a) is the smaller change. | Removes a full pooler handshake from **every** dashboard query — likely the largest dashboard-load win. | M (a) / L (b) |
| C4 | **Down-sample wide windows.** For long time ranges, return server-side aggregated buckets instead of every raw row; charts don't need per-second fidelity across weeks. | Smaller payloads, faster first paint on large datasets. | M |
| C5 | **Cache the active-model-version lookup per tenant** with a short TTL. `get_active_model_version` currently falls back to `"baseline"` silently on error and is called on render; a short in-process TTL avoids a synchronous hop on every page load. | Removes a blocking hop from load. | S |

---

## 6. Message broker (RabbitMQ) — recommendation

**Recommendation: No — do not introduce RabbitMQ now.** (This reverses only the *rationale*, not the conclusion.)

The earlier draft rejected RabbitMQ as "incompatible with serverless." That rationale is void — **RabbitMQ is deployable on Railway** (there is a one-click template), so compatibility is not the issue. The honest Railway-terms reasons to still say no at current scale:

- **No throughput need.** Current volume is a handful of CSV uploads; the workload does not require a broker's fan-out or buffering. A broker would be optimizing a system that has processed ~zero real requests so far.
- **Operational overhead.** RabbitMQ is one more Railway service to deploy, pay for, monitor, and keep healthy (plus a long-lived consumer worker) — real recurring cost and surface area for no current benefit.
- **A simpler mechanism covers the async need.** For a single Railway API container, **FastAPI `BackgroundTasks`** handles A4's "return `job_id`, process after response" with zero new infrastructure. If/when you want durability across restarts or a separate worker, a **Postgres job table** (`jobs(id, tenant_id, type, status, payload_ref, result_ref, created_at)` with `SELECT ... FOR UPDATE SKIP LOCKED`) reuses Supabase (already a dependency) and is trivially observable — it's just rows.

Revisit a broker only when you genuinely need fan-out to many independent consumers, cross-service event streaming, or throughput a Postgres job table can't sustain. Not now.

---

## 7. Caching strategy (Railway warm-container terms)

**Key change from the serverless framing:** on Railway the API and dashboard are **long-lived**, so in-process caches (`@lru_cache`, LRUDict, `@st.cache_data`) **persist across requests** — they are genuinely effective, not wiped each invocation. That is exactly why F1 matters (`MAX_PIPELINE_CACHE_SIZE=1` under-uses a cache that would otherwise stay warm). Cache **only after** Phase 0, so you're not caching an empty DB.

| Layer | What | Where | TTL | Invalidation |
|-------|------|-------|-----|--------------|
| L1 (exists) | Upload inference results, content-addressed | Disk, dashboard side (`inference_cache.py`) | until model version changes | Key = `sha256(file)+model_version`; new version → new key |
| L2 (exists) | Sensor readings / anomaly windows / bounds | `@st.cache_data` in dashboard (`constants.py`) | short/medium TTL tiers | TTL + explicit `clear_anomaly_page_cache` on upload |
| L3 (exists, tune) | Loaded torch detectors/pipelines | In-process `@lru_cache` + LRUDict (`inference.py:36`, `inference_integrated.py:36`) | process lifetime (warm container) | LRU eviction — **raise the bound (B1), collapse the duplicate (B2)** |
| L4 (optional) | Server-side inference result cache (B5) | In-process, API container | 5–15 min | Key = `sha256(payload)+model_version` |

**In-process vs Redis on Railway:**
- **Now (single API instance, single dashboard instance):** in-process + disk (L1) is sufficient and correct. Do **not** add Redis yet.
- **Add Redis (one click on Railway) only when** you scale the API to **multiple instances** that must share L3/L4, or split the dashboard's reads onto their own service. Until there are ≥2 instances sharing state, an external cache is premature.

**Invalidation principle:** prefer **key versioning** (embed `model_version` / a per-tenant epoch in the key) over active deletion — race-free, and already how L1 works.

---

## 8. Recommended sequencing

1. **Phase 0** — redeploy with the IPv6 fix, smoke-test dashboard→API over the private domain, capture the real baseline. *Blocking.*
2. **Config quick wins** — B1 (raise `MAX_PIPELINE_CACHE_SIZE` to 2–4, validate memory) and B2 (collapse duplicate cache). Cheapest, highest-leverage once traffic flows.
3. **Read-path** — C1–C3 (index, bounded first-load query, fix per-call connection cost). Directly attacks dashboard-load slowness.
4. **Upload correctness + memory** — A1–A3 (bound accumulation, native upsert, unique index), then A5.
5. **Async + attribution** — A4 (BackgroundTasks/job table) and B3 (batch attribution).
6. **Re-benchmark against the Phase 0 baseline.** Only then consider B5/L4, Redis, or a broker — and only if the numbers demand it.

Keep the ADR's merged optimizations — they are correct. The only correction to the record is that memory is O(rows), not flat (F4), and that the production failure was a network bind bug, not the database or the hosting model.
