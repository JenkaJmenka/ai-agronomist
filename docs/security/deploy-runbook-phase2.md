# Deploy Runbook — Security Phase 2

**Status (2026-09-29): the remediation in this runbook was executed and merged
in `d76ca19` (merged 2026-09-29).** Phase 3 verification, run the same day,
returned **21 PASS, 2 FAIL, 1 BLOCKED** — the failures and the blocked check
are still open, tracked in `docs/runbooks/deploy.md` under "Follow-ups:
security audit Phase 3":

- `/metrics` is unauthenticated and tenant-labelled.
- Sign-in has a timing oracle — `MIN_RESPONSE_MS` is a floor, not a fixed
  duration.
- A BFF path-traversal sink is confirmed; reachability from an external
  caller is unverified.

The blocked check was this runbook's two-IP `auth.signin_attempts`
rate-limit test, not performed because the DB has no endpoint reachable from
the verification environment.

Branch: `security/phase2-auth`. Target: `main` (auto-deploys `ai-agronomist-api`,
`web`, `ai-agronomist-worker`).

**Every secret is set via the Railway dashboard or a separate terminal. Never
via `!` in the Claude session — `!` output lands in the transcript.**

---

## Public exposure inventory (verified read-only, 2026-09-28)

| Service | HTTP domain | TCP proxy | Repo references | Decision |
|---|---|---|---|---|
| `web` | `web-production-cd280.up.railway.app` | none | `scripts/perf-probe.sh`, `scripts/perf_flows.py`, `.claude/skills/perf-investigation/SKILL.md` | **KEEP** — the only intended public entry point |
| `ai-agronomist-api` | `ai-agronomist-api-production.up.railway.app` | none | `scripts/perf-probe.sh`, `.claude/skills/perf-investigation/SKILL.md` | **REMOVE** (Step 6) — see decision below |
| `RabbitMQ Web UI` | `rabbitmq-web-ui-production-f16f.up.railway.app` | none | **none** | **REMOVE** (Step 0) |
| `RabbitMQ` | none | none | — | already private |
| `Redis` | none | none | — | already private |
| `ai-agronomist-db` | none | none | — | already private |
| `ai-agronomist-worker` | none | none | — | already private |

**There are no TCP proxies on any service.** Two consequences:

1. There is no DB proxy to remove after `schema.sql` — the planned step is moot.
2. `schema.sql` also creates `auth.signin_attempts`, which backs the sign-in
   rate limiter — the limiter fails if the table is absent, so this step is a
   hard prerequisite for login, not just for sessions.
3. `schema.sql` cannot be applied over a public endpoint. Use
   `railway connect ai-agronomist-db` or `railway run --service ai-agronomist-db psql`,
   which tunnel through the CLI, or the dashboard's query console.

There are no Redis or RabbitMQ TCP proxies to remove either.

**Two files reference the API's public domain and will break at Step 6:**
`scripts/perf-probe.sh` (~15 call sites) and
`.claude/skills/perf-investigation/SKILL.md` (lines 10-11, 98). Both curl the
public API host directly, bypassing the BFF. **Decision required from the repo
owner:** repoint them at the private domain (they then only run from inside
Railway), route them through the BFF, or accept that they stop working.
Everything under `RAILWAY_HOSTING.md` / `web/README.md` already uses the
private `*.railway.internal` host and is unaffected.

---

## Credentials

`API_CREDENTIALS_JSON` contains **`web-bff` only**. No grovsen credential is
issued in this phase. `caller_type: "integration"` exists in the model but no
integration credential is provisioned.

```json
{
  "web-bff": {
    "key_hash": "<sha256 hex of the raw web key>",
    "caller_type": "web",
    "name": "Next.js BFF",
    "tenant_ids": "*",
    "scopes": []
  }
}
```

Generate with `src/api/scripts/generate_api_key.py` in a **separate terminal**.
It prints the raw key to stdout and writes nothing to disk. The raw key goes
into `web`'s `API_KEY`; only the hash goes into `API_CREDENTIALS_JSON`.

---

## Variables

`who` is **me** = the repo owner. Claude cannot write Railway variables — the
session's classifier denies secret-store writes.

### `web`

| Var | Source | When |
|---|---|---|
| `API_URL` | **already set** → `http://<api>.railway.internal:8000` | verify only |
| `API_KEY` | dashboard — raw `web-bff` key | Step 2 |
| `AUTH_SECRET` | dashboard — `npx auth secret`, separate terminal | Step 2 |
| `AUTH_DATABASE_URL` | reference → `${{ai-agronomist-db.DATABASE_URL}}` | Step 2 |
| `AUTH_RESEND_KEY` | dashboard — Resend API key | Step 2 |
| `AUTH_RESEND_FROM` | dashboard — plain, at the verified domain | Step 2 |

`AUTH_URL` and `AUTH_TRUST_HOST` are **not needed** — `web/auth.ts:34` sets
`trustHost: true` in code.

### `ai-agronomist-api`

| Var | Source | When |
|---|---|---|
| `ENVIRONMENT` | dashboard → `production` | Step 2 (no effect on old code) |
| `API_CREDENTIALS_JSON` | dashboard — hash only | Step 2 (ignored by old code) |
| `AUTH_ENABLED` | dashboard → `true` | Step 2 — **before** the merge |
| `PORT` | already set | — |

---

## Deploy strategy — option A: variables before the merge

**All three API variables (`ENVIRONMENT`, `AUTH_ENABLED=true`,
`API_CREDENTIALS_JSON`) are set in Step 2, before the merge.**

Why this and not merge-first: the API's `/health` and web's `/api/health`
healthchecks are both active — set directly in the Railway dashboard, not
from `railway.api.json` (inert; see the Config-as-Code note in
`docs/runbooks/deploy.md`) — and `restartPolicyType: ON_FAILURE` is set on
every service. That means a new deploy that fails to start will not be
promoted; the previous deployment keeps serving, so merge-first carries no
outage risk. It still carries an exposure window, though: merging first
without the Step 2 variables set leaves the *old*, open-auth code serving
(the healthcheck has nothing to fail on — old code starts fine) for however
long it takes to notice and set them. Option A removes that window by making
the new code's first deploy boot correctly on arrival, with nothing left to
set afterward.

**What the pre-merge window looks like instead.** Setting `AUTH_ENABLED=true`
against the *old* code makes it strictly **more** restrictive, never less:
`TENANT_API_KEYS` is unset, so the 18 routes that already carry
`require_api_key` begin answering `401`. The app is partly broken until the
merge lands, and that is the intended, fail-closed trade. `ENVIRONMENT` and
`API_CREDENTIALS_JSON` are both ignored by the old code, so neither has any
effect until the new code deploys.

The new code then boots correctly on its **first** deploy, because everything
it requires at startup is already present.

There is no separate `AUTH_ENABLED` pause point. Keep the pre-merge window
short: do Step 2 and Step 4 back to back.

**Note on the healthcheck.** The API's `/health` healthcheck is already
active — it was set directly in the Railway dashboard, not from a config
file. `railway.api.json` (which also declares `healthcheckPath: /health`) is
inert: Railway reads no file for this service (`configPath: None`), so the
file's declared `healthcheckTimeout: 300` differs from the live effective
value (`null`) with no way to reconcile it short of a dashboard edit. It is
scheduled to disappear anyway — see the Config-as-Code sunset entry in
`docs/runbooks/deploy.md`. The "previous deployment keeps serving" behaviour
above already applies today, for real; it doesn't need this file to matter.

---

## Steps

### Step 0 — remove unused public exposure

| | |
|---|---|
| **Action** | Remove the `RabbitMQ Web UI` service's HTTP domain. Zero repo references; it is a public login surface onto the message broker that survives Step 6. No Redis or RabbitMQ TCP proxies exist to remove. |
| **Who** | me |
| **Check** | `curl -s -o /dev/null -w '%{http_code}' https://rabbitmq-web-ui-production-f16f.up.railway.app/` → connection failure or `404`. RabbitMQ and the worker still Online; a queue still drains. |
| **Rollback** | Re-add a generated domain in the dashboard. The hostname will differ; nothing in the repo depends on it. |

### Step 1 — prerequisites

| | |
|---|---|
| **Action** | Verify the Resend sending domain (SPF/DKIM records published). Apply `web/lib/auth/schema.sql` to `ai-agronomist-db` via `railway connect ai-agronomist-db`. Add `ENVIRONMENT=development` to `.env.example` and `.env.development`. Seed at least one `auth.user_tenants` row for your own email. |
| **Who** | me |
| **Check** | Resend shows the domain verified. `\dn` lists schema `auth`; `\dt auth.*` lists **6** tables (`users`, `accounts`, `sessions`, `verification_token`, `user_tenants`, `signin_attempts`); `\dt public.*` unchanged. `select count(*) from auth.user_tenants` ≥ 1. |
| **Rollback** | `drop schema auth cascade` — touches nothing the app owns. |

### Step 2 — set variables

| | |
|---|---|
| **Action** | Set **every** var from the tables above — all six on `web`, and all three on the API including `AUTH_ENABLED=true`. Dashboard or separate terminal. |
| **Who** | me |
| **Check** | Both services still Online. The old API code is now **more** restrictive: the 18 routes already carrying `require_api_key` answer `401`, so parts of the app break until Step 4. That is expected and fail-closed. `ENVIRONMENT` and `API_CREDENTIALS_JSON` have no effect yet. |
| **Rollback** | Unset `AUTH_ENABLED` to restore the pre-Step-2 state. The other two are inert on the old code and can stay. |

### Step 3 — verify the private path

| | |
|---|---|
| **Action** | From the `web` service shell: `curl -s -o /dev/null -w '%{http_code}' http://<api>.railway.internal:8000/health` |
| **Who** | me |
| **Check** | `200`. |
| **Rollback** | None — read-only. **If this fails, stop.** Every later step assumes the private path works. |

### ⏸ PAUSE — confirm before Step 4

### Step 4 — merge

| | |
|---|---|
| **Action** | Merge `security/phase2-auth` → `main` and push. Deploys api + web + worker together. |
| **Who** | me |
| **Check** | All three services reach Online on the new commit. The API **starts cleanly on its first deploy** — everything it needs at startup was set in Step 2. `/health` → `200` over the private domain. The 401s introduced in Step 2 stop, because the BFF now presents a credential the new code accepts. **Confirm by deployment status and logs, not by the site loading** — the healthcheck stops Railway promoting a deploy that never becomes healthy, but a working site still isn't proof the *new* code is live: the previous deployment can be the one answering if promotion silently failed, and only the dashboard's deployment status and logs show which commit is actually serving. |
| **Rollback** | `git revert -m 1 <merge sha>` and push; all three redeploy to `c485235`. The variables can stay — the old code with them set is exactly the Step 2 state. If you want the pre-Step-2 state back, also unset `AUTH_ENABLED`. |

### Step 5 — functional verification

| | |
|---|---|
| **Action** | Exercise the app end to end. |
| **Who** | me |
| **Check** | Magic-link login delivers to a real address and signs in. An anonymous `/api/bff/*` request → `401`. An anonymous app page → redirect to `/login`. A tenant-A session requesting tenant B → `403`. Live monitoring SSE still streams. **And the proof the fix works:** unauthenticated `GET https://ai-agronomist-api-production.up.railway.app/api/readings/greenhouses?tenant_id=tenant_1` → **`401`**, not `200`. |
| **Rollback** | As Step 4. |

### ⏸ PAUSE — confirm before Step 6

### Step 6 — remove the API's public domain

| | |
|---|---|
| **Action** | First settle the two referencing files (`scripts/perf-probe.sh`, `.claude/skills/perf-investigation/SKILL.md`) per the decision above. Then remove `ai-agronomist-api`'s HTTP domain. |
| **Who** | me |
| **Check** | The public host no longer resolves or `404`s at the edge. `/docs`, `/redoc`, `/openapi.json` unreachable. The app works entirely over the private domain. |
| **Rollback** | Re-add a generated domain — **the hostname will differ**, so anything pinned to the old host stays broken until updated. |

### Step 7 — Phase 3

| | |
|---|---|
| **Action** | Dispatch Phase 3 (`qa-engineer`, Opus) against the deployed system. Route enumeration from the app's route table and the Next.js route tree, never a grep. **Added requirement:** from two different client IPs, confirm the sign-in rate limiter buckets them separately — verified by inspecting `auth.signin_attempts` rows, **not** by logging IPs. If Railway's forwarded-for handling makes every client resolve to one address, that is a **failure**, not a pass: it would mean one client can exhaust the 20/hour IP budget for everyone. The limiter reads the *last* forwarded-for hop (`web/features/auth/actions.ts:28`), which is the assumption under test. |
| **Who** | Claude, on my go-ahead |
| **Check** | Every Phase 3 assertion passes, including the two-IP bucketing check above. |
| **Rollback** | n/a — verification only. Findings feed a Phase 4. |

### Step 8 — commit the report

| | |
|---|---|
| **Action** | Committed 2026-09-29 despite Phase 3 not passing (21 PASS / 2 FAIL / 1 BLOCKED) — a deliberate decision to preserve the audit trail. The `.gitignore` negation, this runbook, and `auth-audit-phase1.md` were committed together, with the open Phase 3 findings recorded in each file's status header and tracked in `docs/runbooks/deploy.md`. |
| **Who** | me, or Claude on my go-ahead |
| **Check** | `git check-ignore` no longer matches either file; both appear in `git status`; the commit contains no raw key material. |
| **Rollback** | Revert the commit and restore the blanket `docs/*` rule. |

---

## Deferred, not part of this deploy

- `scripts/perf-probe.sh` and the perf-investigation skill still target the
  public API host (Step 6 decision).
- `tests/integration/test_pgvector_incident_memory.py` claimed CI coverage in
  its own docstring while never being collected. Fixed by collecting Postgres
  tests by marker, but worth checking whether other hardcoded CI file lists
  hide the same problem.
- No grovsen credential, no ingest route, no outbound email/PDF/callback path
  exists yet. When built: recipients and callback URLs come from tenant
  configuration and never from the request payload; outbound payloads signed;
  inbound payloads size-limited with an idempotency key.
