# Handover — Upload & Processing Fixes

**Branch:** `wip/upload-fixes` (pushed to `origin`, remote `JenkaJmenka/ai-agronomist`)
**Last commit at handover:** `40d7f73`
**Date:** 2026-07-07

---

## 1. Goal

Two user-facing asks, plus a production incident:

1. **Fix uploading & processing** — an upload showed **455 anomalies detected** on the
   Anomaly Detection page, but `sensor_readings` and `raw_readings` were **empty in the
   database**. Data is being analyzed but not persisted (or the job never truly completed).
2. **Improve performance** as originally planned — large-CSV uploads must not hit HTTP
   timeouts; processing is portioned and runs off the request path.

Earlier feature work (already done, see commits below): cold-start training on upload +
tenant model registry management (list / select / delete / delete-all).

---

## 2. Current state of the branch

Working tree is **clean**; everything is committed. `main` and `wip/upload-fixes` both
point at `40d7f73`.

Recent commits (newest first):

| Commit    | Summary |
|-----------|---------|
| `40d7f73` | fix(upload): recover upload jobs orphaned in `preparing` by restarts |
| `88a9b33` | chore(models): refresh `pipeline_cust_model` checkpoint from local run |
| `36b2d4d` | fix(upload): NameError in async pipeline (`_resolve_upload_checkpoint` leftover) |
| `5d6fb23` | chore: gitignore cold-start bootstrap outputs and artifact cache |
| `03202e3` | feat(upload): background async upload pipeline + opportunistic cold-start training |

---

## 3. The open bug — "455 anomalies but empty tables"

### What we PROVED this session
`persist_readings_chunk` (the exact function the worker uses to write rows) is **correct**.
Driving it directly against a fresh SQLite DB with valid normalized rows wrote to **both**
tables:

```
persist_readings_chunk returned (s_ins,s_upd,r_ins,r_upd): (2, 0, 2, 0)
sensor_readings rows: 2
raw_readings rows: 2
```

So the write path is not the bug. Anomaly scoring (455) is produced only by worker-side
`finalize_scores`, which means a worker DID run finalize for *some* job.

### Therefore the empty tables can only be one of:
1. The job that produced 455 ran with **`persist_data = false`** (or the 455 is a **stale
   sync analysis result** cached from an earlier "analysis-only" upload) → the persist
   branch in `handle_upload_portion` (`if msg.persist_data:`) never executed.
2. The async job **never actually completed on the worker** (matches earlier job
   `ebfbcbd4`, which polled ~80× without ever reaching a terminal state). Nothing was
   written; the visible 455 is leftover from a previous upload.

### Known facts
- Worker service on Railway **is running** (user confirmed).
- Dashboard defaults `Load data into platform = True` → `persist_data=true` on upload
  (`src/dashboard/pages/2_anomalies.py`, toggle `inf_load_data`, default `value=True`).
- `persist_data` is threaded straight through: route → `run_upload_async_pipeline` →
  `produce_upload_portions(persist_data=...)` → `UploadPortionMessage.persist_data`.
- Table names: `sensor_readings`, `raw_readings` (user's "sensor data" = `sensor_readings`).

---

## 4. DECISIVE next step (blocks the fix direction)

Run this against the Railway Postgres (Query tab) — it pins the ground truth:

```sql
SELECT status, payload->>'persist_data' AS persist, total_portions,
       completed_portions, failed_portions, created_at
FROM jobs WHERE job_type='upload_csv' ORDER BY created_at DESC LIMIT 5;
```

Interpretation:

| Result | Meaning | Fix |
|--------|---------|-----|
| `persist='false'` on the relevant job | Persist was never requested for that job | Plumbing is fine; verify the dashboard toggle state / educate. Add a guard (see §5). |
| `status` not in (`completed`,`partially_failed`,`failed`) | Async job never finished on worker | Reliability fix — see §5 root cause. |
| `status='completed'`, `persist='true'`, counts look full | Rows *should* exist | Re-check tenant/greenhouse filter; check `apply_anomaly_scores` UPDATE targets. |

Also useful:
```sql
SELECT tenant_id, greenhouse_id, count(*) FROM sensor_readings GROUP BY 1,2;
```

---

## 5. Architectural root cause & recommended fix

All real processing depends on a separately-running **worker**, and **cold-start training
runs in the API's in-process `BackgroundTasks`**. This is fragile:

- A Railway redeploy kills the in-process training task **and** the temp CSV mid-run →
  job stuck in `preparing` (mitigated by `fail_stale_preparing_jobs` startup sweep, but
  the upload still fails silently from the user's view).
- If the worker is down or slow, portions never get scored/persisted and **no error is
  surfaced** — you get "analysis appears, DB empty, no explanation."

### Recommended durable fixes (in priority order)
1. **Move cold-start training onto the worker** (like retrain already is) via a durable
   RabbitMQ job, so it survives redeploys and doesn't block the API. Break the
   "retrain-every-upload because no active model ever registers" loop.
2. **Make persistence observable / fail loud.** In `handle_upload_finalize`, if
   `payload.persist_data` is true but `sensor_rows_total == 0 and raw_rows_total == 0`,
   mark the job `partially_failed` with a clear error instead of `completed`. Surface
   `sensor_rows_uploaded` / `raw_rows_uploaded` in the dashboard summary so "0 rows
   persisted" is never silent again.
3. **Dashboard warning:** if the user requested persistence but the terminal job reports
   0 persisted rows, show an error banner (not just the anomaly count).

---

## 6. Key files

| Area | File |
|------|------|
| Async upload route (202) | `src/api/routes/inference.py` (`run_inference_from_csv_async`) |
| Background pipeline | `src/api/services/upload_async_runner.py` |
| Portion producer | `src/api/services/upload_producer.py` |
| Worker handlers (score + persist + finalize) | `src/workers/upload_consumer.py` |
| DB write path (proven correct) | `src/api/services/csv_persist.py` (`persist_readings_chunk`) |
| Bulk upsert | `src/db/upsert.py` |
| Job store | `src/db/jobs.py` (`fail_stale_preparing_jobs`, `set_total_portions`, ...) |
| Startup sweep wiring | `src/api/main.py` (lifespan) |
| Dashboard upload UI | `src/dashboard/pages/2_anomalies.py` |
| Read endpoints (dashboard) | `src/api/routes/readings.py` |
| Config thresholds | `src/config.py` (`UPLOAD_PORTION_ROWS=5000`, `UPLOAD_SYNC_MAX_ROWS=20000`) |

---

## 7. Constraints / gotchas for the next session

- **Cannot reach Railway internal network** (`*.railway.internal`) or the prod DB from a
  local machine. API/dashboard logs are the only prod window unless you use the Railway
  dashboard Query/Logs tabs. Reading `.env` for secrets is blocked (and against project
  rules).
- **`docs/` is gitignored** — put tracked docs at the repo root (this file is `HANDOVER.md`).
- Async path is taken only when `estimate_csv_rows > UPLOAD_SYNC_MAX_ROWS` (20000);
  smaller files use the synchronous `/inference/upload` route.
- Local quick-check of the persist path:
  ```bash
  PYTHONPATH=. python3 -c "..."   # drive persist_readings_chunk against sqlite (see §3)
  ```

---

## 8. Immediate to-do for whoever picks this up

1. Run the SQL in §4 → determine which branch of §3 is true.
2. If job never completes on the worker → implement §5.1 (training on worker).
3. Regardless → implement §5.2 + §5.3 so a 0-row persist can never be silent again.
4. Add a test that runs the async pipeline end-to-end with `persist_data=true` and asserts
   `sensor_readings`/`raw_readings` are non-empty.
