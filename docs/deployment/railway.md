# Railway deployment & performance tuning

Railway does **not** read `render.yaml` or `docker-compose.yml`. Configuration
lives in **`.railway/railway.ts`** (Railway Infrastructure as Code, whole-project
mode). Applying a change is a gated flow — plan first, never apply unless the
plan reports `0 to destroy` — covered below. This doc is the checklist for the
three app services + three backing plugins so a training upload no longer takes
30+ minutes.

## Services & configuration mechanism

The three app services — **API**, **Worker**, and **web** (the Next.js operator
UI; it replaced a Streamlit dashboard service, which no longer exists in
production) — build from this repo. API and Worker build the one `Dockerfile`
(differing only by start command, `docker/start-api.sh` / `docker/start-worker.sh`);
`web` builds with **Railpack**, with Node pinned to `20` by `web/.nvmrc`.

| Service | Start command | HTTP? |
|---------|----------------|-------|
| API     | `docker/start-api.sh`    | yes (`/health`) |
| Worker  | `docker/start-worker.sh` | no |
| web     | `pnpm start`             | yes (`/api/health`) |

Per-service **Config as Code** files (`railway.api.json`, `railway.worker.json`,
`railway.dashboard.json`, `web/railway.json`) were deleted in commit `2f924fa` —
Railway's Config-as-code mechanism is no longer used anywhere in this project.
There is nothing to set under Settings → Config-as-code for any service, and no
pending 2026-12-01 cutover: the project moved off Config as Code ahead of
Railway's sunset date, not because of it.

**The live procedure — what to run, the pinned-plan apply flow, the rule that no
plan may be applied unless it reports `0 to destroy`, and the two documented
null-default quirks (`restartPolicyType`, `build.builder`) — is
`docs/runbooks/deploy.md`** (see its "Railway configuration — Infrastructure as
Code" section). That file is the operational authority; this doc does not
duplicate it. In short: diagnose and apply with
`railway config plan` / `railway config apply` against `.railway/railway.ts`,
never by editing a service in the dashboard and never by inferring current
state from the manifest (Railway stores some defaults as `null` rather than the
resolved value, so writing a resolved value back into `railway.ts` creates a
permanent phantom diff).

Backing plugins (Railway → New → Database/Plugin):

- **PostgreSQL** — provides `DATABASE_URL`
- **Redis** — provides `REDIS_URL`
- **RabbitMQ** — provides the broker; set `RABBITMQ_URL` (use the plugin's
  **private** domain, e.g. `amqp://user:pass@rabbitmq.railway.internal:5672/`)

## Resource allocation (max 8 vCPU / 8 GB per replica)

Set each service's usage limit in **Settings → Resources**:

| Service    | vCPU | RAM  | Replicas | Why |
|------------|------|------|----------|-----|
| Worker     | 8    | 8 GB | **2–4**  | CPU LSTM training + portion scoring live here; replicas parallelize the "find" phase (competing consumers on the portions queue) |
| API        | 2–4  | 4 GB | 1–2      | I/O bound; only the small synchronous-upload path touches torch |
| Postgres   | 4–8  | 8 GB | 1        | Portion results + readings upserts |
| Redis      | 1    | 1 GB | 1        | Cache |
| RabbitMQ   | 1–2  | 2 GB | 1        | Broker |

`web` is not in this table — it runs Node/Next.js, not the torch-threaded
Python path the rest of this doc tunes, and its current resource allocation
has not been re-verified for this doc. The Streamlit dashboard row that used to
appear here is gone: that service no longer exists in production (`web`
replaced it — see `RAILWAY_HOSTING.md` §1).

## Environment variables (per service)

Set on **Worker** (the hot path):

```
TORCH_NUM_THREADS=8          # pin PyTorch intra-op pool to the 8 vCPU allocation
MQ_PREFETCH_COUNT=1          # one portion per replica at a time → each uses all 8 cores
PROFILE_LOGGING_ENABLED=true # emit PROFILE phase-timing logs; set false to silence
```

`PROFILE_LOGGING_ENABLED` gates every `PROFILE phase=…` line. Leave it `true`
while investigating, then set it `false` (or `0`/`no`/`off`) and **restart the
service** to silence the timing logs — no code change or redeploy needed. Set it
on both **Worker** and **API** (the two services that emit these logs).

`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`
default to `TORCH_NUM_THREADS` automatically via `docker/thread-env.sh` (sourced
by every start script), so you only need to override them to deviate.

Set on **API**: `TORCH_NUM_THREADS` to that service's vCPU count.

Shared (from plugins): `DATABASE_URL`, `REDIS_URL`, `RABBITMQ_URL`.

### The parallelism lever

A single upload's find phase is N portions of `UPLOAD_PORTION_ROWS` (5000) rows on
the shared `upload.portions` queue. **Worker replicas are competing consumers** —
K replicas at `MQ_PREFETCH_COUNT=1` process K portions concurrently, so the find
phase scales ~K×. Training (`upload.prepare`, prefetch=1) runs on a single replica
using all 8 of its cores; it is not split across replicas.

- Single large upload, want max speed → fewer replicas, more cores each.
- Many concurrent uploads → more replicas.

## Verifying it worked (read the profiling logs)

Every phase now emits a greppable line. In Railway logs:

```
grep 'PROFILE ' | sort
```

You will see, per job:

```
PROFILE phase=bootstrap.pass1.load_resample ok=1 elapsed_ms=... rows=...
PROFILE phase=bootstrap.pass1.physics_flags ok=1 elapsed_ms=... rows=...
PROFILE phase=bootstrap.pass1.isolation_forest ok=1 elapsed_ms=... rows=...
PROFILE phase=bootstrap.pass2.train ok=1 elapsed_ms=... sequences=... epochs=10 device=cpu
PROFILE phase=upload.prepare.train ok=1 elapsed_ms=... job=...
PROFILE phase=upload.prepare.produce_portions ok=1 elapsed_ms=... job=...
PROFILE phase=upload.portion.score ok=1 elapsed_ms=... job=... portion=... rows=...
PROFILE phase=upload.portion.persist ok=1 elapsed_ms=... job=... portion=...
PROFILE phase=upload.finalize ok=1 elapsed_ms=... job=...
```

Confirm on worker startup: `PyTorch CPU threads configured: intra_op=8 ...` and
`Thread env: TORCH_NUM_THREADS=8 OMP_NUM_THREADS=8`.

Sum the phases for one `job=` to see the real train-vs-find split — that decides
whether the remaining effort goes into training (P1: take it off the critical
path) or portion scoring (more replicas / bigger portions).
