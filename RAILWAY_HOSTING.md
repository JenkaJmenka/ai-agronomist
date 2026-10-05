# Railway Hosting Guide — AI Agronomist

**Audience:** whoever deploys or operates the platform. Covers the full service
topology, the new Next.js operator UI (`web/`), step-by-step first deploy,
cutover from Streamlit, rollback, and troubleshooting.
Last updated: 2026-07-11 · Delivery status lives in `MIGRATION_PLAN.md` §0 (same directory).

---

## 1. What runs where

Everything is one Railway project (**ai-agronomist**, environment **production**).

> **`ENVIRONMENT` on `ai-agronomist-api` (security phase 2, 2026-09-28):**
> the app's own `ENVIRONMENT` var defaults to `"production"` — unset is
> treated as production, not development. This is deliberate: a deployment
> that forgets to set it must fail closed (refuse to start unless
> `AUTH_ENABLED=true`), not silently serve every tenant-scoped route to
> anyone the way an opt-in default would. Railway's own
> `RAILWAY_ENVIRONMENT_NAME` (always present on a deployed service) backs
> this up as a second, independent signal — either one alone is enough to
> require `AUTH_ENABLED=true`. **Set `ENVIRONMENT=production` explicitly on
> `ai-agronomist-api`** rather than relying on the default, and set
> `ENVIRONMENT=development` (or `=test`) on any non-production deployment of
> this service (a preview/staging environment, a personal Railway fork,
> etc.) — leaving it unset there now means production semantics, including
> the startup refusal.

| Service | What it is | Source |
|---|---|---|
| `ai-agronomist-api` | FastAPI backend (`src/api/`) — all data, inference, jobs | repo root, branch `main`, Docker (`docker/start-api.sh`, binds `::` dual-stack on `$PORT`, default 8000) |
| `ai-agronomist-worker` | Async job consumer (`src/workers/`) — portioned CSV uploads, training | repo root, branch `main` |
| `web` | **New** Next.js operator UI (`web/`) — replaces the dashboard | repo, branch `main`, root directory `web` |
| `ai-agronomist-db` | Postgres (`DATABASE_URL` for api + worker) | Railway plugin |
| `Redis` | API response cache (`fastapi-cache`) | Railway plugin |
| `RabbitMQ` (+ Web UI) | Job queue between api and worker | Railway template |

Data flow: browser → `web` (Next.js BFF injects `X-API-Key` server-side) →
`ai-agronomist-api` over Railway **private networking** (IPv6,
`*.railway.internal`) → Postgres/Redis/RabbitMQ. The browser never holds the
API key and never talks to FastAPI directly.

## 2. Tech stack (new UI)

- **Framework:** Next.js 16 App Router, React 19, TypeScript strict (no `any`)
- **Data:** TanStack Query v5 (staleTimes per `MIGRATION_PLAN.md` §4.3; job
  status never cached), zod validation at every BFF response boundary
- **UI:** Tailwind CSS v4, Radix primitives, uPlot charts (canvas),
  TanStack Table + Virtual (virtualized tables), nuqs (URL-driven state)
- **Realtime:** SSE for job progress (`/api/bff/jobs/{id}/events` relay →
  FastAPI G5 stream), automatic 3 s polling fallback
- **Tooling:** pnpm **9.15.9 via corepack** (never pnpm 11 — incompatible with
  Node 20), Biome, Vitest, Playwright (e2e + CI-enforced perf budgets)
- **Types:** generated from the committed `web/openapi.json` snapshot
  (`pnpm gen:api`; refresh with `gen:api:refresh`, which rebuilds the
  schema in-process from `src/api/main.py` — no running API needed)

## 3. Current state of the `web` service

Already done (2026-07-11, via CLI/GraphQL — no deploy has run yet):

- Service **`web`** exists (id `b0d43998-851e-4715-8723-f2c87ed89fd8`)
- **Root directory:** `web`
- **`API_URL`** = `http://${{ai-agronomist-api.RAILWAY_PRIVATE_DOMAIN}}:8000`
  (reference variable — resolves to `ai-agronomist-api.railway.internal:8000`
  and tracks the API service if it is renamed/moved)
- **`API_KEY`** = `CHANGE_ME_WHEN_AUTH_ENABLED` placeholder (harmless while
  `AUTH_ENABLED` is off on the API; see §6)
- Build/start config is **code**, not dashboard state: `web/railway.json`
  (Nixpacks, `corepack enable && pnpm install --frozen-lockfile && pnpm build`,
  start `pnpm start --port ${PORT:-3000}`, healthcheck `/api/health`) — true
  only until Railway retires Config as Code on 2026-12-01; see the sunset
  follow-up in `docs/runbooks/deploy.md`.

Not done (deliberately — a human decision): the production hostname switch.

## 4. First deploy — step by step

1. **Redeploy the API from `main` first.** The web app needs endpoints that
   shipped on `main` on 2026-07-11 (`anomalies-in-window`, `anomalies/groups`,
   `anomalies/enriched`, `jobs/{id}/events` SSE). In the Railway dashboard:
   `ai-agronomist-api` → Deployments → Redeploy (or push to `main` if
   auto-deploy is on). Verify: `curl https://<api-domain>/health` →
   `{"status":"ok"}`. (`GET /api/auth/validate` was removed in the Phase 2
   auth hardening — the BFF now sends `X-API-Key` / `X-Tenant-Id` directly on
   every request and reads the resulting 401/403 instead of pre-validating.)
2. **Repo connection — already done.** `web` is already connected to `JenkaJmenka/ai-agronomist`, branch **`main`** (root directory `web` is already set; leave it). This step does not need to be performed again; it is kept here only for historical context on how the connection was made.
3. **Watch the first build.** Expect Nixpacks to detect Node via
   `web/package.json` (`packageManager: pnpm@9.15.9`), run the build command
   from `railway.json`, then healthcheck `/api/health`. Build ≈ 2–4 min.
4. **Generate a domain.** `web` → Settings → Networking → **Generate Domain**.
   This is the service's own URL for the parallel run — the production
   hostname is untouched.
5. **Smoke-check the deployment** (all through the BFF, no key needed in the
   browser):
   - `https://<web-domain>/` renders the Home page
   - `/monitoring?tenant=tenant_1&gh=gh_A` shows charts (needs data)
   - `/anomalies` — score timeline; fault-type labels prove G4 works
   - `/jobs` — list renders; a running upload streams progress (SSE)
   - `/operations` — model registry loads (proves API_URL + private
     networking are correct)

## 5. Cutover runbook (later, human decision)

1. Run both UIs in parallel ≈ 1 week; compare error rates and data parity
   (`MIGRATION_PLAN.md` §8 P5 gates).
2. Switch the production hostname to `web` directly (Settings → Networking on `web`) — there is no `ai-agronomist-dashboard` service in production to move the domain *from* (see §1).
3. ~~Keep Streamlit deployed but demoted for one release.~~ Moot — there is no `ai-agronomist-dashboard` service in production (see §1); nothing is deployed to demote.
4. **Rollback:** not possible by moving a domain back — `ai-agronomist-dashboard` does not exist in production, so there is no dashboard deployment to roll back to. No data migration is involved at any point; both UIs read the same API.
5. ~~Decommission `ai-agronomist-dashboard`.~~ Moot — that service does not exist in production (see §1); there is nothing left to decommission.

## 6. Environment variables (web service)

| Var | Value | Notes |
|---|---|---|
| `API_URL` | `http://${{ai-agronomist-api.RAILWAY_PRIVATE_DOMAIN}}:8000` | Private IPv6 host; port 8000 is `start-api.sh`'s default. If the API service defines `PORT` explicitly, change to `:${{ai-agronomist-api.PORT}}`. |
| `API_KEY` | placeholder until auth is on | When `AUTH_ENABLED=true` on the API, set to the raw key for a `caller_type=web` entry in its `API_CREDENTIALS_JSON` (see `src/api/scripts/generate_api_key.py`). Server-side only — **never** `NEXT_PUBLIC_*`. |
| `PORT` | injected by Railway | Bound explicitly by the start command. |
| `AUTH_SECRET` | **not yet set** | New (security phase 2) — Auth.js session/cookie signing key. Generate with `npx auth secret`. Server-side only. |
| `AUTH_DATABASE_URL` | **not yet set** | New (security phase 2) — points at the **existing** `ai-agronomist-db` Postgres (same instance as the API's `DATABASE_URL`, not a second plugin). Auth.js's users/sessions/verification-token tables plus `user_tenants` live in a dedicated **`auth` schema** (not `public`), applied by hand from `web/lib/auth/schema.sql` — `public` stays exclusively the FastAPI app's alembic-managed schema so `alembic revision --autogenerate` never sees these tables. `web`'s pool sets `search_path=auth` (`web/lib/auth/db.ts`); `src/` never reads this schema. |
| `AUTH_RESEND_KEY` | **not yet set** | New (security phase 2) — Resend API key for magic-link email (name matches Auth.js's own `AUTH_{PROVIDER}_KEY` convention, not a made-up name). Outside production, `web/lib/auth/emailProvider.ts` mocks the sender (logs the link) instead of calling Resend, so this can stay unset until cutover. **Never** `NEXT_PUBLIC_*`. |
| `AUTH_RESEND_FROM` | **required**, no default | New (security phase 2) — the from-address for magic-link email. There is no fallback: `web/lib/auth/emailProvider.ts` throws rather than send from an address this project does not own, so magic-link login fails once `AUTH_RESEND_KEY` is set but this is not. Resend requires the sending domain in this address to be verified in the Resend account (add the SPF/DKIM DNS records Resend provides for that domain) before it can deliver to arbitrary recipients — until that's done, Resend only allows sending to the account owner's own verified address via its shared `onboarding@resend.dev` sender, which is not usable for real operator logins. No Auth.js convention exists for this var name; chosen to mirror `AUTH_RESEND_KEY`. |

## 7. CI

`.github/workflows/web-ci.yml` runs on every push/PR touching `web/**`:
biome lint → tsc → vitest → next build → Playwright e2e including
**perf budgets that fail the build** (anomalies cold load < 2 s, filter apply
< 500 ms, channel toggle < 300 ms with zero refetches). E2E is hermetic — the
Playwright web server points `API_URL` at a dead port and every BFF call is
mocked, so CI needs no backend. `PostgreSQL Integration Tests` covers `main`.

**Railway does not wait for CI.** Railway builds and deploys on every push to `main` without gating on GitHub Actions — in practice it can start building before the CI workflows are even queued, so a deploy can reach production and succeed while CI is still running or has failed. All three repo-backed services (`ai-agronomist-api`, `ai-agronomist-worker`, `web`) redeploy on every push to `main`, including docs-only changes.

## 8. Troubleshooting

- **502 from every page, `{"detail":"Upstream API unreachable"}`** — the BFF
  can't reach the API: check `API_URL` resolves (Variables tab shows the
  resolved value), the API service is up, and it still binds `::` (IPv4-only
  binds are invisible over Railway's IPv6 private network).
- **Anomalies/jobs pages 404 on data calls** — the API is running an old
  `main` without G2–G5; redeploy it (step 4.1).
- **Job progress frozen** — SSE stream dropped; the UI auto-falls back to 3 s
  polling (badge says "live stream unavailable, polling"). Check the API
  service logs for the `/events` route.
- **Build fails resolving pnpm** — the lockfile is pnpm-9; ensure the build
  command keeps `corepack enable` (pnpm 11 is incompatible with Node 20 here).
- **401s after enabling auth** — set the real `API_KEY` on `web` (§6) and
  redeploy it.

## 9. Local development & verification

```bash
# Backend (repo root, main):
venv/bin/python -m uvicorn src.api.main:app --port 8000

# Frontend (web/, fe/nextjs-migration):
corepack pnpm install
API_URL=http://localhost:8000 corepack pnpm dev     # dev server
corepack pnpm lint && corepack pnpm typecheck && corepack pnpm test \
  && corepack pnpm build && corepack pnpm exec playwright test   # all 5 gates
```
