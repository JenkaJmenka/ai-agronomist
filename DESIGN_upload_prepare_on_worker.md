# Design — Move cold-start CSV-upload *preparation* onto the durable RabbitMQ worker (handover §5.1)

Status: PROPOSAL (design only — no code changes in this doc)
Author: architect
Date: 2026-07-07

---

## 0. Problem recap (one paragraph)

`POST /api/inference/upload-async` currently (a) writes the CSV to a local
`NamedTemporaryFile` on the **API** container, (b) creates a `preparing` job, and
(c) hands the rest to `BackgroundTasks` (`run_upload_async_pipeline`). That
in-process task does cold-start training, then `produce_upload_portions` (parse
CSV → publish one `UploadPortionMessage` per 5000 rows → stamp
`total_portions`), then deletes the temp file. A Railway redeploy kills the
BackgroundTask **and** the local temp CSV mid-run, so no portions are ever
enqueued, `total_portions` stays `NULL`, and the upload is silently lost. The
startup sweep (`fail_stale_preparing_jobs`) only marks it `failed` — the data is
gone.

The retrain path already survives redeploys because it runs on the worker and
trains from **DB-paged** data (`iter_customer_data`), never a file. Cold-start
*upload* training is different: it trains on the freshly-uploaded CSV, which
today lives **only** on the API container's local disk. Moving preparation to
the worker (a separate container with no shared filesystem) therefore requires
making the uploaded CSV **durably reachable by the worker**.

---

## 1. Chosen CSV-transport mechanism

### Decision: store the raw CSV bytes in the existing `model_artifacts` table (bytea), via thin generic helpers, keyed `uploads/<job_id>`.

Reuse the already-deployed `ModelArtifact` blob store
(`src/models/artifact_store.py`, migration 015). It already solves the exact
cross-container problem — it exists *specifically because Railway services share
no filesystem* — and already gives us: `bytea` durability in Supabase Postgres,
sha256 content-addressing, upsert-by-key (`put_checkpoint`), a
`db://model_artifacts/<key>` path scheme that already crosses the API→worker
boundary, a resolve-to-local-cache-file step (`resolve_checkpoint`), and a
delete (`delete_artifact`). The API writes the upload to bytea under key
`uploads/<job_id>`; the worker resolves that `db://` path to a local file and
feeds it to the same `CSVAdapter` / `produce_upload_portions` code unchanged.

`put_checkpoint` and `delete_artifact` are already fully generic (key + bytes);
only `resolve_checkpoint` hard-codes a `.pt` cache-file suffix. So the only new
storage code is a **suffix parameter / thin `resolve_blob` sibling** plus
clearly-named wrappers (`put_upload_blob` / `resolve_upload_blob` /
`delete_upload_blob`) so intent stays readable and CSVs are never confused with
checkpoints in call sites.

Trade-offs accepted:
- **bytea bloat.** Upload CSVs now transit `model_artifacts`. A sensor CSV is
  small: the dashboard already caps uploads at **50 MB**
  (`upload_validator.py`), and a 100k-row 4-sensor CSV is ~5-15 MB. Postgres
  TOAST/`bytea` handles this fine. Bloat is bounded because the blob is
  **deleted as soon as portions are enqueued** (§4/§5) plus a stale-blob sweep
  (§5) as backstop. This is the decisive mitigation — the table never
  accumulates.
- **Semantic mixing.** `model_artifacts` conceptually held "model checkpoints";
  now it also holds transient upload CSVs. Mitigated by the `uploads/` key
  prefix and dedicated wrapper functions. If this ever feels wrong, migrating
  the `uploads/*` keys to a dedicated table is mechanical.

### Rejected alternatives

- **Supabase Storage bucket / S3 object storage.** The "correct" long-term home
  for large blobs and the natural choice if CSV size ever grows past tens of MB.
  Rejected now for **friction**: needs new infra (a bucket), new credentials/env
  vars on *both* API and worker, a new SDK dependency and client code, and its
  own lifecycle/retention config — all to move a ≤50 MB file that the DB already
  handles. Keep it as the documented escape hatch if the 50 MB cap is ever
  raised significantly (see §8, Open Question O3).
- **Dedicated `upload_blobs` table.** Cleaner separation than reusing
  `model_artifacts`, avoids the semantic mixing above. Rejected because it costs
  a **new migration + a new store module** for essentially the same bytea
  mechanics we already have and trust. Not worth it for a blob that lives
  seconds-to-minutes. (This is the natural refactor if bloat/semantics ever
  become a real problem — low regret to defer.)
- **Carry the whole CSV inside the AMQP message.** Rejected: RabbitMQ is a poor
  large-payload store, messages would balloon (gzip helps but 50 MB messages are
  abusive), and the retry/DLQ machinery would shuttle megabytes around. The blob
  belongs in durable storage; the message carries only a **reference**.

---

## 2. New message schema + queue

### New queue: `upload.prepare`

Add to `src/mq/topology.py`:

```python
UPLOAD_PREPARE_QUEUE = "upload.prepare"

MAIN_QUEUES = (
    UPLOAD_PREPARE_QUEUE,      # NEW — declared first
    UPLOAD_PORTIONS_QUEUE,
    UPLOAD_FINALIZE_QUEUE,
    TRAINING_ONLINE_QUEUE,
    TRAINING_RETRAIN_QUEUE,
)
```

Because `declare_topology` iterates `MAIN_QUEUES`, adding the name here
**automatically** creates `upload.prepare`, `upload.prepare.retry` (TTL →
dead-letters back to main) and `upload.prepare.dlq` with the exact same
retry/DLQ conventions as every other queue. No bespoke wiring.

### New message: `UploadPrepareMessage` (in `src/mq/schemas.py`)

```python
class UploadPrepareMessage(BaseModel):
    """Instruction to prepare an uploaded CSV on the worker: cold-start train
    (when needed), then publish scored portions. The CSV itself lives in the
    artifact store; only its db:// reference travels here."""

    job_id: str
    tenant_id: str
    greenhouse_id: str = "default"
    blob_path: str                 # db://model_artifacts/uploads/<job_id>
    filename: Optional[str] = None
    persist_data: bool = False
    train_model: bool = False
    seq_len: int = 20              # SEQ_LEN default; carried for forward-compat
```

Small message (a few hundred bytes) — no gzip, no DLQ payload concerns. It is
the durable, redeliverable replacement for the `background_tasks.add_task(...)`
call.

---

## 3. New API route behavior (`POST /api/inference/upload-async`)

Remove `BackgroundTasks` entirely. New flow:

1. Validate `.csv` suffix (unchanged).
2. Stream the upload to a local `NamedTemporaryFile` (unchanged) — this file is
   now **transient scratch on the API side only**, deleted before the request
   returns.
3. `create_job(..., status="preparing", payload={filename, persist_data,
   train_model})` → obtain `job.id` (need it to key the blob).
4. `blob_path = await put_upload_blob(db, f"uploads/{job.id}", tmp_path)` — reads
   the temp file, writes bytea, returns `db://model_artifacts/uploads/<job_id>`.
5. `update_job(db, job.id, payload={**payload, "blob_path": blob_path})` — record
   the ref in the job so the stale-sweep can clean the blob even if the message
   is lost.
6. `publish_json(UPLOAD_PREPARE_QUEUE, UploadPrepareMessage(...))`.
7. `finally:` delete the local temp file.
8. Return `202 UploadJobAcceptedResponse(job_id, portion_count=0, poll_url=...)`
   (unchanged shape).

Failure handling in the route:
- If `put_upload_blob` fails → delete temp file, set job `failed`
  (`set_job_result`), raise 500. (Job exists but has no blob; sweep-safe.)
- If `publish_json` fails → the job is `preparing` with a stored `blob_path` but
  no message. The stale-prepare sweep (§5) will fail it and clean the blob after
  the timeout. We do **not** silently swallow — log ERROR and raise 500 so the
  client knows to retry. (Acceptable: blob is cleaned; user re-uploads.)

`run_upload_async_pipeline` / `src/api/services/upload_async_runner.py` is
**deleted** — its logic moves to the worker handler (§4). The route no longer
imports `resolve_upload_checkpoint`, `produce_upload_portions`, `CSVAdapter`, or
`clear_detector_cache` for the async path.

Note: `produce_upload_portions` and `resolve_upload_checkpoint` stay where they
are (`src/api/services/*`) — they are plain async functions with no FastAPI
dependency and are imported by the worker, exactly as the worker already imports
`get_cached_detector`, `resolve_checkpoint`, `score_portion`, etc. across the
`src/api/services` ↔ `src/workers` boundary today.

---

## 4. New worker handler `handle_upload_prepare`

New module `src/workers/upload_prepare_consumer.py`. Registered in
`src/workers/main.py:build_bindings()` as:

```python
QueueBinding(
    queue_name=UPLOAD_PREPARE_QUEUE,
    schema=UploadPrepareMessage,
    handler=handle_upload_prepare,
    on_terminal_failure=_terminal_upload_prepare,   # set job failed + delete blob
    prefetch=1,                                      # like retrain: long, CPU-heavy
    handler_timeout=MQ_PREPARE_HANDLER_TIMEOUT_SECONDS,
)
```

Handler outline:

```
async def handle_upload_prepare(msg, db):
    # (1) Idempotent claim / resume decision — see §5.
    proceed = await _claim_or_resume(db, msg.job_id)
    if not proceed:
        return  # redelivery of finished/terminal prep, or lost the race

    # (2) Materialize the CSV from durable storage to a worker-local file.
    local_csv = await resolve_upload_blob(db, msg.blob_path)  # db:// -> local path
    #     FileNotFoundError here => blob already deleted (prep finished on a
    #     prior delivery) OR was never stored. Treat as terminal: if job already
    #     advanced (total_portions set) skip; else fail job with clear error.

    try:
        # (3) Cold-start training. Skip if a prior (crashed) attempt already
        #     trained — payload["checkpoint_path"] present => reuse it.
        payload = (await get_job(db, msg.job_id)).payload or {}
        if "checkpoint_path" in payload:
            checkpoint_path = payload["checkpoint_path"]
            model_trained  = payload.get("model_trained", False)
            model_id       = payload.get("model_id")
            skip_reason    = payload.get("training_skipped_reason")
        else:
            (checkpoint_path, model_trained, model_id, skip_reason) = \
                await resolve_upload_checkpoint(db, msg.tenant_id, local_csv, msg.train_model)
            # persist result so a crash-retry does NOT retrain (idempotency, §5)
            await update_job(db, msg.job_id, payload={**payload,
                "checkpoint_path": checkpoint_path, "model_trained": model_trained,
                "model_id": model_id, "training_skipped_reason": skip_reason})
            if model_trained:
                clear_detector_cache()

        # (4) Parse CSV -> publish portions -> stamp total_portions.
        adapter = CSVAdapter({"filepath": local_csv}); adapter.connect()
        try:
            await produce_upload_portions(
                tmp_path=local_csv, adapter=adapter, db=db,
                tenant_id=msg.tenant_id, greenhouse_id=msg.greenhouse_id,
                checkpoint_path=checkpoint_path, persist_data=msg.persist_data,
                filename=msg.filename, seq_len=msg.seq_len,
                model_trained=model_trained, model_id=model_id,
                training_skipped_reason=skip_reason,
                existing_job_id=msg.job_id,     # updates status preparing->pending
            )
        finally:
            adapter.disconnect()

        # (5) Prep done: the portions carry their own rows. The CSV is no longer
        #     needed by anyone -> delete the blob now.
        await delete_upload_blob(db, msg.blob_path)

    except (InsufficientDataError, ValueError) as exc:
        await set_job_result(db, msg.job_id, status="failed", error=str(exc))
        await delete_upload_blob(db, msg.blob_path)
        return                       # ACK — a data/validation error will not fix on retry
    except ColdStartTrainingError as exc:
        await set_job_result(db, msg.job_id, status="failed", error="Cold-start training failed")
        await delete_upload_blob(db, msg.blob_path)
        return
    # Any *other* exception (transient DB/broker/OOM) propagates -> worker's
    # retry/DLQ machinery handles it; blob is NOT deleted so a retry can resume.
    finally:
        _cleanup_local_file(local_csv)   # worker-local cache copy only
```

Key points:
- **Deterministic failures** (`InsufficientDataError`, `ValueError`,
  `ColdStartTrainingError`, empty/unparseable CSV) are caught, mark the job
  `failed` with a clear message, delete the blob, and **ACK** — retrying cannot
  help. This mirrors `run_upload_async_pipeline`'s current `set_job_result(...,
  "failed")` branches and its CSV-parser hint text (preserve that hint).
- **Transient failures** (DB hiccup, broker reset, OOM, handler timeout) are left
  to propagate so `_handle_message` routes them to `upload.prepare.retry` →
  eventually `upload.prepare.dlq`, and the terminal hook marks the job failed.
  The blob is preserved across retries so a resumed attempt can re-read it.

### Terminal-failure hook

```python
async def _terminal_upload_prepare(db, msg: UploadPrepareMessage, error, attempt):
    await set_job_result(db, msg.job_id, status="failed", error=error)
    await delete_upload_blob(db, msg.blob_path)   # best-effort
```

Register in `main.py` (a new hook, sibling to `_terminal_job_failure`).

### Blob cleanup lifecycle (single source of truth)

The CSV blob is deleted at the **earliest** of:
1. **Success** — immediately after `produce_upload_portions` stamps
   `total_portions` (step 5). Portions are self-contained (rows travel in the
   messages), so once enqueued **no consumer needs the CSV again**, including
   finalize. Do **not** wait for finalize.
2. **Deterministic failure** — in the catch blocks (job marked failed).
3. **Terminal MQ failure** — in `_terminal_upload_prepare` after DLQ.
4. **Backstop** — the stale-prepare sweep (§5) deletes blobs for jobs stuck in
   `preparing`/`running` past the timeout, using `payload["blob_path"]`.

Every deletion is **best-effort** (log on failure); the sweep guarantees no
permanent leak.

---

## 5. Idempotency & failure semantics

### Redelivery / crash-mid-prep — the `_claim_or_resume` gate

The prep step is long and non-transactional; the worker can crash or time out
at any point. The gate must (a) let exactly one delivery run when the job is
fresh, (b) let a redelivery **resume** an attempt that crashed mid-prep, and
(c) skip a redelivery of an attempt that already finished.

```python
async def _claim_or_resume(db, job_id) -> bool:
    if await try_transition(db, job_id, "preparing", "running"):
        return True                      # fresh claim (the common path)
    job = await get_job(db, job_id)
    if job is None:
        return False
    if job.total_portions is not None or job.status in (
        "finalizing", "completed", "partially_failed", "failed"):
        return False                     # prep already finished/terminal -> skip
    if job.status in ("running", "pending"):
        return True                      # a prior attempt crashed mid-prep -> resume
    return False
```

Why each downstream step is safe to re-run on resume:
- **Training** — guarded by `payload["checkpoint_path"]`: the first attempt
  records the trained checkpoint in the job payload *before* producing portions,
  so a resumed attempt **skips retraining** and reuses it. This makes the
  expensive step effectively idempotent. (Without this guard, a crash after
  training but before portions would retrain and register a duplicate model
  version — wasteful, not corrupting. The guard removes even that waste.)
- **`produce_upload_portions`** — already redelivery-safe: portion messages are
  deduped by `record_portion_result` `ON CONFLICT (job_id, portion_idx)` in
  `handle_upload_portion`, and `set_total_portions` is an idempotent overwrite
  to the same value. Re-publishing portions on resume is therefore harmless
  (duplicates are ignored by the portion consumer).
- **`set_total_portions`** — idempotent; the race where every portion finished
  before the total was stamped is already handled (it fires finalize itself).

**Partial portion publishing:** if the worker crashes after publishing e.g. 3 of
20 portion messages, `total_portions` was never stamped (it is set *after* the
loop). On resume, portions 0-2 are re-published (deduped and ignored by the
portion consumer, which may already have processed them) and 3-19 are published
for the first time; then `total_portions=20` is stamped. Finalize fires once
`completed+failed >= 20`. No portion is lost, none double-counted. ✅

**Concurrency caveat (documented risk):** distinguishing "my own crashed attempt
being retried" from "another worker currently running this job" is not possible
without a lease/heartbeat. With `prefetch=1`, a single worker replica, and the
30 s retry-queue TTL delay before redelivery, concurrent same-job prep is
extremely unlikely. If multiple worker replicas are ever run, add a short lease
column (or a `prepared_by`/`lease_expires_at` on the job) — noted in §8 (O4).

### Interaction with `fail_stale_preparing_jobs` — **this changes meaning**

Today the API-startup sweep eagerly fails **every** `preparing` upload job,
because in the BackgroundTasks world a `preparing` job at startup was *definitely*
lost. **That assumption is now false**: prep is durable — a `preparing` job may
have a `upload.prepare` message still sitting in the queue or actively being
processed by the worker. Eagerly failing it at API startup would kill legitimate
in-flight uploads (the API redeploys independently of the worker).

Required change — make the sweep **time-based and blob-aware**, and move it off
the eager API-startup path:

- Fail only jobs that have been `preparing` (or `running` with
  `total_portions IS NULL` and `job_type='upload_csv'`) for longer than
  `STALE_PREPARE_MINUTES` (a new config, e.g. 45 min > the prep handler timeout
  + retry budget). This targets genuinely-lost work (message never delivered, or
  DLQ hook itself failed) without touching in-flight prep.
- When it fails such a job, also `delete_upload_blob(db, payload["blob_path"])`
  (backstop cleanup #4).
- Because the worker's retry→DLQ→`_terminal_upload_prepare` path already fails
  genuinely-broken prep jobs promptly, this sweep is now a **belt-and-suspenders**
  for the rare "message lost entirely" case, not the primary recovery mechanism.
- Update the docstring: it no longer describes a BackgroundTasks limitation.

Recommendation: keep running it at startup (cheap) **but** with the time gate, so
it can never fail a job younger than `STALE_PREPARE_MINUTES`. (Optionally also
run it on the worker on a timer — but startup-only is sufficient and simplest.)

---

## 6. Backpressure / timeout — fits the existing worker model

Cold-start training on an upload can take minutes; this is the same shape as
retrain and is already accommodated:

- **Prefetch = 1** on the `upload.prepare` binding (mirrors retrain) so a
  long-running prep never holds other prepare messages hostage.
- **Dedicated handler timeout.** Add
  `MQ_PREPARE_HANDLER_TIMEOUT_SECONDS` to `src/config.py`, e.g.
  ```python
  MQ_PREPARE_HANDLER_TIMEOUT_SECONDS: float = float(
      os.environ.get("MQ_PREPARE_HANDLER_TIMEOUT_SECONDS", "1800"))  # 30 min
  ```
  Between the portion default (600 s) and retrain (3600 s). A hung prep becomes a
  visible retry, never a silent prefetch jam (the exact bug the timeout model
  exists to prevent).
- **Heartbeats vs CPU-bound work — implementation-critical.** `MQ_HEARTBEAT_SECONDS
  = 300`. A blocked event loop cannot answer AMQP heartbeats, and the broker
  then resets the connection ("Connection reset by peer" → requeue loop) — this
  is documented in `handle_upload_portion`, which deliberately runs torch
  scoring via `asyncio.to_thread`. The prep handler does **more** CPU-bound work
  (model training, CSV parsing). Therefore: **the heavy CPU sections of
  `resolve_upload_checkpoint`/`train_cold_start_from_csv` and
  `produce_upload_portions` must run off the event loop** (wrap in
  `asyncio.to_thread`, or confirm they already yield). This must be verified
  during implementation — if training blocks the loop for >300 s the connection
  will reset mid-prep. (Flag for the implementing agent: audit these two call
  chains for synchronous torch/pandas blocks and wrap them; note DB calls inside
  them must stay on the loop, so the wrap may need to target the pure-compute
  inner functions rather than the whole async call.)
- **Watchdog** already exits the worker for a clean restart if the connection or
  a channel dies — unchanged, and now also protects prep.

---

## 7. Step-by-step implementation plan (agent-sized tasks)

Ordered; each labeled by layer/owner. Tasks 1-3 have no interdependencies and
can go in parallel; 4 depends on 1-3; 5-6 depend on 4; 7 (tests) last.

**T1 — MQ schema (mq layer, backend-engineer).** Add `UploadPrepareMessage` to
`src/mq/schemas.py` (fields per §2). No behavior change. Trivial.

**T2 — MQ topology (mq layer, backend-engineer).** Add `UPLOAD_PREPARE_QUEUE =
"upload.prepare"` and put it **first** in `MAIN_QUEUES` in `src/mq/topology.py`.
Automatically yields retry+DLQ. Trivial.

**T3 — Blob storage helpers (artifact layer, backend-engineer).** In
`src/models/artifact_store.py`: parametrize the cache-file suffix (add
`suffix: str = ".pt"` to `resolve_checkpoint`, or add a small `resolve_blob`
that `resolve_checkpoint` delegates to). Add thin wrappers `put_upload_blob`,
`resolve_upload_blob` (suffix `.csv`), `delete_upload_blob` — all delegating to
the existing generic `put_checkpoint`/resolve/`delete_artifact`. **No DB
migration** (reuses `model_artifacts`). Unit-testable in isolation.

**T4 — Config (config layer, backend-engineer).** Add
`MQ_PREPARE_HANDLER_TIMEOUT_SECONDS` and `STALE_PREPARE_MINUTES` to
`src/config.py` (near the other `MQ_*` constants; env-overridable, no magic
numbers).

**T5 — Worker handler (worker layer, backend-engineer).** New
`src/workers/upload_prepare_consumer.py`: `handle_upload_prepare`,
`_claim_or_resume`, `_terminal_upload_prepare` (§4/§5). Move the training +
portion-production logic out of `upload_async_runner.py`. Wire the binding into
`src/workers/main.py:build_bindings()` (prefetch=1, dedicated timeout, terminal
hook) and import the new hook. **Audit + wrap CPU-bound sections for heartbeat
safety (§6).**

**T6 — API route + sweep (API + db layer, backend-engineer).** Rewrite
`POST /api/inference/upload-async` in `src/api/routes/inference.py` per §3
(create job → `put_upload_blob` → record `blob_path` in payload → publish →
delete temp file → 202; remove `BackgroundTasks`). Change
`fail_stale_preparing_jobs` in `src/db/jobs.py` to time-based + blob-aware (§5)
and update its docstring. **Delete** `src/api/services/upload_async_runner.py`
and its imports. Keep `produce_upload_portions` / `resolve_upload_checkpoint`
where they are.

**T7 — Tests (tests layer, qa-engineer).** Mirror the existing no-broker harness
in `tests/workers/test_upload_consumer.py` /
`test_upload_finalize_persist_guard.py`:
- Route: 202 returned, job `preparing`, blob stored under `uploads/<job_id>`,
  one `UploadPrepareMessage` published, temp file deleted (mock publisher/blob).
- Handler happy path: prepare → portions published → `total_portions` stamped →
  blob deleted.
- Idempotency: redelivery after `total_portions` set → skipped; resume after
  simulated crash post-training → **no retraining** (checkpoint reused from
  payload), portions re-published + deduped, finalize still fires once.
- Deterministic failure (`InsufficientDataError`) → job `failed`, blob deleted,
  ACK.
- Stale sweep: job younger than `STALE_PREPARE_MINUTES` untouched; older one
  failed + blob deleted.
- Blob store round-trip (`put_upload_blob`/`resolve_upload_blob`/`delete`).

**T8 — Code review (code-reviewer).** Guardrails: no tight API↔worker coupling
beyond the existing service-import pattern; blob deletion on every terminal
path; heartbeat-safety of CPU sections; no secrets.

### Migration / ordering concerns

- **No DB schema migration** (reuses `model_artifacts`). If the team later prefers
  a dedicated table, that is an independent follow-up.
- **Queue declaration before publish:** `declare_topology` (called by both
  `publish_json` on first publish and worker startup) creates `upload.prepare` +
  retry + DLQ from `MAIN_QUEUES`. Idempotent. Adding the name is sufficient.
- **Deploy order: WORKER FIRST, then API.** The worker must be consuming
  `upload.prepare` before the API starts publishing to it; otherwise prepare
  messages pile up undelivered until the worker catches up. (They are durable, so
  nothing is lost even if order slips — but worker-first avoids a visible stall.)
  Both services must ship the new `src/config.py` constants and `src/mq/*`
  changes together (shared modules).
- **In-flight during cutover:** old-style `preparing` jobs created by the
  outgoing API code (BackgroundTasks) that die in the redeploy are cleaned by the
  (now time-gated) sweep — accept a one-time loss of any upload mid-flight during
  the deploy, same as any redeploy today.
- `upload.portions` / `upload.finalize` queues and their in-flight messages are
  untouched — `upload.prepare` is purely additive upstream of them.

---

## 8. Risks & open questions for the user

- **O1 — Heartbeat safety of training (highest risk).** Cold-start training must
  run off the event loop or it will trip the 300 s AMQP heartbeat and reset the
  connection mid-prep (the documented "Connection reset by peer" failure). The
  implementing agent must audit `train_cold_start_from_csv` /
  `resolve_upload_checkpoint` / `produce_upload_portions` for synchronous
  torch/pandas blocks and wrap the pure-compute parts in `asyncio.to_thread`.
  **Confirm this is in scope for implementation.**
- **O2 — Retention / bloat.** Blobs are deleted on success/failure + a
  time-based backstop sweep. Confirm `STALE_PREPARE_MINUTES` (proposed 45) and
  `MQ_PREPARE_HANDLER_TIMEOUT_SECONDS` (proposed 1800). Is a periodic (not just
  startup) blob-orphan sweep wanted, or is startup-only acceptable?
- **O3 — Max CSV size.** The dashboard caps uploads at 50 MB. Is that the
  hard platform ceiling? If uploads could ever be much larger (hundreds of MB),
  we should switch the transport to Supabase Storage/S3 (the documented escape
  hatch) rather than bytea. Please confirm the ceiling.
- **O4 — Worker replicas.** The `_claim_or_resume` gate is safe for a single
  worker replica (prefetch=1 + 30 s retry delay). If you plan to scale the worker
  horizontally, we need a short job lease to prevent two replicas from preparing
  the same job concurrently — additive, but out of scope unless you want it now.
- **O5 — Duplicate model registration on the (now unlikely) retrain-then-crash
  path.** The `payload["checkpoint_path"]` guard removes retraining on resume. If
  the very first attempt crashes *after registering a model but before writing
  the payload guard*, a resume could register a second version. This is rare and
  non-corrupting (latest active wins). Acceptable, or do you want the training
  step to also be transactionally marked? (Recommend: accept.)

---

## Where I was uncertain

- **Reuse `model_artifacts` vs a dedicated `upload_blobs` table.** I chose reuse
  for zero-migration friction and to leverage proven code, but a dedicated table
  is the cleaner long-term shape. Either is defensible; I made the low-friction
  call the handover hinted at.
- **Exact heartbeat-wrapping boundary (O1).** I could not fully trace whether
  `train_cold_start_from_csv` blocks the loop without reading it; I flagged it as
  a mandatory implementation-time audit rather than guessing the wrap points.
- **Sweep placement** (API startup vs a worker timer). I recommended time-gated
  startup for simplicity; a periodic worker sweep is a reasonable alternative if
  the team prefers prep-lifecycle logic to live entirely worker-side.
