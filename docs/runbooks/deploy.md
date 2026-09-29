# Deploy — Mixed-Version Safety Runbook

**Effort:** ~15 minutes · **Risk:** medium (queue must be empty) · **Depends on:** nothing

---

## Why this runbook exists

Railway deploys the API and worker as **separate services** with no atomic
cutover. During the window between the two deploys, the API and worker run
different code versions. This matters whenever a message schema changes.

The format changes that require this procedure:

| Field | Where | Risk if skipped |
|---|---|---|
| `stuck_carry_state` | `UploadPortionMessage` | Old worker silently drops stuck-fault detection |
| `stat_carry_prior_rows` | `UploadPortionMessage` | Old worker uses wrong row-index mask, wrong statistical hits |
| `schema_version` | `UploadPortionMessage` / portion result payload | Old worker silently processes with wrong logic |
| `psychro_context` in portion result | Portion result payload | Old consumer misinterprets or ignores suppression context |

Without draining the queue first, a message published by a new API may arrive
at an old worker (or vice versa). The safe resolution: **drain first, then
deploy**. A consumer that receives a `schema_version` it does not recognise
**fails the job with a clear, retryable error** instead of producing a partial
or corrupt result.

---

## Pre-deploy: drain the upload queue

**Step 1 — Stop new uploads.**

Pause the upload route. If a maintenance flag exists in the app, enable it:

```bash
# If MAINTENANCE_MODE env var is available on Railway:
railway variables set MAINTENANCE_MODE=true --service api
```

If no maintenance flag is wired up, you can stop new inbound jobs by taking
the API service down temporarily (Railway UI → API service → Settings →
**Suspend**). Do **not** suspend the worker yet — it must finish draining.

**Step 2 — Wait for queue depth to reach 0.**

Check the three upload queues in the RabbitMQ management console. You need
`messages` to be 0 and `messages_unacknowledged` to be 0 for all three:

```bash
# Replace <RABBITMQ_HOST>, <USER>, <PASS> with your Railway RabbitMQ connection values.
# Run for each queue: upload.prepare, upload.portions, upload.finalize

curl -u <USER>:<PASS> \
  'http://<RABBITMQ_HOST>:15672/api/queues/%2F/upload.prepare' \
  | python3 -c "import sys,json; q=json.load(sys.stdin); print('ready:', q['messages'], 'unacked:', q['messages_unacknowledged'])"

curl -u <USER>:<PASS> \
  'http://<RABBITMQ_HOST>:15672/api/queues/%2F/upload.portions' \
  | python3 -c "import sys,json; q=json.load(sys.stdin); print('ready:', q['messages'], 'unacked:', q['messages_unacknowledged'])"

curl -u <USER>:<PASS> \
  'http://<RABBITMQ_HOST>:15672/api/queues/%2F/upload.finalize' \
  | python3 -c "import sys,json; q=json.load(sys.stdin); print('ready:', q['messages'], 'unacked:', q['messages_unacknowledged'])"
```

You want all six numbers to be `0` before proceeding.

**Step 3 — Confirm no jobs are in flight.**

Run this SQL against the production database. It must return **0 rows**:

```sql
-- Replace <TENANT_ID_FILTER> with '%' to check all tenants,
-- or a specific tenant_id string to scope the check.
SELECT id, tenant_id, status, created_at, updated_at
FROM jobs
WHERE job_type = 'upload_csv'
  AND status IN ('preparing', 'running', 'finalizing')
ORDER BY created_at DESC;
```

All upload jobs must be in `completed`, `failed`, or `partially_failed` before
you deploy. (The `pending` status is also safe — those jobs have not yet
published messages, so there is nothing in the queue for them.)

---

## Deploy order

**Deploy the worker first, then the API.**

Reasoning from the failure modes:

| Scenario | Behaviour |
|---|---|
| Old worker + new message | Old worker receives a message with a `schema_version` it does not know. **Best case:** Pydantic silently ignores unknown fields and the job runs with old logic — no error, wrong output, no alerting. This is the worst possible failure mode. |
| New worker + old message | New worker receives a message without `schema_version`. The worker **fails the job with a clear retryable error** and logs the version mismatch. The grower can re-upload; no data is silently corrupted. |

New worker + old message is retryable and visible. Old worker + new message is
silent and corrupt. **Worker first.**

If the queue is fully drained (queue depth 0, no jobs in flight), the order
does not strictly matter — no in-flight messages can arrive at the wrong
version. Even so, deploy the worker first as belt-and-suspenders.

```
1. git push → worker service deploys
2. Confirm worker is healthy (see Post-deploy step 1)
3. git push → API service deploys (or re-enable suspended API)
4. Re-enable uploads (unset MAINTENANCE_MODE or unsuspend API)
```

---

## Post-deploy

**Step 1 — Health checks.**

Verify both Railway services report healthy in the Railway dashboard. Check the
worker logs for the startup line that confirms schema version and thread
settings. The API health endpoint must return HTTP 200:

```bash
curl -f https://<YOUR_API_HOST>/health
```

**Step 2 — Smoke test.**

Run the async upload smoke test using the `native_dec_stuck_fault.csv` fixture
against a **test tenant** (never production data). Full procedure is in
`docs/runbooks/STEP-4-detector-fanout.md` and `STEP-5-episode-incidents.md`.

Expected result for the December stuck-fault fixture:
- **4 urgent humidity stuck-fault alerts** — onsets at 12-14, 12-15, 12-16, 12-18; each `detected_ts` = onset + 6 h
- **0 false-positive humidity** statistical hits
- **102 non-humidity** statistical hits

If the numbers differ, check the worker logs for schema-version errors before
assuming a regression.

---

## Rollback

If a job fails with a schema-version error after deploy:

1. **Stop new uploads** (maintenance flag or API suspend — same as Pre-deploy step 1).
2. **Drain the queue** — wait for `messages` and `messages_unacknowledged` to reach 0 on all three upload queues (same curl commands as above).
3. **Re-deploy the old worker version** (Railway UI → worker service → Deployments → select the previous deployment → **Redeploy**).
4. **Re-deploy the old API version** the same way.
5. **Confirm no jobs are in flight** (same SQL as Pre-deploy step 3).
6. **Re-enable uploads.**
7. Ask affected growers to **re-upload their CSV**. Jobs that failed with the version error produced no output, so no partial results need to be cleaned up.

> **Note:** Jobs that failed with a version error have `status = 'failed'` and
> `error` containing the version mismatch message. They will not be retried
> automatically. The grower must start a new upload.

---

## Follow-up: Config as Code sunset (2026-12-01)

**Deadline:** 2026-12-01 — Railway stops reading Config as Code (repo
`railway*.json` files) for every service in this project.

**Why it matters for `web`.** Of the three per-service config files, only
`web/railway.json` is still live — its `fileServiceManifest` really maps six
settings to the Railway dashboard: `build.builder`, `build.buildCommand`,
`deploy.healthcheckPath`, `deploy.healthcheckTimeout`,
`deploy.restartPolicyType`, `deploy.restartPolicyMaxRetries`. Losing
`builder: NIXPACKS` and `buildCommand: corepack enable && pnpm install
--frozen-lockfile && pnpm build` can **break web deploys outright** — that is
a bigger risk than losing the healthcheck. Losing `healthcheckPath:
/api/health` would silently revert commit `4e95205` (the fix for the 307
redirect loop that broke the probe on `/`).

**Action required.** Before 2026-12-01, migrate those six `web/railway.json`
settings to dashboard settings or Railway IaC, then delete the file.

**Safe to delete now, no operational change.** `railway.api.json` and
`railway.worker.json` are already inert (`configPath: None` on both
services — Railway reads nothing from them). `railway.dashboard.json` is
orphaned: it targets a `dashboard` service that no longer exists in the
project (live services are `api`, `web`, `worker`, RabbitMQ, RabbitMQ Web
UI, Redis, `ai-agronomist-db`).

**Verification.** After migrating, run `railway status --json` and confirm
`web`'s `latestDeployment.meta.serviceManifest.deploy` still shows the
expected effective values (`healthcheckPath: /api/health`,
`healthcheckTimeout: 120`, `restartPolicyType: ON_FAILURE`,
`restartPolicyMaxRetries: 3`) and that `build` still shows
`builder: NIXPACKS` with the same `buildCommand`.
