# Handover — Upload & Processing Fixes

**Branch:** `wip/upload-fixes` (pushed to `origin`, remote `JenkaJmenka/ai-agronomist`)
**Last commit at handover:** `b5040fb`
**Date:** 2026-07-07

---

## 1. Goal (recap)

1. **Fix uploading & processing** — an upload showed **455 anomalies detected** while
   `sensor_readings` / `raw_readings` were **empty in the database** (data analyzed but
   not persisted, or the job never truly completed).
2. **Improve performance** — large-CSV uploads must not hit HTTP timeouts; processing is
   portioned and runs off the request path.

---

## 2. What this session did

All work below is committed and **pushed** to `origin/wip/upload-fixes`.

| Commit    | Summary |
|-----------|---------|
| `b5040fb` | feat(upload): move cold-start prep onto durable worker (§5.1) |
| `df10e0e` | fix(upload): fail loud when persist requested but 0 rows written (§5.2 + §5.3) |
| `fedbc56` | docs: add upload/processing handover (previous session) |

Full test suite: **1800 passed, 12 skipped, 1 xfailed**, clean collection, no regressions.

### 2a. §5.2 + §5.3 — a 0-row persist can no longer be silent (`df10e0e`)
- **Worker** (`src/workers/upload_consumer.py`, `handle_upload_finalize`): when `persist_data`
  is true and data was processed (`stat_carry.total_rows > 0`) but 0 rows reached
  `sensor_readings`/`raw_readings`, the job is marked **`partially_failed`** with an
  actionable `error`, instead of `completed`.
- **Dashboard** (`src/dashboard/components/inference_results.py`, `render_upload_status_bar`):
  shows an **error banner** when a terminal upload persisted 0 rows despite requesting
  persistence (gated on the row-count keys being present, so legacy job payloads don't
  false-fire).
- Tests: end-to-end persist writes rows + completes clean; the incident shape →
  `partially_failed`; single-table partial writes stay `completed`; `partially_failed`
  jobs stay displayable.

### 2b. §5.1 — cold-start upload prep moved onto the durable worker (`b5040fb`)
Design doc: **`DESIGN_upload_prepare_on_worker.md`** (repo root).
- **New `upload.prepare` queue** + `UploadPrepareMessage` (declared first in `MAIN_QUEUES`,
  so it inherits retry/DLQ wiring).
- **CSV transport**: the uploaded CSV crosses the API→worker boundary via the existing
  `model_artifacts` bytea store — new `put_upload_blob` / `resolve_upload_blob` /
  `delete_upload_blob` in `src/models/artifact_store.py`, keyed `uploads/<job_id>`
  (Railway containers share no filesystem). **No DB migration.** bytea confirmed fine for
  the real workload (largest known CSV ≈ 13 MB / 110K rows; dashboard already caps at 50 MB).
- **New worker handler** `handle_upload_prepare` (`src/workers/upload_prepare_consumer.py`,
  prefetch=1, `MQ_PREPARE_HANDLER_TIMEOUT_SECONDS=1800`): idempotent claim/resume
  (`preparing→running` via `try_transition`; a crashed attempt resumes and **skips
  retraining** via the persisted `checkpoint_path`), cold-start train **off the event loop**
  (heartbeat-safe), `produce_upload_portions`, stamp `total_portions`, delete the blob.
- **Route** (`POST /inference/upload-async`): store blob → publish prepare → 202.
  `BackgroundTasks` removed; `upload_async_runner.py` **deleted**.
- **`fail_stale_preparing_jobs`** (`src/db/jobs.py`) is now a **time-based, blob-aware
  backstop** (`STALE_PREPARE_MINUTES=45`) instead of failing every `preparing` job at API
  startup — because prep is now legitimately durable. `try_transition` bumps `updated_at`,
  so an actively-progressing prep is never swept mid-run.
- **Error semantics**: deterministic errors (bad/empty CSV, insufficient data, cold-start
  failure) fail the job + delete the blob + ACK; transient errors ride retry/DLQ and
  **preserve** the blob so a resumed attempt can re-read it.
- Tests: rewritten async-route test (stores blob, publishes prepare, 202, no in-process
  training); prepare-handler unit tests (happy path, resume-without-retrain,
  deterministic/transient split, blob lifecycle, missing-blob, terminal hook); worker-binding
  test updated for the 5th queue.

---

## 3. Diagnosis of the original incident (RESOLVED)

The decisive SQL (§4 of the prior handover) was run against Supabase and disambiguated it:

| Jobs | Result | Meaning |
|------|--------|---------|
| **07-05 ×2** | `completed`, 22/22 portions, `persist=true`, **but `sensor_readings` empty** | **Stale deployment.** These ran on 2026-07-05, ~24h *before* the async persist pipeline (`03202e3`, committed 2026-07-06 17:38) existed. The "455 anomalies, empty tables" you saw is old code that no longer exists in the branch — not reproducible against HEAD. |
| **07-06 ×2** | `failed`, `total_portions=NULL` | Died during in-process "preparing" before portions were enqueued → the §5.1 redeploy-kills-BackgroundTasks problem. |
| **07-06 ×1** | `preparing`, `total_portions=NULL` | Orphaned mid-prep (swept by the old startup sweep). |

`SELECT tenant_id, greenhouse_id, count(*) FROM sensor_readings` → **no rows** (empty across
all tenants — not a read-side filter mismatch).

**Conclusion:** the empty-tables incident was old deployed behavior; the genuinely-current
bug was jobs dying in `preparing` (now fixed by §5.1). The current branch's persist path
(`persist_readings_chunk` + upsert) is correct — for non-empty valid rows it either writes
or raises (which would fail the portion). §5.2 now makes any *future* 0-row persist loud.

---

## 4. Immediate to-do for whoever picks this up

1. **Deploy `wip/upload-fixes`.** Deploy order matters: **worker first** (so it is consuming
   `upload.prepare`), **then the API** (which starts publishing). No DB migration.
2. **Validate against prod.** Upload a large CSV (>20000 rows; the ≈13 MB / 110K-row file is
   ideal) with **Load data into platform** on, then re-run:
   ```sql
   SELECT status, payload->>'persist_data' AS persist, total_portions,
          completed_portions, failed_portions, created_at
   FROM jobs WHERE job_type='upload_csv' ORDER BY created_at DESC LIMIT 5;

   SELECT tenant_id, greenhouse_id, count(*) FROM sensor_readings GROUP BY 1,2;
   ```
   Expect: job `completed` with `sensor_readings`/`raw_readings` populated. If persistence
   still writes 0 rows, §5.2 now surfaces it as `partially_failed` + a clear error (no longer
   silent) — read `jobs.error` and the worker logs.
3. **Optional — independent code review of §5.1.** This session's T8 (fresh `code-reviewer`
   agent) was blocked by a Claude Code account session limit and was replaced by an inline
   manual review. Re-run a fresh review on `b5040fb` if you want an independent pass. Manual
   review found no blockers; the two accepted low-probability risks are documented in
   `DESIGN_upload_prepare_on_worker.md` (multi-worker double-claim; a train→crash→resume
   double-registration window).

---

## 5. Key files

| Area | File |
|------|------|
| Async upload route (202) | `src/api/routes/inference.py` (`run_inference_from_csv_async`) |
| **Worker prep handler (NEW)** | `src/workers/upload_prepare_consumer.py` (`handle_upload_prepare`) |
| Upload-blob store (NEW helpers) | `src/models/artifact_store.py` (`put/resolve/delete_upload_blob`) |
| Prepare message / queue | `src/mq/schemas.py` (`UploadPrepareMessage`), `src/mq/topology.py` (`UPLOAD_PREPARE_QUEUE`) |
| Worker binding | `src/workers/main.py` (`build_bindings`) |
| Portion producer | `src/api/services/upload_producer.py` (`produce_upload_portions`) |
| Portion + finalize handlers | `src/workers/upload_consumer.py` (`handle_upload_portion`, `handle_upload_finalize`) |
| DB write path | `src/api/services/csv_persist.py` (`persist_readings_chunk`) |
| Bulk upsert | `src/db/upsert.py` |
| Job store + stale sweep | `src/db/jobs.py` (`fail_stale_preparing_jobs`, `try_transition`, `set_total_portions`, ...) |
| Startup sweep wiring | `src/api/main.py` (lifespan) |
| Dashboard upload UI + status bar | `src/dashboard/pages/2_anomalies.py`, `src/dashboard/components/inference_results.py` |
| Config thresholds | `src/config.py` (`UPLOAD_PORTION_ROWS`, `UPLOAD_SYNC_MAX_ROWS`, `MQ_PREPARE_HANDLER_TIMEOUT_SECONDS`, `STALE_PREPARE_MINUTES`) |
| §5.1 design | `DESIGN_upload_prepare_on_worker.md` (repo root) |

---

## 6. Constraints / gotchas

- **Cannot reach Railway internal network / the prod DB from a local machine.** Use the
  Supabase Query/Logs tabs (the `jobs` and `sensor_readings` tables both live in Supabase).
- **`docs/` is gitignored** — tracked docs go at the repo root (`HANDOVER.md`,
  `DESIGN_upload_prepare_on_worker.md`).
- Async path is taken only when `estimate_csv_rows > UPLOAD_SYNC_MAX_ROWS` (20000); smaller
  files use the synchronous `/inference/upload` route.
- **Deploy order (§5.1):** worker before API. If the API publishes `upload.prepare` before
  the worker declares/consumes it, messages queue harmlessly once topology is declared, but
  deploying the worker first avoids a window of unconsumed prepare jobs.

---

## 7. Nice-to-have follow-ups (not blocking)

- Surface the worker's richer `job.error` in the dashboard for `partially_failed` uploads
  (currently the dashboard re-derives its own banner text; `job.error` is log/API-only).
- Consider a periodic worker-side stale sweep in addition to the API-startup one.
