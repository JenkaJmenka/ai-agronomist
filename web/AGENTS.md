<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# AI Agronomist — web/ (Next.js operator UI)

Next 16 App Router frontend replacing the Streamlit dashboard (`src/dashboard/`).
The FastAPI backend stays; the browser only ever talks to the **BFF** under
`app/api/bff/` which injects `X-API-Key` server-side. Full context:
`../MIGRATION_PLAN.md` (§0 = delivery status, §7 = design tokens, §4.3 = cache TTLs).

## Commands (run from web/; pnpm 9.15.9 via corepack — never pnpm 11)

- `corepack pnpm lint` / `lint:fix` — biome
- `corepack pnpm typecheck` — tsc strict, no `any`
- `corepack pnpm test` — vitest unit (lib/ + hook logic)
- `corepack pnpm build` — production build (also feeds e2e)
- `corepack pnpm exec playwright test` — e2e + perf budgets (needs the build)
- `corepack pnpm gen:api` — regenerate `lib/api-types.gen.ts` from the committed
  `openapi.json`; `gen:api:live` rebuilds `openapi.json` in-process from
  `src/api/main.py` via `scripts/dump_openapi.py` (no running server needed),
  then runs `gen:api`. **Never hand-edit `openapi.json` or `api-types.gen.ts`
  — run `pnpm gen:api:live`.** CI fails the build on drift between either file
  and what `scripts/dump_openapi.py` + `gen:api` would regenerate.

All five gates must be green before every push; CI (`.github/workflows/web-ci.yml`)
enforces the same set including the §1.2 perf budgets in `test/e2e/perf.spec.ts`.

Local Git hooks (repo root, after `corepack pnpm install` once):

- **pre-commit** — `lint-staged` runs biome `--write` on staged `web/**/*.{ts,tsx,js,jsx,json}`
- **pre-push** — `pnpm lint` + `typecheck` + `test` inside `web/` (e2e remains CI-only)

Do not use `--no-verify` unless explicitly requested. Agent skill:
`.cursor/skills/web-quality-gates/SKILL.md`.

## Architecture map

- `app/<page>/page.tsx` — thin RSC shells; each renders a client view from features/
- `app/api/bff/[...path]/route.ts` — generic proxy to `${API_URL}` (key injection)
- `app/api/bff/jobs/[id]/events/route.ts` — SSE relay (job progress, G5)
- `features/<domain>/{components,hooks,api}/` — one folder per page domain:
  monitoring (also drives climate/gas via `channelFilter`), anomalies, thresholds,
  metrics, operations, jobs, feedback, shell, home
- `components/ui/` — primitives (Button, Card, Chip, Skeleton, DataTable
  [virtualized >100 rows], DateRangeControls, TwoStepConfirm)
- `lib/` — severity/FAULT_LABELS (single source of truth §7.2), query staleTimes
  (§4.3), upload-validator (client CSV pre-check), useScope (tenant/gh from URL),
  api-types.gen.ts (generated — never edit, biome-ignored)

## Conventions that must not regress

- **URL is the state**: tenant/gh/from/to/threshold/channel live in the query
  string (nuqs). Reload must reproduce the exact view.
- **Never cache job status** (staleTime 0); SSE first, polling fallback.
- **Two-step confirm** on every destructive/committing action (uploads, clear
  data, model delete) — first click arms, second fires.
- **Zod at every BFF response boundary**; loose() schemas, throw on !res.ok.
- **e2e mocks all `/api/bff/**` routes** (`page.route`) — tests never hit a real
  backend. Perf budgets fail the suite: cold < 2 s, filter < 500 ms,
  channel toggle < 300 ms with zero refetches.
- Loading + error + **explicit empty state** on every data component;
  one component per file, ≤300 lines, no `any`.

## Deploy state (2026-07-11)

Railway service `web` exists in project `ai-agronomist` (root directory `web`,
`API_URL` reference var → api service, `API_KEY` placeholder). Repo not yet
connected — first deploy and the hostname cutover are manual decisions
(see README "Deploy on Railway" + MIGRATION_PLAN §0.3).
