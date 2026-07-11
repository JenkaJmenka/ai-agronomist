# AI Agronomist — Frontend Migration & Redesign Master Plan

**Streamlit → Next.js (App Router)**
Author: Architect · Date: 2026-07-10 · Status: **EXECUTED — P0–P5 shipped, cutover pending**
Execution branch: `fe/nextjs-migration` (updated 2026-07-11; see §0 for delivery state)

> The FastAPI backend under `src/api/` **stays**. Next.js becomes a new frontend
> that consumes it. Streamlit (`src/dashboard/`) stays alive during a parallel run
> and is retired only at cutover (P5). This document is opinionated: one option is
> chosen per decision, with rejected alternatives noted in one line.

---

## 0. Execution status (as of 2026-07-11)

All build phases are **complete, gated, live-verified, and pushed**. What remains
is the production cutover itself (§0.3).

### 0.1 Delivered

| Item | State |
|---|---|
| P0 scaffold + design system + BFF + Playwright smoke | ✅ shipped |
| P1 Monitoring / Climate / Gas read views (G1 downsampled, uPlot, URL windows) | ✅ shipped |
| P2 Anomaly Detection flagship (score timeline, tables, URL filters) | ✅ shipped |
| P3 Thresholds preview slider + System Metrics islands | ✅ shipped |
| P4 Upload & Operations (validated CSV upload w/ two-step confirm, model registry, inject/remove synthetic, clear data) + Job Monitoring (SSE via BFF relay, polling fallback, terminate) | ✅ shipped |
| P5 Operator Feedback (optimistic verdict w/ rollback) | ✅ shipped |
| P2/P1 follow-ups: G4 fault labels in tables, G3 server grouping ≥500 rows, virtualized DataTable (cap lifted), channel-aware Monitoring jump, lockfile-warning fix | ✅ shipped |
| Backend gaps G1–G5, G7, G9 on `main` (additive; Streamlit untouched) | ✅ shipped |
| OpenAPI snapshot (`web/openapi.json`) + generated types (`web/lib/api-types.gen.ts`) | ✅ committed |
| CI: `.github/workflows/web-ci.yml` — lint / typecheck / vitest / build / Playwright | ✅ active on `web/**` |
| §1.2 perf budgets asserted in CI (`web/test/e2e/perf.spec.ts`): cold < 2 s, filter < 500 ms, toggle < 300 ms + zero refetch | ✅ failing-gate |
| Railway config as code (`web/railway.json`, README deploy section) | ✅ committed |

### 0.2 Verification

- Five gates green at every push; 26 Playwright e2e (all BFF calls mocked — e2e
  never touches a real backend).
- Live end-to-end verification (2026-07-11, local uvicorn + production build):
  G2/G3/G4/G7 with real seeded data, G5 SSE stream (progress mid-stream,
  keep-alives, terminal close) direct **and** through the BFF relay, browser pass
  over all five migrated pages, multipart CSV upload through the BFF. Zero code
  changes needed.

### 0.3 Remaining for cutover (manual, dashboard-level)

1. Redeploy `ai-agronomist-api` from `main` (it needs G2–G5/G7 before the web
   service's anomalies/jobs pages work against prod).
2. Railway `web` service **exists and is configured** (root directory `web`,
   `API_URL` reference var → `ai-agronomist-api.railway.internal:8000`, `API_KEY`
   placeholder). Last step: connect the GitHub repo (branch `fe/nextjs-migration`)
   — this triggers the first deploy to the service's own domain.
3. Set a real `API_KEY` when `AUTH_ENABLED` is turned on server-side.
4. 1-week parallel run, error-rate parity check, then the hostname switch
   (rollback = flip the route back to the still-warm Streamlit service).

---

## 1. Executive summary & goals

### 1.1 Why migrate
The current operator UI is a Streamlit app (`src/dashboard/`, ~50 Python modules,
~23 shared components). Streamlit's execution model re-runs the entire page script
top-to-bottom on **every** interaction. Combined with two full-resolution Plotly
charts and a 105k-score upload envelope on first paint, this produces the measured
production pain:

| Symptom (live benchmark) | Measured | Root cause in Streamlit model |
|---|---|---|
| Cold page render | 10–12 s | Full script rerun + 2 full-res Plotly charts + 105k-row envelope on first paint |
| Wide-window (50k pts) graphical rerun | 4+ min | Full Plotly redraw server-side; same data as a table = 0.6 s |
| Monitoring sensor-channel toggle | 35–45 s | Checkbox = full server rerun (recompute + full chart redraw) |
| Any interaction | full server rerun | Streamlit re-executes the page script every time |
| Interrupting a heavy rerun | loses filter state | State lives in `st.session_state`, wiped on interrupt |

A React/Next.js SPA-with-server-components splits **render** (client, instant) from
**data fetch** (cached, incremental), so a channel toggle never touches the server
and never refetches data.

### 1.2 Performance targets (acceptance budgets)
- **Cold load TTI < 2 s** (was 10–12 s) — RSC shell streams instantly; charts hydrate progressively.
- **Filter apply < 500 ms perceived** — URL-driven; server returns a downsampled window; skeletons cover the gap.
- **Chart channel toggle < 300 ms, client-side, zero refetch** — toggling a trace is a client state change over already-fetched data.
- **Zero state loss** — all filter/window/view state lives in the URL; reload or interrupt is lossless.
- **Wide-window (50k raw pts) render < 1 s** — server downsamples to ≤2 k points per trace (LTTB); uPlot renders on canvas.

### 1.3 UX goals
Functional, lightweight, minimalistic, maximum intuitiveness and interactivity.
Skeletons not spinners; optimistic UI for feedback actions; URL-driven filters that
survive reload; client-side chart interaction (brush-zoom, channel toggle) with no
server round-trip; consistent severity semantics across every anomaly surface.

---

## 2. Page inventory & rendering methodology

Legend — **RSC** = React Server Component (rendered on the Next server, zero client JS);
**Client island** = a `"use client"` interactive subtree; **CSR** = client-rendered
data fetching (TanStack Query); **Streaming** = RSC + `<Suspense>` progressive flush.

The App-Router default is *server components everywhere*; we opt into client islands
**only** where there is genuine interactivity. Data that is user-/tenant-scoped and
volatile is fetched client-side through TanStack Query (so it is never baked into an
RSC cache that would leak across tenants); truly static chrome is RSC.

### 2.1 Rendering matrix

| # | Page (Streamlit source) | Strategy | Justification |
|---|---|---|---|
| 0 | Home (`home.py`) | **RSC** (static) + tiny client nav | No per-user data; pure landing chrome. Ship as static RSC, near-zero JS. |
| 1 | Monitoring (`1_Monitoring.py`) | **RSC shell + client chart island + CSR windowed data** | Bounds fetched in RSC (fast, cacheable); the combined multi-sensor chart + data table are client islands reading a server-downsampled window. Channel toggle stays client-side → fixes the 35–45 s toggle. |
| 2 | Climate Data (`2_Climate_Data.py`) | **RSC shell + client chart island (grid/combined) + CSR** | Same data shape as Monitoring; adds per-sensor anomaly-count cards + events table (client islands over the same fetched window). |
| 3 | Gas Monitoring (`3_Gas_Monitoring.py`) | **RSC shell + client chart island + CSR** | Reads `/api/gas/summary` + `/api/gas/readings`; gas stack/combined charts are client islands. |
| 4 | Anomaly Detection (`2_anomalies.py`) — **flagship** | **RSC shell + streaming Suspense + client islands (upload, score-timeline chart, anomaly tables) + CSR windowed anomalies** | The heaviest page. RSC renders chrome + metrics skeleton; the score-timeline chart, top-anomalies table, table-view grouping, and upload panel are separate client islands. Windowed anomaly data is fetched client-side with a server-downsampled + score-merged endpoint. Filters (window, threshold, fault-type, view mode) are **URL params** → fixes state loss. |
| 5 | Thresholds (`3_thresholds.py`) | **RSC shell + CSR window fetch + pure-client slider preview** | Fetch the scored window once; the slider preview (count at threshold) is 100% client compute over the fetched scores — no server round-trip per drag. Saved threshold → URL/localStorage, not `st.session_state`. |
| 6 | System Metrics (`4_metrics.py`) | **RSC shell + CSR data-health island + CSR pipeline-metrics island** | Data-health reads the scored window (client); pipeline metrics poll `/api/monitoring/metrics` with a 30 s staleTime. Two independent islands so one slow call never blocks the other. |
| 7 | Upload & Operations (`5_upload_operations.py`) | **CSR** (client page) | Almost entirely mutations (upload/train, inject/remove synthetic, clear data) + model registry. Interactivity-dominant; no SSR value. |
| 8 | Job Monitoring (`6_job_monitoring.py`) | **CSR + SSE (fallback polling)** | Live job progress. Replace the 5 s Streamlit `fragment(run_every=)` poll with an SSE stream (`GET /api/jobs/{id}/events`); fall back to TanStack Query `refetchInterval` if SSE unavailable. Never cached. |
| 9 | Operator Feedback (`99_Operator_Feedback.py`) | **RSC shell + client islands (review queue, incident detail, label form) + optimistic mutations** | Queue + incident detail are islands; the label/verdict form uses optimistic UI so the verdict shows instantly and reconciles on server response — fixes the "st.success then st.rerun discards it" bug (COR-1). |

### 2.2 Cross-cutting rendering rules
- **No ISR/SSG for data pages** — all data is tenant-scoped and mutable; static
  generation would leak or stale. SSG applies only to Home and pure marketing/help chrome.
- **Streaming (`loading.tsx` + `<Suspense>`)** on every data page so the shell paints
  < 2 s while data streams in behind skeletons.
- **Route Handlers as BFF** (`app/api/**`) proxy the FastAPI backend so the API key
  never reaches the browser (see §11 auth).

---

## 3. Tech stack (pinned choices)

| Concern | Choice | Version | Why | Rejected (one-liner) |
|---|---|---|---|---|
| Framework | **Next.js App Router** | 15.x | RSC + streaming + Route Handlers give the render/fetch split that kills the rerun tax. | Remix (smaller RSC story); Vite SPA (loses server components + BFF). |
| Language | **TypeScript, `strict: true`** | 5.x | Repo CLAUDE.md forbids `any`; strict + zod at boundaries. | JS (violates guardrails). |
| UI runtime | **React** | 19.x | Ships with Next 15; Actions + `useOptimistic` for feedback UI. | — |
| Styling | **Tailwind CSS v4** + **Radix UI primitives** | TW 4.x / Radix latest | Lightweight, utility-first, no runtime CSS-in-JS cost; Radix gives accessible headless dialog/select/tabs/slider/tooltip. | MUI/Chakra (heavy, opinionated theme); shadcn wholesale (we cherry-pick Radix instead). |
| Charts (large time-series) | **uPlot** (thin React wrapper) | 1.6.x | Canvas renderer, handles 10k–100k points at 60fps; tiny (~40 kB). Best fit for the 50k-point series after server downsampling. | Recharts (SVG, dies > ~2k pts); ECharts (heavier ~1 MB, canvas but overkill); visx (low-level, more build cost); Plotly (what we're leaving — SVG/heavy). |
| Data layer | **TanStack Query v5** | 5.x | Per-key staleTime, request dedupe, background refetch, `refetchInterval` for jobs, optimistic mutations. | SWR (thinner mutation/optimistic story); raw fetch (no cache/dedupe). |
| Tables | **TanStack Table v8** + **TanStack Virtual** | 8.x | Headless, sort/group/filter in-client; virtualization renders 50k rows in 0.6 s (matches the benchmark's "table is fast" finding). | AG-Grid (heavy, licensed features); MUI DataGrid (bundle). |
| URL state | **nuqs** | latest | Type-safe URL search-param state — the mechanism that makes filters survive reload/interrupt. | Hand-rolled `useSearchParams` glue (error-prone). |
| Ephemeral state | **Zustand** | 5.x | Only for non-URL UI state (sidebar tenant/greenhouse selection, in-flight upload tracker). | Redux (overkill); Context-only (re-render storms). |
| Forms | **React Hook Form** + **Zod** | RHF 7.x / Zod 3.x | Label form, inject-anomalies form; zod schema shared with API-response validation. | Formik (heavier, slower). |
| Dates | **date-fns** (+ `date-fns-tz`) | 3.x | Tree-shakeable; the app is timezone-sensitive (readings carry tz). | Moment (deprecated, heavy); Day.js (weaker tz). |
| Testing | **Vitest** + **Testing Library**; **Playwright** (e2e + visual regression) | latest | Vitest fast unit; Playwright covers e2e, visual snapshots, and perf-budget assertions in CI. | Jest (slower); Cypress (heavier than Playwright for this). |
| Lint/format | **Biome** | latest | Single fast tool for lint + format; zero-config parity across the FE package. | ESLint+Prettier (two tools, slower) — acceptable fallback if Biome gaps on a Next rule. |
| Package manager | **pnpm** | latest | Fast, disk-efficient, strict peer deps. | npm (slower installs). |

**Downsampling library:** LTTB (Largest-Triangle-Three-Buckets) implemented
server-side in Python (§4/§5); the client receives ≤2 k points and never downsamples.

---

## 4. Caching strategy (layer by layer)

The guiding rule: **cache aggressively for shape-stable reference data, never for
job status or mutations, and always scope by tenant so nothing leaks.**

### 4.1 Server downsampling contract (the biggest win)
Add a `max_points` query param to the windowed readings endpoints. The server
applies **LTTB** per numeric trace and **always preserves** rows flagged as anomalies
(scored above threshold) so downsampling never hides a spike.

```
GET /api/readings/window?...&max_points=2000&include_scores=true
→ { rows: [...≤2000 downsampled + all anomaly rows...], count, downsampled: true, original_count }
```

Consequence: the 50k-point 4-minute case becomes a ≤2 k-point sub-second render.

### 4.2 Next.js fetch cache / `revalidate` tags (per endpoint class)

| Endpoint class | Next cache | revalidate | Tag |
|---|---|---|---|
| `/api/readings/greenhouses` | `force-cache` | 300 s | `greenhouses:{tenant}` |
| `/api/readings/bounds` | `force-cache` | 120 s | `bounds:{tenant}:{gh}` |
| `/api/readings/window`, `/recent`, `/anomalies`, `/raw` | **no-store** (client-fetched via TanStack) | — | — |
| `/api/gas/summary`, `/gas/readings` | `revalidate` | 60 s | `gas:{tenant}:{gh}` |
| `/api/monitoring/metrics` | no-store (client, 30 s staleTime) | — | — |
| `/api/jobs/**` | **no-store, never cache** | — | — |
| feedback / upload / model mutations | **no-store** + `revalidateTag` on success | — | — |

Mutations call `revalidateTag('bounds:…')` / `revalidateTag('greenhouses:…')` after
upload/clear so reference data refreshes without a blanket purge.

### 4.3 Client-side TanStack Query `staleTime` (per data type)

| Data type | staleTime | refetch | Notes |
|---|---|---|---|
| bounds | 5 min | on mount | shape-stable per (tenant, gh) |
| greenhouses list | 5 min | on mount | changes only after upload |
| windowed readings | 2 min | on window change | keyed by `[tenant, gh, start, end, maxPoints]` |
| anomalies-in-window | 60 s | on filter change | keyed incl. threshold |
| active model version | 60 s | on mount | invalidated on activate/train |
| pipeline metrics | 30 s | `refetchInterval: 30s` | process-wide counters |
| **job status** | **0 (always stale)** | **SSE / `refetchInterval: 2–5s`** | never trust cache |
| incidents / open incidents | 60 s | on mount | |

`placeholderData: keepPreviousData` on window/anomaly queries so panning the
timeline shows the previous frame under a skeleton instead of blanking (perceived
< 500 ms).

### 4.4 HTTP caching headers the FastAPI side should add
The readings endpoints already carry a server-side Redis `@cache`
(`REDIS_CACHE_EXPIRE_SECONDS`). Add matching HTTP headers so the Next fetch layer and
any CDN honor them:
- `bounds`, `greenhouses`: `Cache-Control: private, max-age=120, stale-while-revalidate=300`
- `window`/`recent`/`anomalies` (downsampled): `Cache-Control: private, max-age=30, stale-while-revalidate=120`
- `jobs`, mutations, `metrics`: `Cache-Control: no-store`

### 4.5 Content-addressed upload inference cache (keep the concept)
Streamlit's Layer-1 cache (`inference_cache.py`, sha256(file_bytes) → results) is a
good idea and should be **preserved server-side** — expose it via the existing
`/api/inference/upload` path (server already writes the envelope). The FE does not
re-implement a disk cache; it relies on TanStack Query + the server cache.

### 4.6 What NEVER to cache
Job status/progress; feedback submissions; label verdicts; clear-data/inject
mutations; the SSE job stream. These are always live or one-shot.

---

## 5. API gap analysis (FastAPI additions for the new FE)

> **Status:** G1–G5, G7, G9 shipped on `main` (2026-07-11); G6 confirmed already
> in place; G8 satisfied by the BFF-only design (no CORS opened, key server-side).
> Path params now uniformly `tenant_id`; body fields keep the documented
> `customer_id` name — renaming those would break live Streamlit callers.

The backend already exposes a clean read surface (`/api/readings/*`, `/api/jobs/*`,
`/api/inference/*`, `/api/feedback/*`, `/api/incidents/*`, `/api/models/*`,
`/api/gas/*`, `/api/monitoring/metrics`, `/api/upload/*`). Gaps to close for the FE:

| # | Gap | Proposed route signature | Owner | Notes |
|---|---|---|---|---|
| G1 | Server downsampling | add `max_points: int = Query(None, ge=100, le=10000)` + `include_scores: bool` to `GET /api/readings/window` and `/recent`; response adds `downsampled`, `original_count` | backend | LTTB per trace; **preserve anomaly rows**. Biggest perf lever. |
| G2 | Anomalies-in-window (pre-filtered) | `GET /api/readings/anomalies-in-window?tenant_id&greenhouse_id&start&end&threshold&max_points` → `{ rows, anomaly_count, total_count }` | backend | Moves `load_anomalies_for_window` merge+filter server-side (currently client/Streamlit). |
| G3 | Aggregated anomaly groups | `GET /api/anomalies/groups?tenant_id&greenhouse_id&start&end&threshold&group_by=date|type|sensor` → `{ groups: [{key, count, top_score, sample_ids}] }` | backend | Powers the anomalies "Table view" grouping without shipping all rows. |
| G4 | Enriched top anomalies | `GET /api/anomalies/enriched?tenant_id&greenhouse_id&start&end&limit` → rows with `fault_type`, `root_cause`, attribution | backend | Replaces `load_enriched_top_anomalies` (incident join + fault classify). |
| G5 | Job progress SSE | `GET /api/jobs/{job_id}/events` → `text/event-stream` emitting `{status, progress_pct, completed_portions, total_portions}` until terminal | backend | Replaces the 5 s poll fragment; big UX win on long uploads. |
| G6 | Jobs list already lightens result | (exists — `_lighten_result` in `jobs.py`) | — | Confirm the FE never requests heavy arrays from the list endpoint. |
| G7 | Auth/me + key validation | `GET /api/auth/validate` (X-API-Key) → `{ok, tenant_scope?}` | backend | Lets the BFF verify the key server-side once. |
| G8 | CORS / same-origin | ensure FastAPI CORS allows the Next origin **or** all FE traffic proxied via Next Route Handlers (preferred — no CORS, key hidden) | backend/devops | Prefer BFF proxy. |
| G9 | Consistent camelCase or documented snake_case | publish an OpenAPI schema the FE generates types from (`openapi-typescript`) | backend | Avoids hand-drift between `tenant_id`/`customer_id` (COR-1 in prior review). |

**Non-breaking**: G1/G2 are additive query params; existing Streamlit callers keep
working during the parallel run.

---

## 6. Component architecture & best practices

### 6.1 Folder-by-feature structure (per repo CLAUDE.md)
```
frontend/
  app/
    (shell)/layout.tsx            # RSC root shell: sidebar, tenant/gh context
    page.tsx                      # Home (RSC)
    monitoring/{page,loading,error}.tsx
    climate/…  gas/…
    anomalies/{page,loading,error}.tsx
    thresholds/…  metrics/…  operations/…  jobs/…  feedback/…
    api/                          # BFF Route Handlers (proxy FastAPI, inject key)
      readings/[...]/route.ts
      jobs/[id]/events/route.ts   # SSE relay
  features/
    monitoring/{components,hooks,api,types}/
    anomalies/{components,hooks,api,types}/
    …one folder per domain…
  components/ui/                   # design-system primitives (Button, Chip, Skeleton…)
  lib/{api-client,query,downsample,tz,format}.ts
  test/                           # Vitest setup, Playwright specs
```
Rules (from CLAUDE.md guardrails): one component/class per file, file name matches
export, **max 300 lines/file**, group by feature not type, barrels for re-export only.

### 6.2 Streamlit component → React component map (~23 → React)

| Streamlit component | React component (feature) | Server/Client |
|---|---|---|
| `global_sidebar.py` | `AppSidebar` (tenant/gh select, API-key) | Client island (writes URL + Zustand) |
| `charts.py` (plot_combined_sensors, grid, gas) | `SensorTimeSeriesChart`, `SensorGridChart`, `GasStackChart` (uPlot) | Client |
| `sequence_chart.py` | `SequenceChart` | Client |
| `anomaly_table.py` / `anomaly_table_view.py` / `anomaly_table_view_style.py` | `AnomalyTable`, `AnomalyGroupedTable` (TanStack Table+Virtual) | Client |
| `sticky_table.py` / `data_table.py` | `DataTable` (virtualized, sticky header) | Client |
| `table_export.py` | `useCsvExport` hook + `ExportButton` | Client |
| `timeline.py` / `timeline_utils.py` | `DateRangeControls`, `useTimelineWindow` (URL-driven) | Client island |
| `inference_results.py` | `UploadResultsPanel`, `ScoreTimeline`, `ColdRunTables` | Client |
| `model_registry.py` | `ModelRegistryPanel` | Client |
| `learning_progress.py` | `LearningProgressCard` | RSC (data) + client bar |
| `diagnosis_card.py` | `DiagnosisCard` | RSC |
| `events_table.py` | `EventsTable` | Client |
| `sensor_columns.py` / `color_manager.py` | `lib/sensors.ts`, `lib/colors.ts` (pure utils) | shared |
| `data_availability.py` | `NoSensorDataHint` (empty state) | RSC |
| `monitoring_jump.py` | `useMonitoringJump` (URL params) | Client |
| `services/api_client.py` | `lib/api-client.ts` + generated types | shared |
| `services/anomalies_page_data.py` | `features/anomalies/api/*` + query hooks | shared |
| `services/inference_cache.py` | (server-side; FE uses TanStack Query) | — |
| `services/async_upload.py` | `useUploadJob` (SSE) | Client |
| `services/window_data.py` / `anomaly_events.py` | `features/*/api` + `lib/downsample-guard` | shared |
| `services/review_queue.py` | `features/feedback/api` | shared |
| `services/tz_normalize.py` | `lib/tz.ts` | shared |
| `upload_validator.py` | `lib/upload-validator.ts` (zod) — keep client-side pre-validation | shared |

### 6.3 Conventions
- **Server components by default**; add `"use client"` only for interactivity.
- Every data component ships **three states**: `loading.tsx`/skeleton, `error.tsx`
  boundary, and an explicit **empty state** (mirroring `render_no_sensor_data_hint`).
- Co-located tests: `Component.tsx` + `Component.test.tsx`.
- Naming per CLAUDE.md: `handleX`/`onX` handlers, `is/has/can/should` booleans,
  `UPPER_SNAKE_CASE` consts, descriptive names (no `data`/`temp`).
- **Validate every API response with zod** at the boundary (guardrail: validate
  external input) — types generated from OpenAPI, refined with zod schemas.
- Accessibility baseline: Radix primitives (focus-trap, ARIA), keyboard-navigable
  tables, `prefers-reduced-motion` honored, color-contrast AA, severity conveyed by
  **text + icon**, never color alone.

---

## 7. Design system

Brief: **functional, lightweight, minimalistic, maximum intuitiveness & interactivity.**
This is an internal operator tool — density and speed beat decoration.

### 7.1 Design tokens
```
Color (semantic, light/dark via CSS vars):
  --bg, --bg-elevated, --border, --text, --text-muted
  --accent (brand green #16a34a)         # "Agronomist"
  Severity:
    --sev-normal   #16a34a  (green)      # score < threshold
    --sev-warning  #f59e0b  (amber)      # non-LSTM / single-sensor flag
    --sev-lstm     #ff8c00  (orange)     # LSTM/drift window (matches current markers)
    --sev-critical #ef4444  (red)        # agreement / high score
  Chart palette: 8-color categorical, colorblind-safe (Okabe-Ito).
Spacing: 4-pt scale (4,8,12,16,24,32,48).
Type: Inter; scale 12/14/16/20/24/32; tabular-nums for metrics & tables.
Radius: 6 px controls, 8 px cards. Shadow: one subtle elevation token.
```
Dark mode is first-class (operators run this on wall displays); tokens flip via a
`data-theme` attribute.

### 7.2 Severity semantics (anomaly chips) — single source of truth
Chip color + label derive from one `getSeverity(score, detector, agreement)` util so
every surface (tables, chart markers, cards) agrees. Preserve the existing meaning:
agreement→critical(red), LSTM/drift→orange, single-sensor statistical→amber,
below-threshold→normal(green). Fault-type labels reuse `FAULT_LABELS`
(spike/calibration_drift/stuck/bias_offset/accuracy_decline/unknown).

### 7.3 Layout shells
- **App shell (RSC):** left sidebar (tenant/greenhouse select, API-key, nav groups:
  Active Monitoring / Detection / Operations / Feedback), top context bar
  (tenant · greenhouse · active window), main content region with streaming Suspense.
- **Page shell:** title + context caption, filter bar (date range, threshold,
  fault-type), then content islands.

### 7.4 Interaction patterns
- **URL-driven filters** — window start/end, threshold, fault-type, view mode, active
  channels all live in the query string (`nuqs`). Reload/refresh/interrupt = lossless.
  *This directly fixes the "interrupting a rerun loses filter state" bug.*
- **Skeletons, not spinners** — every island has a shaped skeleton; `keepPreviousData`
  keeps the old frame visible under the skeleton while a new window loads.
- **Optimistic UI for feedback** — `useOptimistic`/TanStack mutation shows the verdict
  (confirm / false-positive) instantly, reconciles on server response, rolls back on
  error. *Fixes COR-1 (success toast discarded by rerun).*
- **Client-side chart interaction** — brush-zoom and channel toggle mutate client
  state over already-fetched data; **no refetch**. *Fixes the 35–45 s toggle and 4-min
  redraw.* A "load raw resolution for this zoom" action explicitly refetches a
  narrower window at higher `max_points` only when the user zooms in.

### 7.5 Chart interaction spec
- X-axis brush → updates URL window params (debounced 250 ms) → refetch at the new range.
- Legend chips toggle trace visibility client-side (instant).
- Anomaly markers overlaid as a separate series (dots for spikes, shaded vrect bands
  for LSTM/drift windows) — port `_add_anomaly_markers` semantics to uPlot plugins.
- Normalized 0–100 % multi-sensor view (matches current Monitoring) as a toggle;
  hover shows real units (°C, %, ppm, lux).

---

## 8. Phased roadmap (branch `fe/nextjs-migration`)

Each phase: **scope · deliverables · test gates · rollback.** Phases land behind the
parallel run — Streamlit stays live until P5 cutover.

### P0 — Scaffold + design system ✅ DONE
- **Scope:** Next 15 app, TS strict, Tailwind v4, Radix, TanStack Query provider,
  Biome, Vitest+Playwright, BFF proxy skeleton, design tokens, UI primitives
  (Button, Chip, Skeleton, Card, DataTable shell, DateRangeControls), app shell +
  sidebar, OpenAPI type generation, uPlot React wrapper.
- **Deliverables:** running app with Home + empty routed pages + tenant/gh sidebar
  wired to URL/Zustand; CI green.
- **Test gates:** lint/typecheck pass; Playwright smoke (app boots, nav works);
  Lighthouse TTI < 2 s on Home; visual snapshot of tokens/primitives.
- **Rollback:** branch only; nothing user-facing.

### P1 — Read-only pages (Monitoring / Climate / Gas) ✅ DONE
- **Scope:** the three monitoring pages; windowed+downsampled reads (G1), uPlot
  charts, virtualized data tables, channel toggles client-side, URL-driven window.
- **Deliverables:** feature-parity read views hitting the real API through the BFF.
- **Test gates:** channel toggle < 300 ms (Playwright perf); 50k-pt window < 1 s;
  e2e: pick window → chart+table render; visual regression vs. design.
- **Rollback:** feature-flag routes off; Streamlit remains primary.

### P2 — Anomaly Detection (flagship) ✅ DONE
- **Scope:** RSC shell + streaming; score-timeline chart island; top-anomalies +
  grouped table-view islands; upload panel (sync + async job via SSE); anomalies-in-
  window (G2), enriched (G4), groups (G3); URL filters (window/threshold/fault/view).
- **Deliverables:** flagship page at parity + the perf targets.
- **Test gates:** cold load < 2 s TTI; filter apply < 500 ms perceived; zero state
  loss on reload (e2e reloads mid-filter and asserts identical view); upload→job→
  results happy path e2e.
- **Rollback:** flag off; Streamlit anomalies page stays.

### P3 — Thresholds + Metrics ✅ DONE
- **Scope:** threshold preview (client slider over fetched scores), saved threshold in
  URL/localStorage; metrics data-health + pipeline islands.
- **Deliverables:** both pages at parity.
- **Test gates:** slider drag recomputes count client-side < 50 ms; metrics islands
  independent (one failing doesn't blank the other); e2e.
- **Rollback:** flag off.

### P4 — Upload / Jobs (async flows) ✅ DONE
- **Scope:** Upload & Operations (train, inject/remove synthetic, clear data w/ two-
  step confirm, model registry) + Job Monitoring (SSE live progress, terminate).
- **Deliverables:** all mutations wired with optimistic/confirm UX + SSE job stream.
- **Test gates:** upload→SSE progress→terminal e2e; two-step destructive confirm e2e;
  no fire-and-forget mutations (all have error handling); terminate job e2e.
- **Rollback:** flag off; Streamlit ops page stays (mutations are backend-idempotent).

### P5 — Feedback + cutover ✅ DONE except the manual cutover switch (§0.3)
- **Scope:** Operator Feedback (queue, incident detail, label form with optimistic
  verdict, learning progress, clear feedback/models); then **parallel run** both UIs,
  then **Railway routing switch** to Next as primary, Streamlit demoted to a fallback
  URL for one release, then retired.
- **Deliverables:** full parity; cutover runbook; Streamlit decommission ticket.
- **Test gates:** optimistic verdict e2e (instant + reconcile + rollback-on-error);
  full-suite visual regression; perf budgets enforced in CI; 1-week parallel-run
  error-rate parity check.
- **Rollback:** flip Railway routing back to Streamlit (kept warm) — single env/router
  change, no data migration involved.

---

## 9. Orchestration prompts (per phase)

These follow this repo's CLAUDE.md orchestrator workflow: **Bash dispatch, background
after dispatch, write state, start watcher, exit.** Paste one per phase.

### 9.1 Generic phase dispatch (fill AGENT/TASK/OUTPUT)
```bash
SYSTEM=$(awk '/^---/{c++;next} c==2' .claude/agents/AGENT_NAME.md)
TARGET_MODEL=$(awk -F': ' '/^model:/{print $2}' .claude/agents/AGENT_NAME.md | tr -d '\r ')
claude --model "$TARGET_MODEL" --print --dangerously-skip-permissions \
  --system-prompt "$SYSTEM" \
  "PHASE Pn TASK — see docs/migration/nextjs_migration_plan.md §8 Pn and §10 (your role).
Work on branch fe/nextjs-migration under frontend/. Read the plan first.
Acceptance criteria: <paste Pn test gates>.
When ALL work is complete and tests pass:
mkdir -p tasks/done
echo '{\"agent\":\"AGENT_NAME\",\"status\":\"done\",\"output\":\"OUTPUT\",\"tests_passed\":true}' > tasks/done/AGENT_NAME.json
On failure:
echo '{\"agent\":\"AGENT_NAME\",\"status\":\"failed\",\"reason\":\"...\"}' > tasks/done/AGENT_NAME.json" < /dev/null &
echo "Dispatched AGENT_NAME (PID $!) on $TARGET_MODEL"
```

### 9.2 State + watcher (after each phase dispatch)
```bash
SESSION=$(echo "${CLAUDE_SESSION_ID:-unknown}")
cat > tasks/orchestrator_state.json << STATEEOF
{ "phase": "fe-Pn", "status": "waiting",
  "waiting_for": ["frontend-engineer"],
  "dispatched_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "next_phase": {"phase":"fe-Pn+1"},
  "session_id": "$SESSION" }
STATEEOF
[[ -z "$SESSION" || "$SESSION" == "unknown" ]] && { echo "SESSION UNKNOWN — run /status"; exit 1; }
nohup bash scripts/agent-watcher.sh "$SESSION" frontend-engineer >> tasks/watcher.log 2>&1 &
echo "Watcher started (PID $!) — exiting orchestrator now"
```
Verification each resume: read `tasks/orchestrator_state.json`, read the phase output
under `frontend/`, confirm the done-signal `tests_passed:true`, confirm expected files
exist on disk; if any agent reported `failed` or a file is missing → STOP and report.

Parallel phases (e.g. P2 flagship FE + backend G1–G5 gaps) dispatch two blocks with
`&`, `waiting_for` both agents.

---

## 10. Engineer-specific prompts (ready to paste)

Common context block for every FE prompt:
```
Repo: /Users/eugenesidorov/Projects/ai-agronomist  · Branch: fe/nextjs-migration · Dir: frontend/
Stack: Next 15 App Router, TS strict, Tailwind v4 + Radix, uPlot, TanStack Query/Table/Virtual,
  nuqs, Zustand, RHF+Zod, date-fns, Biome, Vitest+Playwright, pnpm.
Backend stays: consume FastAPI via BFF Route Handlers (app/api/**) that inject X-API-Key server-side.
Design tokens (§7): severity normal #16a34a / warning #f59e0b / lstm #ff8c00 / critical #ef4444;
  brand #16a34a; 4-pt spacing; Inter; dark+light via data-theme.
Rules: server components by default, client islands only for interactivity; one component/file,
  ≤300 lines; folder-by-feature; loading+error+empty states per data component; zod at boundaries;
  URL-driven filters (nuqs); skeletons not spinners; a11y AA. Read docs/migration/nextjs_migration_plan.md.
```

### 10.1 frontend-engineer (per phase)
- **P0:** Scaffold app + design system + BFF proxy + uPlot wrapper + CI. Deliver Home
  + routed empty pages + sidebar (URL/Zustand). Gate: TTI<2s Home, smoke e2e green.
- **P1:** Monitoring/Climate/Gas read views: `SensorTimeSeriesChart`/`GridChart`/
  `GasStackChart` (uPlot), virtualized `DataTable`, `DateRangeControls` (URL), client
  channel toggles, TanStack Query hooks against G1 endpoints. Gates in §8 P1.
- **P2:** Anomaly Detection flagship — see §2 row 4 + §8 P2. Islands: `UploadPanel`
  (sync+async SSE), `ScoreTimeline`, `TopAnomaliesTable`, `AnomalyGroupedTable`,
  `ColdRunTables`. URL filters. Gates: cold<2s, filter<500ms, zero state loss.
- **P3:** Thresholds (client slider over fetched scores) + Metrics (two islands).
- **P4:** Upload & Operations (mutations + two-step confirm + model registry) + Job
  Monitoring (SSE via BFF relay). Gates in §8 P4.
- **P5:** Operator Feedback (optimistic verdict via `useOptimistic`) + cutover polish.

### 10.2 backend-engineer (API gaps §5)
```
Add, non-breaking, to src/api/routes + src/api/services (Pydantic v2, SQLAlchemy 2.x async,
tenant_id filtering, tests to 90%):
G1: max_points (LTTB, preserve anomaly rows) + include_scores on /api/readings/window & /recent.
G2: GET /api/readings/anomalies-in-window (threshold-filtered, downsampled).
G3: GET /api/anomalies/groups (group_by date|type|sensor).
G4: GET /api/anomalies/enriched (fault_type/root_cause/attribution join).
G5: GET /api/jobs/{id}/events  (text/event-stream, emit until terminal status).
G7: GET /api/auth/validate (X-API-Key).
G8: confirm CORS or document BFF-only access. G9: ensure OpenAPI schema is complete + correct
    (fix tenant_id/customer_id naming drift) so the FE generates types from it.
Add Cache-Control headers per §4.4. Keep existing Streamlit callers working (additive params only).
After writing: run smoke + full pytest, fix until green.
```

### 10.3 qa-engineer (e2e + visual + perf budgets in CI)
```
Playwright e2e per phase (§8 gates): nav/smoke; window→chart+table; upload→SSE job→results;
  reload-mid-filter asserts identical view (zero state loss); optimistic verdict (instant+reconcile
  +rollback); two-step destructive confirm. Visual regression snapshots per page (light+dark).
Perf budgets ASSERTED in CI (fail the build): Home TTI<2s; anomalies cold<2s; filter apply<500ms;
  channel toggle<300ms; 50k-pt window render<1s. Vitest unit for lib/ (downsample-guard, severity,
  tz, format, upload-validator) + hooks. Target 90% on frontend/ non-view logic.
```

### 10.4 devops-lead (Railway + CI + previews)
```
Add a second Railway service for the Next app (Node) alongside API + worker + Streamlit.
Env: API_INTERNAL_URL (FastAPI railway.internal, IPv6 — bind already ::), API_KEY (server-only, never
  NEXT_PUBLIC_*), NODE_ENV, per-env feature flags. BFF proxies API so the browser never holds the key.
CI: pnpm install/lint/typecheck/vitest/playwright + perf budgets; PR preview deploys.
Cutover (P5): route production hostname → Next; keep Streamlit on a fallback subpath one release,
  then decommission. Rollback = flip the hostname route back to Streamlit (kept warm).
```

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **API key exposure** | Never ship the key to the browser. All FastAPI calls go through Next **BFF Route Handlers** that inject `X-API-Key` server-side; browser talks only to same-origin `/app/api/**`. No `NEXT_PUBLIC_` secret. (G7/G8) |
| **Plotly → uPlot parity** | Feature-map every current chart behavior (normalized multi-sensor, anomaly markers/vrect bands, brush-zoom, hover units) to uPlot plugins in P1; visual-regression snapshots gate parity. |
| **CSV upload streaming** | Reuse the existing async upload route (`/api/inference/upload-async`) — multipart POST from the BFF; large files never block the UI; progress via SSE (G5). Keep client-side pre-validation (`upload-validator.ts`). |
| **Long-running job UX** | SSE progress (G5) with fallback polling; job state never cached; terminal states surface clear success/partial-fail/error; timeout guard mirrors current `JOB_TIMEOUT_SECONDS`. |
| **50k-point rendering** | Server LTTB downsampling to ≤2 k pts (G1) + uPlot canvas + TanStack Virtual tables. "Zoom to raw" refetches a narrow window at higher resolution on demand. |
| **State loss on interrupt** | URL-first filter state (nuqs) — reload/interrupt is lossless by construction (the exact bug being fixed). |
| **Tenant data leakage via cache** | Never cache user-scoped reads in the RSC/Next fetch cache; TanStack keys always include tenant+greenhouse; `revalidateTag` scoped by tenant. |
| **SEO irrelevance (internal tool)** | No SSG/marketing overhead; choose CSR/CSR-island freely where it wins; skip metadata/OG work — informs the "CSR is fine here" calls above. |
| **Parallel-run drift** | Keep Streamlit fully alive until P5; feature-flag Next routes; 1-week error-rate parity check before cutover; single-switch rollback (Railway hostname). |
| **Naming drift (tenant_id vs customer_id)** | Generate FE types from OpenAPI (G9); fix backend naming inconsistency flagged in prior review (COR-1) as part of the schema pass. |
| **Scope creep during migration** | Parity-first: no new features during P1–P5; redesign is visual/interaction only, not new capabilities. |

---

## 12. Appendix — current API surface consumed (verified from code)

`/api/readings`: `window`, `recent`, `bounds`, `anomalies`, `raw`, `raw/exists`,
`greenhouses`, `incidents` · `/api/jobs`: `GET /{id}`, `POST /{id}/cancel`, `GET` list ·
`/api/inference`: `inference`, `upload`, `upload-async`, `comprehensive` ·
`/api/feedback`: `mark-anomaly`, `learning-progress/{id}`, `statistics/{id}` ·
`/api/incidents`: `open`, `GET /{id}`, `clear` · `/api/models/{tenant}`: `versions`
(GET/DELETE), `activate`, `versions/{model_id}` (DELETE) · `/api/learning`:
`models/{tenant}/active-version`, `models` (DELETE) · `/api/monitoring/metrics` ·
`/api/gas`: `summary`, `readings` · `/api/upload`: `inject-test-anomalies` (POST/
DELETE), `data` (DELETE).

*End of plan. No application code written; execution occurs later on
`fe/nextjs-migration` after user approval.*
</content>
</invoke>
