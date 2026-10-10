# Auth Exposure Audit — Phase 1 (Diagnose Only)

> **Status (2026-09-29):** the Phase 1 findings below — and the Phase 2
> remediation in `deploy-runbook-phase2.md` — were resolved in commit
> `d76ca19` (merged 2026-09-29). Phase 3 verification, run the same day,
> returned **21 PASS, 2 FAIL, 1 BLOCKED**; the failures and the blocked check
> are still open, tracked in `docs/runbooks/deploy.md` under "Follow-ups:
> security audit Phase 3":
> - `/metrics` is unauthenticated and tenant-labelled.
> - Sign-in has a timing oracle — `MIN_RESPONSE_MS` is a floor, not a fixed
>   duration.
> - A BFF path-traversal sink is confirmed; reachability from an external
>   caller is unverified.
>
> The blocked check was the runbook's two-IP `auth.signin_attempts`
> rate-limit test, not performed because the DB has no endpoint reachable
> from the verification environment.
>
> **Not everything below is fixed.** The route inventory in this document —
> §2b in particular — describes the pre-remediation state, and one of those
> routes, `/metrics`, is still unauthenticated in production today; it is the
> first open item listed above, not a closed historical finding.

## Un-ignoring (applied 2026-09-29)

This section previously prescribed the `.gitignore` diff needed to make this
file committable; that negation has now landed, alongside this commit. The
diff actually applied differs from the one once planned here: it adds a
`docs/security/*` re-block line, which git requires (a directory that is
merely un-ignored by name still excludes the per-file negations inside it
unless the directory's contents are re-blocked first), and it also covers
`deploy-runbook-phase2.md`, which the original plan didn't mention. One
piece of the original reasoning still holds and is worth keeping: leaving
the directory ignored until now was the fail-safe — the worst case of
forgetting to un-ignore it was a missing doc, not a leaked vulnerability
list.

Date: 2026-09-28
Scope: read-only investigation. No application code changed. No destructive
routes called. No email/report/webhook triggered.

## Summary numbers (see methodology in §2)

| Metric | Count |
|---|---|
| Unauthenticated tenant-scoped FastAPI routes (no `require_api_key` dependency at all) | **58** |
| Unauthenticated destructive routes (DELETE / wipe) | **6** |
| API's Railway domain publicly reachable, unauthenticated | **Yes** — verified |
| `AUTH_ENABLED` set in production (`ai-agronomist-api` service) | **NOT SET** (defaults to `false`) — verified |
| `TENANT_API_KEYS` set in production (`ai-agronomist-api` service) | **NOT SET** (defaults to empty) — verified |

Because `AUTH_ENABLED` is unset (→ `false`) in production, `require_api_key`
returns `None` unconditionally and `enforce_tenant_access` no-ops whenever
`auth_tenant is None` (`src/api/auth.py:29-49`). **This means every tenant-scoped
route — including the ones that already call `require_api_key` in code — is
currently open in production**, not just the 58 counted below. Full tally
across the 21 route modules: **81 route decorators total, 18 call
`require_api_key`, 63 do not**; of those 63, 5 are not tenant-scoped
(`health.py` ×2, `GET /api/auth/validate`, `GET /api/monitoring/metrics`,
`GET /api/config/crop-profiles`) — leaving **58 tenant-scoped unauthenticated
routes** (§2b). This 58/63 split below is a code-level (static) gap; the
runtime gap today is 100% (see the `AUTH_ENABLED` note above).

---

## 1. BFF routes (`web/app/api/bff/...`)

| Route | Methods | Upstream target | Auth today |
|---|---|---|---|
| `web/app/api/bff/[...path]/route.ts` (catch-all) | GET, POST, PUT, DELETE | `${API_URL}/api/<path>` (any path) | **None.** No session/login check on the incoming browser request at all — no NextAuth, no cookie/JWT check, nothing (`grep` of `web/` found no session library). It unconditionally forwards the request and attaches `X-API-Key: $API_KEY` server-side (`web/app/api/bff/[...path]/route.ts:24-25`) if that env var is set. Any anonymous caller who can reach the Next.js server gets the same treatment as a "logged in" one, because there is no login concept anywhere in `web/`. |
| `web/app/api/bff/live/events/route.ts` | GET (SSE) | `${API_URL}/api/live/events?tenant_id=...&greenhouse_id=...` | Same: no session check. Proxies `tenant`/`gh` query params straight through as `tenant_id`/`greenhouse_id`, attaches the API key server-side. Caller picks the tenant. |
| `web/app/api/bff/jobs/[id]/events/route.ts` | GET (SSE) | `${API_URL}/api/jobs/${id}/events` | Same: no session check. `job_id` is caller-supplied; no tenant ownership check upstream either (see `jobs.py` in §2). |

**Verified:** `grep -rln "next-auth|getServerSession|iron-session|jwt|cookies().get" web/app web/lib` returned nothing, and `web/package.json` has no auth dependency. There is no human-login gate anywhere in the current web app — the BFF's only "auth" is that it holds an API key the browser never sees, and that key is currently meaningless because `AUTH_ENABLED=false` upstream.

---

## 2. FastAPI routes

Router mounting (from `src/api/main.py:326-346`) and per-route auth, tenant
determination. "✓" = dependency present in code; "—" = absent. Tenant column:
**cred** = bound to the authenticated tenant, **caller** = taken verbatim
from query/form/body/path with no binding to any credential.

### 2a. Routes WITH `require_api_key` + `enforce_tenant_access` (structurally protected, but non-functional today per the runtime note above)

| Method | Path | Tenant source |
|---|---|---|
| POST | `/api/feedback/mark` | caller (`request.customer_id`), checked against cred |
| GET | `/api/feedback/statistics/{tenant_id}` | caller, checked against cred |
| GET | `/api/feedback/learning-progress/{tenant_id}` | caller, checked against cred |
| POST | `/api/feedback/record-outcome` | caller (`request.customer_id`), checked against cred |
| POST | `/api/feedback/mark` (integrated, `feedback_integrated.py`) | caller (`request.customer_id`), checked against cred |
| POST | `/api/inference/...` comprehensive (`inference_integrated.py`) | caller (`request.customer_id`), checked against cred |
| POST | `/api/inference/...` batch (`inference_integrated.py`) | caller (`request.customer_id`), checked against cred |
| GET | `/api/live/status` | caller (query `tenant_id`), checked against cred |
| POST | `/api/live/resume` | caller (query `tenant_id`), checked against cred |
| POST | `/api/live/pause` | caller (query `tenant_id`), checked against cred |
| GET | `/api/live/events` | caller (query `tenant_id`), checked against cred |
| GET | `/api/incidents/open` | caller (query `tenant_id`), checked against cred |
| GET | `/api/incidents/nearby` | caller (query `tenant_id`), checked against cred |
| DELETE | `/api/incidents/clear` | caller (query `tenant_id`), checked against cred |
| GET | `/api/incidents/{incident_id}` | caller (query `tenant_id`), checked against cred |
| GET | `/api/config/operational` | caller (query `tenant_id`), checked against cred |
| PUT | `/api/config/operational` | caller (query `tenant_id`), checked against cred |
| GET | `/api/baselines/seasonal` | caller (query `tenant_id`), checked against cred |

That's 18 routes. All correctly reject a mismatched `tenant_id` **when `AUTH_ENABLED=true`** — the binding logic in `src/api/auth.py:41-49` is sound. They do nothing today because auth is off.

### 2b. Routes with NO auth dependency at all (58 total) — tenant is 100% caller-supplied, unverified

**Methodology note (see full limitation in Verification notes below):** this
table was hand-built by grepping each route module for `tenant_id` and
`require_api_key`. On review, two `incidents.py` routes (`/statistics`,
`/recurring-issues` — both take `tenant_id: str = Query(...)`, same shape as
every other row here) were missed on the first pass and are corrected into
the `incidents.py` row below, moving the total from 56 to 58. That the miss
happened on an ordinary `Query(...)` case, not an exotic one, is itself the
argument for not trusting a grep-based scan as exhaustive — see the
phase-3 instruction at the end of this document.

| File | Routes | Tenant source |
|---|---|---|
| `anomalies.py` | GET `/api/anomalies/groups`, `/episodes`, `/enriched` | caller (query `tenant_id`) |
| `backfill.py` | POST `/api/backfill/scores` | caller (body `tenant_id`) |
| `bootstrap.py` | POST `/api/v1/bootstrap/run` | caller (form `tenant_id`) |
| `gas_anomalies.py` | GET `/api/gas/summary`, `/api/gas/readings` | caller (query `tenant_id`) |
| `inference.py` | POST `/api/inference`, `/api/inference/upload`, `/api/inference/upload` (async variant) | caller (body/form `tenant_id`) |
| `jobs.py` | GET `/api/jobs/{job_id}`, GET `/api/jobs/{job_id}/events`, POST `/api/jobs/{job_id}/cancel`, GET `/api/jobs` | GET list: caller (query `tenant_id`); the three `{job_id}` routes have **no tenant parameter or ownership check at all** — any caller who can guess/enumerate a `job_id` (UUID) can read or cancel another tenant's job. This is a separate IDOR-shaped gap, not just a missing-`tenant_id`-binding one. |
| `learning.py` | POST `/api/learning/observe`, `/jobs/observe`, `/predict`; GET `/statistics`, `/history`, `/drift`, `/models/{tenant_id}/active-version`; **DELETE `/api/learning/models`** | caller (query/body `tenant_id`) |
| `models.py` | GET `/api/models/{tenant_id}`, `/{tenant_id}/versions`, `/{tenant_id}/training-status`; POST `/{tenant_id}/train`, `/{tenant_id}/rollback`, `/{tenant_id}/activate`; **DELETE `/api/models/{tenant_id}/versions/{model_id}`**, **DELETE `/api/models/{tenant_id}/versions`** | caller (path `tenant_id`) |
| `network_anomalies.py` | POST `/api/network/rtt-jitter`, `/iat`, `/tcp-handshake`, `/dhcp-fingerprint`, `/service-response`, `/threat/fit`, `/threat/score` | caller (body `tenant_id`, defaults to `"default"` on some models) |
| `readings.py` | GET `/api/readings/window`, `/recent`, `/anomalies-in-window`, `/bounds`, `/anomalies`, `/raw`, `/raw/exists`, `/greenhouses`; POST `/api/readings/greenhouses`; GET `/api/readings/incidents` | caller (query `tenant_id`) — **this is the route from the known-facts anonymous-GET finding, confirmed** |
| `upload.py` | POST `/api/upload`, POST `/api/upload/inject-test-anomalies`; **DELETE `/api/upload/data`**, **DELETE `/api/upload/live-data`**, **DELETE `/api/upload/inject-test-anomalies`** | caller (form/query `tenant_id`) — **confirms the known fact**: neither destructive route imports `require_api_key` at all |
| `incidents.py` (subset, 6 of 10 routes) | POST `/api/incidents/store`, GET `/api/incidents/similar`, GET `/api/incidents/suggest-fixes`, POST `/api/incidents/resolve/{incident_id}`, GET `/api/incidents/statistics`, GET `/api/incidents/recurring-issues` | caller (query/body `tenant_id`) — **`/store` and `/resolve/{incident_id}` are unauthenticated cross-tenant writes**: anyone can write an incident into any tenant, or mark any tenant's incident resolved, by naming that tenant's ID. The other 4 routes in this file (`/open`, `/nearby`, `DELETE /clear`, `/{incident_id}`) are protected — see §2a. |


`monitoring.py` (`GET /api/monitoring/metrics`) and `health.py`'s two routes are
aggregate/non-tenant-scoped and not counted above; likewise `GET /api/config/crop-profiles`
(not tenant-scoped — no `tenant_id`). `auth.py`'s `GET /api/auth/validate` is also
excluded from the 58 (it validates a key, it doesn't return tenant data) — but it
is a finding in its own right, see §2e.

### 2c. Destructive routes (DELETE / wipe / bulk-delete) — full list, authed or not

| Method | Path | `require_api_key`? | `enforce_tenant_access`? |
|---|---|---|---|
| DELETE | `/api/incidents/clear` | ✓ | ✓ |
| DELETE | `/api/upload/data` | **—** | **—** |
| DELETE | `/api/upload/live-data` | **—** | **—** |
| DELETE | `/api/upload/inject-test-anomalies` | **—** | **—** |
| DELETE | `/api/learning/models` | **—** | **—** |
| DELETE | `/api/models/{tenant_id}/versions/{model_id}` | **—** | **—** |
| DELETE | `/api/models/{tenant_id}/versions` | **—** | **—** |

**6 of 7 destructive routes have zero auth.** `/api/upload/data` (`clear_tenant_data` → `wipe_tenant_data`) matches the known fact exactly — confirmed by reading `src/api/routes/upload.py:220-233`, which has no `Depends(require_api_key)` anywhere in the file.

### 2d. Ingest/write routes a machine client (grovsen) would plausibly use

These are the routes an external pusher would call today if simply pointed at
the API. All are in §2b (no auth) except `POST /api/config/operational` (§2a, authed):

- `POST /api/upload` — CSV ingestion, the closest existing analog to a grovsen push
- `POST /api/inference/upload` / `/api/inference/upload` (async) — ingest + score
- `POST /api/incidents/store` — write an incident record into any named tenant
- `POST /api/incidents/resolve/{incident_id}` — resolve any named tenant's incident
- `POST /api/backfill/scores` — bulk historical scoring
- `POST /api/readings/greenhouses` — register a greenhouse (also matches the known fact about no `require_api_key`)

`/api/incidents/store` and `/api/incidents/resolve/{incident_id}` are called out
specifically because they are unauthenticated **cross-tenant writes today**, not
just routes that would be convenient for a future grovsen credential — any
current caller can already exploit them against any tenant.

None of these are safe to expose to an unauthenticated internet caller as-is; all take `tenant_id` as a plain caller-supplied field.

### 2e. `GET /api/auth/validate` — the one BFF-facing auth check fails open

`src/api/routes/auth.py` exists specifically so the BFF can check a presented
`X-API-Key` and branch on the JSON body instead of exception-handling a 401
(`{ok: false}` vs. `{ok: true, tenant_scope: <tenant>}`). It always answers 200
— there is no rate limit and no distinction in the HTTP status between valid,
invalid, and missing keys, so it is usable as a silent key-guessing oracle
(bounded only by `secrets.compare_digest` in `resolve_tenant_from_api_key`,
which is constant-time but does not rate-limit attempts).

More importantly today: with `AUTH_ENABLED=false` (confirmed in production,
§4), `validate_api_key` takes its `if not AUTH_ENABLED` branch and returns
`{ok: True, tenant_scope: None}` **for every input, including no key at all**
(`src/api/routes/auth.py:26-28`). This is the second half of why there is no
effective auth today: it isn't only that individual routes skip
`require_api_key` — the one component built to let the BFF verify a key before
trusting it is itself wired to say "valid" unconditionally while auth is off.
Kept out of the 58 in §2b/§2d because it's a validation endpoint, not a
tenant-data route, but it belongs in the same finding as `AUTH_ENABLED=false`.

---

## 3. Tenant determination — explicit call-out

Every route in §2b determines tenant purely from a caller-supplied
`tenant_id`/`customer_id` (query, form, path, or body field) with **no
binding to any credential**, because no credential is even requested. This
is the structural finding: **58 routes let any caller read or write any
tenant's data merely by naming that tenant's ID in the request.** The 18
routes in §2a bind correctly in code (`enforce_tenant_access` 403s on
mismatch) but that binding is inert while `AUTH_ENABLED=false`.

---

## 4. `AUTH_ENABLED` / `TENANT_API_KEYS` in production

Checked via `railway variable list --service <name> --environment production
--json`, piped directly into a script that printed only sorted key names —
raw values were never displayed or logged at any point.

**`ai-agronomist-api` service (the API):**
- `AUTH_ENABLED` — **NOT SET**
- `TENANT_API_KEYS` — **NOT SET**
- (Full variable name list, for context, contains no auth-related keys: `DATABASE_URL, LIVE_ANOMALY_THRESHOLD, LIVE_AUTOSTART_ENABLED, LOG_LEVEL, MAX_PIPELINE_CACHE_SIZE, MOCK_SCADA_ENABLED, MOCK_SCADA_SCOPES, PORT, PROFILE_LOGGING_ENABLED, RABBITMQ_URL, REDIS_CACHE_EXPIRE_SECONDS, REDIS_URL`, plus Railway-injected `RAILWAY_*` metadata vars.)

Per `src/config.py:1156-1161`, `AUTH_ENABLED` defaults to `false` and
`TENANT_API_KEYS` defaults to an empty dict when unset — so production is
running with auth **structurally off**, confirming §0's summary.

**`web` service (the BFF):**
- `API_KEY` — **SET** (name only; not inspected or printed)
- `API_URL` — SET (points at the API's private/public domain; not printed)

The BFF holds an API key server-side as designed, but it currently has no
effect because the API doesn't check it (`AUTH_ENABLED=false`).

Verified without any value exposure; no `railway variable list` output was
displayed without piping through a key-only extractor first.

---

## 5. API's Railway domain — public reachability

Domain found in repo: `railway status` output lists `ai-agronomist-api`
service at `https://ai-agronomist-api-production.up.railway.app` (also
present as `RAILWAY_PUBLIC_DOMAIN` for that service).

Two harmless unauthenticated GETs from this machine:

| Request | Result |
|---|---|
| `GET https://ai-agronomist-api-production.up.railway.app/health` | **200**, no API key sent |
| `GET https://ai-agronomist-api-production.up.railway.app/docs` | **200**, no API key sent — the interactive Swagger UI (and therefore the full OpenAPI schema) is publicly browsable |

**Verified: the API is not private to the BFF.** It is reachable directly
from the public internet, bypassing the BFF (and therefore bypassing the
one place that currently attaches an API key) entirely. Combined with
§4 (`AUTH_ENABLED=false`), every route enumerated in §2 is reachable by
anyone who finds this domain — no destructive route was called to confirm
this; `/health` and `/docs` were sufficient.

---

## 6. Existing outbound integrations (email / PDF / webhooks / external calls)

**No email and no PDF generation exist anywhere in the repo** — no
SMTP/`sendmail`/email-sender pattern, no PDF library (`reportlab`,
`weasyprint`, `pdfkit`, `fpdf`) in code or in `requirements*.txt`, no
webhook/callback send pattern. That part of the original scan stands.

**Correction: outbound HTTP calls to third parties do exist** — three files,
not one:

- `src/live/sources/hoogendoorn.py` — `HoogendoornConnector`, polls the
  LetsGrow REST API (`httpx.AsyncClient`) with a static `X-Api-Key` header.
  The key is read from `os.environ[api_key_env]` — `api_key_env` (the env
  var *name*, not the key) comes from `SourceConfig.params`.
- `src/live/sources/priva.py` — `PrivaConnector`, polls the Priva
  metrics-history API via OAuth2 client-credentials (`httpx.AsyncClient`).
  The client secret is read from `os.environ[client_secret_env]`, same
  pattern — the env var name comes from config, the secret from the
  environment, never from a request.
- `src/dashboard/services/http_utils.py` — the internal Streamlit
  dashboard's HTTP client **for calling our own API** (`api_get`/`api_post`/
  `api_delete` wrapping `requests`), not a third-party integration. Original
  characterization of this one file was correct.

**Verified: `base_url` is config-derived, not caller-derived — no SSRF today
via these connectors.** Both `HoogendoornConnector.connect()` and
`PrivaConnector.connect()` read `base_url` from `self._config.params["base_url"]`
(`SourceConfig.params`, set at connector-construction time from source
configuration, not from any HTTP request). Checked `src/api/routes/live.py`
for any caller-settable `params`/`base_url`/`source_type` field —
`grep -n "params|base_url|source_type|SourceConfig" src/api/routes/live.py`
returned nothing. No live route lets a caller influence which URL a
connector polls.

**This is the pattern phase 2 must follow for the grovsen callback:** URL
and recipient come from stored configuration, resolved server-side, never
from the inbound request payload. The repo already has one working
precedent for "secret name from config, secret value from environment,
destination URL from config, never from a request" — extend it rather than
inventing a new pattern for the grovsen return path.

No `grovsen` reference exists anywhere in the repo (code, docs, or web/) —
confirming the *return-path* work (email report, grovsen push-back) is
genuinely upcoming with nothing built yet, even though the *inbound*
outbound-HTTP pattern (hoogendoorn/priva) already exists and sets a safe
precedent to reuse.

**Conclusion:** the email-report and grovsen-callback paths in the "upcoming
context" don't exist yet, so there's no legacy anti-pattern to migrate away
from — but there IS an existing good pattern (config-derived destination) to
extend, which simplifies phase 2.

---

## 7. Minimum fix proposal

This section proposes a shape; it does not implement anything.

**Human path (web app):**
- Add a real login step to the web app (currently none exists — this is new
  work, not a fix to something broken) that establishes a server-side
  session identifying the tenant.
- BFF continues to attach the API key server-side (already correct — the
  browser never sees it), but the BFF must also confirm the session's tenant
  before proxying, and should stop trusting any caller-supplied `tenant_id`
  in the query/body — derive it from the session instead. Today the catch-all
  and both SSE routes pass tenant straight through from the request with no
  session check at all; that has to close as part of this work, not survive
  it.

**API path (all tenant-scoped routes):**
- Set `AUTH_ENABLED=true` in production. This alone reactivates the 18
  routes in §2a's correct binding logic — but 58 routes still need
  `require_api_key` + `enforce_tenant_access` added; the dependency and the
  enforcement function already exist and are unit-testable (`src/api/auth.py`),
  so this is mechanical repetition per route, not new design.
- Tenant must derive from the credential (`auth_tenant`), never trust a raw
  `tenant_id`. Where a caller-supplied `tenant_id` is still accepted for
  convenience, it must be checked against the credential's tenant via
  `enforce_tenant_access` and 403 on mismatch — exactly the existing pattern
  in §2a, applied everywhere.
- `jobs.py`'s three `{job_id}` routes need an explicit ownership check
  (job's `tenant_id` column vs. authenticated tenant) since they carry no
  tenant parameter to bind against at all today — a plain `enforce_tenant_access`
  call isn't enough there; it needs a DB lookup first.

**Credential design for two caller classes:**
- `TENANT_API_KEYS` today is a flat `tenant_id -> key` map with no caller-type
  or scope concept (`src/config.py:688-690`, `_parse_tenant_api_keys`). It
  would need a second dimension — e.g. a `client_type` (web-bff vs.
  named-integration) and/or a scope list — before a grovsen credential could
  be issued, rate-limited, and revoked independently of a tenant's own web
  session key. That's new schema, not a config tweak; flagging it because
  the current format doesn't leave room for it without a breaking change to
  `TENANT_API_KEYS`'s parsing format.
- A dedicated `/api/ingest/...` route (or router) scoped to the grovsen
  credential type, separate from the tenant-web routes, is the cleanest way
  to keep grovsen's blast radius contained to ingestion rather than every
  tenant-scoped route.

**Network exposure — recommendation:**
Make the API private to the BFF (Railway private networking / internal
domain only) and add one dedicated public ingest route for grovsen, rather
than leaving the whole API public with per-route auth as the only barrier.
Trade-off: per-route auth alone (current structural direction in §2a) is
simpler to ship incrementally and doesn't require a network topology change,
but it means a single missed route (as found 58 times over in §2b) is a
full tenant-data leak with no network-layer backstop. A private API + one
public ingest route means a routing mistake fails closed (connection refused)
instead of failing open (200 with data), which is why it's the
recommendation despite being more work — Railway's private networking
(`RAILWAY_PRIVATE_DOMAIN`, already present in both services' variable lists
per §4) appears usable for this without new infrastructure.

**Return path (constraints only):**
- Email recipient and any grovsen callback URL must come from tenant
  configuration (a new column/table, doesn't exist today — see §6), never
  from the inbound request payload — the exact anti-pattern the task
  description warns about, and one this repo has no code for yet, so it's
  cheap to avoid from day one rather than retrofit.
- Outbound payloads to grovsen should be signed (e.g. HMAC over the body
  with a per-tenant or per-integration secret) so grovsen can verify
  authenticity of what it receives back.
- Inbound grovsen payloads need a size limit (FastAPI doesn't enforce one by
  default — `src/api/main.py` middleware list in §"main.py" shows no body-size
  middleware today) and an idempotency key so retried pushes don't double-ingest.

---

## Verification notes / what was and wasn't checked

- All claims above are marked verified where a command was actually run
  against this machine, this repo, or the linked Railway project during this
  session. No claim relies on assumption beyond "the code as read describes
  the running behavior," which was cross-checked by unauthenticated GETs
  against production in §5.
- Not checked (out of phase-1 scope, and would require destructive or
  write calls that were explicitly disallowed): whether any tenant-scoped
  GET route beyond `readings/greenhouses` (already confirmed per the known
  facts) actually returns another tenant's data end-to-end in production —
  the code-level absence of `require_api_key` combined with `AUTH_ENABLED=false`
  makes this a near-certainty, not something this report independently
  reproduced beyond the one known-fact GET.
- CORS origins list in `src/api/main.py` was skimmed but not fully audited;
  flagging as unverified whether a production web origin is present there —
  out of scope for this pass but relevant to phase 2 if browser-based CSRF-style
  concerns come up once real login exists.
- **Methodology limitation — how the count moved from 56 to 58.** The route
  scan behind §2b/§2c/§2d was built by hand: grepping each module for
  `@router.` decorators and `tenant_id`/`require_api_key` occurrences, then
  transcribing the result into tables. The first pass missed two ordinary
  `tenant_id: str = Query(...)` routes in `incidents.py` (`/statistics`,
  `/recurring-issues`) on transcription, not because they used an unusual
  parameter shape — they look exactly like every other row in the table.
  That a plain grep-and-transcribe miss happened on the *easy* case is the
  reason not to trust this method as exhaustive for the harder cases (tenant
  IDs arriving via a request body field or a path parameter, which several
  routes in §2b already do — e.g. `models.py`'s path-based `{tenant_id}`,
  `network_anomalies.py`'s body-based `tenant_id`). **Phase 3 must enumerate
  routes from the running app's actual route table (`app.routes`, or the
  generated OpenAPI schema at `/openapi.json`) and cross-reference dependency
  lists programmatically, not from a grep of source text.** Treat every count
  in this document as a lower bound confirmed by two independent manual
  passes, not as a value derived from the app's own routing metadata.
