# Frontend Agent Memory

## Phase 5B — Live Monitoring e2e Spec (2026-07-18)

### What was done
`web/test/e2e/monitoring-live.spec.ts` was already written by the previous agent (7 tests covering all live mode scenarios). The spec was green in logic but the production build was stale (built 2026-07-13, before live monitoring files were added 2026-07-18). Running `corepack pnpm build` rebuilt the bundle and all 7 tests passed.

### Key data-testids confirmed in source
- `live-toggle` — on the `<Button>` in `LiveToggle.tsx` (always present, disabled when from+to set)
- `live-toggle-wrapper` — on the tooltip trigger `<span>` (only rendered when disabled)
- `live-badge` — on the wrapper div in `LiveBadge.tsx` (rendered only when `transport !== "idle"`)
- `live-anomaly-strip` — on the container div in `LiveAnomalyStrip.tsx`
- `live-anomaly-list` — on the `<ul>` in `LiveAnomalyStrip`
- `live-anomaly-item-{seq|timestamp}` — on each `<li>`
- `live-anomaly-link-{seq|timestamp}` — on each `<a>`
- `date-range-controls` — on the wrapper div in `DateRangeControls.tsx`

### SSE mock pattern (established in jobs.spec.ts and confirmed here)
Use `page.route("**/api/bff/live/events**", route => route.fulfill({ contentType: "text/event-stream", body: "..." }))` where body uses named SSE frames: `event: reading\nid: 1-0\ndata: {...}\n\n`. The `useLiveReadings` hook batches rows at 500 ms intervals (FLUSH_INTERVAL_MS), so anomaly events that set state immediately are faster to observe than reading rows.

### Important: build staleness
The e2e suite uses `pnpm start` (production build). Any component changes require `pnpm build` before e2e tests. The playwright config sets `reuseExistingServer: !process.env.CI` — locally this reuses the running server, so a stale build will silently serve old code.

### Gates status
- `pnpm lint`: 2 warnings (pre-existing: unused biome-ignore in LiveBadge.tsx, noNonNullAssertion in test), 0 errors
- `pnpm typecheck`: clean
- `pnpm test`: 72/72
- `pnpm exec playwright test monitoring-live.spec.ts`: 7/7

---

## Phase 11 — Live Demo v2 Review Fixes (2026-07-20)

### What was done
Applied 7 review findings from `docs/architecture/live-demo-v2-review.md` (Part 1, FE items):

**F1 — FIXED** (`readings.ts`): `discoverChannels` was sampling `rows.slice(0, 50)` (oldest), missing extras in live rows that sort after canonical history. Changed to `rows.slice(-50)` (newest tail); also switched dedup from O(n) `includes` to `Set`. New test file `readings.test.ts` with F1 regression test: 100 canonical rows + 2 live rows → all 7 channels discovered.

**F3 — FIXED** (`useSetpoints.ts`): Removed lazy `useState(() => loadOverrides(storageKey))` initializer that caused SSR/hydration mismatch. State now starts as `{}`. The existing `useEffect([storageKey])` already loads overrides post-mount in both mount and scope-change cases.

**F4 — FIXED** (`channelMeta.ts`): Widened CO₂ optimal from `[350, 500]` to `[330, 520]` and humidity from `[55, 75]` to `[53, 77]` to eliminate routine demo warning flicker.

**F9 — FIXED** (`useSetpoints.ts`): Hoisted defaults to module-scope `DEFAULT_SETPOINTS` constant (computed once at load time); merged setpoints now wrapped in `useMemo([overrides])` to prevent unnecessary recalculation on every SSE tick.

**F10 — FIXED** (`useSetpoints.ts`): Added `.refine(([min, max]) => min < max)` to `setpointRangeSchema`. Hand-edited `[80, 20]` in localStorage now fails Zod validation → falls back to defaults. Test added in `useSetpoints.test.ts`.

**F11 — FIXED** (`channelMeta.ts`): Added `alarmBelowMin?: false` to `ChannelMeta` interface; set it on `light`. `getChannelStatus` branches on `CHANNEL_META[channel]?.alarmBelowMin === false` instead of the string literal `channel === "light"`. All existing light tests pass unchanged.

**F12 — FIXED** (`LiveBadge.tsx`): Replaced raw palette classes (`bg-green-400`, `bg-amber-400`, `bg-red-400`, etc.) with `sev-*` design tokens (`bg-sev-normal`, `bg-sev-warning`, `bg-sev-critical`). Wrapped `animate-pulse` with `motion-safe:` to respect `prefers-reduced-motion`.

**F13 — FIXED** (`SetpointsPanel.tsx`):
- Focus management: captures `document.activeElement` on trigger click (`savedFocusRef`), moves focus to first `input` in dialog on open (falls back to `tabIndex={-1}` dialog div), and restores focus to the captured element on Escape.
- Blur-commit: `tryUpdate` now fires `onBlur` instead of `onChange`. `onChange` only updates local state and clears any stale error — eliminates transient "Must be numeric" flashes on partial input.

### Zod v4 `.refine()` note
Zod v4 classic's `.refine()` returns `this` (same schema type), not `ZodEffects` like v3. This means `z.record(z.string(), setpointRangeSchema.refine(...))` type-checks and behaves correctly.

### React 19 ref-forwarding note
`Button` component doesn't use `forwardRef`. Rather than adding it, `SetpointsPanel` uses `document.activeElement` capture at open-time (saved in a ref) to return focus on close — more robust and requires no changes to the Button component.

### Gates status (2026-07-20)
- `pnpm typecheck`: clean
- `pnpm lint`: 3 pre-existing infos, 0 errors
- `pnpm test`: 135/135 (10 test files)
- `pnpm build`: clean
- `pnpm exec playwright test`: 49/49

---

## Performance profiling (2026-07-23)

### What was done
Added PROFILE logging to `web/`, gated by the same `PROFILE_LOGGING_ENABLED` env var as the Python backend (`1`/`true`/`yes`/`on`).

- `lib/profileFlag.ts` — shared env parser
- `lib/profileLog.ts` — server stdout emitter (`PROFILE phase=… ok=… elapsed_ms=…`)
- `lib/profiling.ts` — client helpers (`timed`, `measureInteraction`, `profileEvent`) → POST `/api/profile`
- `app/api/profile/route.ts` — GET `{enabled}` / POST ingest (no-op when disabled)
- BFF proxy + SSE routes emit `bff.proxy` / `bff.sse.jobs` / `bff.sse.live`
- UI: `ui.nav` (Sidebar), `ui.channel_toggle` (MonitoringView), `ui.time_window` (DateRangeControls)

Railway `web` already has `PROFILE_LOGGING_ENABLED=yes` (set via CLI). Redeploy web to activate BFF/UI lines in logs.

### Gates status (2026-07-23)
- `pnpm typecheck`: clean
- `pnpm lint`: 0 errors (pre-existing infos only)
- `pnpm test`: 178/178

---

## Architect review cleanup batch (2026-07-24) — findings #6–#9

### What was done

**#6 — `useChannelVisibility` hook** (`features/monitoring/hooks/useChannelVisibility.ts`):
Extracted the channel-visibility state machine out of `MonitoringBody`: knownChannels session memory, hidden/userToggledChannels/jumpFocus anomaly-jump pre-select, visible derivation, and toggleChannel. MonitoringView.tsx shrank from 370 → 296 lines (under 300). Behavior is identical.

**#7 — `useChartRangeSelect` hook** (`lib/useChartRangeSelect.ts`):
Extracted the duplicated `useWindowBusyCue + debounce(setRange, 200) + cancel-on-unmount + onChartRangeSelect` block that was identical in `MonitoringView` and `AnomaliesView`. Both now call `useChartRangeSelect({ isReadingsBusy })`. Named constant `RANGE_WRITE_DEBOUNCE_MS = 200` exported. Unit test added (`lib/useChartRangeSelect.test.ts`).

**#8 — Explicit drag-pan DOM contract** (`components/ui/useTableDragPan.ts` + `DataTable.tsx`):
`DataTable` now sets `data-row-selectable=""` on clickable body rows (keeping `tabIndex` for a11y). `useTableDragPan` checks `tbody tr[data-row-selectable]` instead of `tbody tr[tabindex]`.

**#9 — Scoped `chartBaseline`** (`components/charts/chartBaseline.ts` + callers):
All three functions (`rememberChartBaseline`, `readChartBaseline`, `ensureChartBaseline`) now take a `scopeKey: string` arg. Storage key is `ai-agronomist.chart-baseline:${scopeKey}`. `DateRangeControls` and `LiveToggle` each take a `pageId` prop and build `${pageId}:${tenant}:${gh}` internally. `ChartResetButton` takes `scopeKey` directly. `UPlotChart` takes `chartBaselineScopeKey` (ref-pattern) for the dblclick handler. `SensorTimeSeriesChart` and `ScoreTimeline` take `scopeKey` and thread it to both. `SharedViewControls` takes `pageId` and threads it to DateRangeControls + LiveToggle. All 4 MonitoringView pages (`monitoring`, `climate`, `gas`) pass `pageId`; `AnomaliesView` passes `"anomalies"`. `chartBaseline.test.ts` updated with cross-scope isolation tests.

### Gates status (2026-07-24)
- `pnpm lint`: clean (0 errors)
- `pnpm typecheck`: clean
- `pnpm test`: 215/215 (26 test files)
- `pnpm build`: clean

---

## Fix: blank Sensors/Reasoning in Table view (2026-07-27)

### Root cause
`AnomaliesView` computed `anomalyTotal = enrichment.anomalyCount ?? anomalies.length`. While the slow G4 `/enriched` request was in flight (43–96 s in production), `enrichment.anomalyCount` was null, so `anomalyTotal` fell back to the downsampled chart row count (≈35). `GroupedAnomalies` received that 35, computed `35 >= 500 = false`, and took the client grouping path — but enrichment only covers the top-500 by score while the client path lists rows by date. The row subsets barely intersect, so `enrichmentFor()` map misses on every displayed row → blank Sensors and Reasoning cells.

### Fix

**New files:**
- `features/anomalies/hooks/useAnomalyCount.ts` — Always-enabled hook that fetches `/readings/anomalies-in-window?max_points=1` to get the exact count cheaply (one row transferred; response always includes `anomaly_count`). Query key `["anomaly-count", ...]` distinct from `["flagged-window", ...]`.
- `features/anomalies/lib/groupingDecision.ts` — Pure `resolveGroupingMode(count: number | null): 'server' | 'client' | 'loading'`. Returns `'loading'` when null so callers never prematurely commit to a rendering path.
- `features/anomalies/lib/groupingDecision.test.ts` — 5 tests covering null→loading, count≥500→server, count<500→client.
- `features/anomalies/api/flaggedWindow.ts` — Added `fetchAnomalyCount` (max_points=1 variant).

**Modified:**
- `GroupedAnomalies.tsx`: removed `anomalyCount` prop; calls `useAnomalyCount` internally (always enabled); routes via `resolveGroupingMode`; shows `<Skeleton>` while count is unknown so no rendering path is ever taken prematurely. `AnomalyDetailRow` gained `isEnrichmentLoading?: boolean` — when true and sensors/reasoning are absent, renders `<Skeleton>` inside the span (preserving `data-testid`s) instead of empty string.
- `AnomaliesView.tsx`: `anomalyTotal` is now `number | null` (`enrichment.anomalyCount ?? (enrichment.isLoading ? null : anomalies.length)`); headline shows "—" while null; "All anomalies" card title uses null-safe guard; `anomalyCount` prop removed from `<GroupedAnomalies>`.
- `test/e2e/anomalies-deferred.spec.ts`: added `anomalies-in-window` mock (returning count=600) to "grouped view switches to G3" test; added new test "table view sensors cell never renders empty while enrichment is in flight".

### Key pattern: cheap count gate
`useAnomalyCount` (`max_points=1`) is far faster than G4 `/enriched` (43 s). Wire it always to `GroupedAnomalies`; the G4 result is only used for display (headline), not for routing. Any time these two can diverge, prefer the cheap count for routing decisions.

### Gates status (2026-07-27)
- `pnpm typecheck`: clean
- `pnpm lint`: 0 errors (pre-existing deprecated biome.json field only)
- `pnpm test`: 223/223 (28 test files — 5 new tests in groupingDecision.test.ts)
- `pnpm build`: clean
- `pnpm exec playwright test`: 54/54 (1 new test)

---

## P0 fix: Anomaly table 100% broken — max_points=1 → HTTP 422 (2026-07-27)

### Root cause
`fetchAnomalyCount` in `flaggedWindow.ts` sent `max_points=1` as a cheap count trick, but the backend declares `max_points` with `ge=DOWNSAMPLE_MAX_POINTS_FLOOR` (= 100, `src/config.py:1053`). Every count request returned 422, so `useAnomalyCount` always `isError=true`, and `GroupedAnomalies` always showed "Could not determine anomaly count — narrow the window or retry."

### Fix
**`features/anomalies/api/flaggedWindow.ts`:**
- Added `export const DOWNSAMPLE_MAX_POINTS_FLOOR = 100` (mirrors `src/config.py`; include a comment to keep in sync).
- Changed `query.set("max_points", "1")` → `query.set("max_points", String(DOWNSAMPLE_MAX_POINTS_FLOOR))`.
- Also corrected the JSDoc comment (the old comment was a lie: "max_points=1 so only one row is transferred").

**Three error strings in `GroupedAnomalies.tsx`:** Replaced "narrow the window or retry" (misleading for a 422) with a classified hint via `fetchErrorHint(error)`.

**New lib: `features/anomalies/lib/classifyFetchError.ts`:**
- `classifyFetchError(error): FetchErrorClass` — parses `"... failed (STATUS)"` pattern in error messages; 422/4xx → "validation", 408/504 → "timeout", 5xx → "server", no-status → "network".
- `fetchErrorHint(error): string` — human-readable parenthetical e.g. "invalid request params — this is a bug, please report it".

**Hooks — added `error: Error | null` field** to `AnomalyCountState`, `FlaggedWindowState`, `AnomalyGroupsState` (hooks now return `query.error`).

**New regression test: `features/anomalies/api/flaggedWindow.test.ts`:**
- Asserts `DOWNSAMPLE_MAX_POINTS_FLOOR === 100`.
- Asserts `fetchAnomalyCount` sends `max_points >= DOWNSAMPLE_MAX_POINTS_FLOOR`.
- Asserts `fetchAnomalyCount` does NOT send `max_points=1`.
- Asserts `fetchFlaggedWindow` sends `max_points >= DOWNSAMPLE_MAX_POINTS_FLOOR`.

### Key rule
Never send `max_points` below `DOWNSAMPLE_MAX_POINTS_FLOOR` to `/readings/anomalies-in-window`. The backend SQL COUNT for `anomaly_count` runs on the full window regardless, so the floor is the minimum for minimal data transfer.

### Gates status (2026-07-27)
- `pnpm typecheck`: clean
- `pnpm lint`: 0 errors (pre-existing deprecated biome.json field info only)
- `pnpm test`: 231/231 (29 test files — 8 new tests in flaggedWindow.test.ts)

---

## Severity: urgent + server-preferred path (2026-09-08)

### What was done
Task 1 frontend half — ADR-006 / migration 024 follow-up.

**Problem:** `Severity` union had no `"urgent"` value; the backend assigns `detected_severity: "urgent"` in 14 places but the UI had no way to render it. Additionally, the `getSeverity` function had dead `detector`/`agreement` branches that no call site ever populated, meaning all 232 rows in a 5-month export showed `"warning"`.

**Changes:**

- **`lib/severity.ts`**: Added `"urgent"` to `Severity` union. Added `SEVERITY_ORDER` constant (`urgent=4 > critical=3 > lstm=2 > warning=1 > normal=0`). Removed dead `detector`/`agreement` branches from `getSeverity` (simplified to score/threshold fallback only, returns `"normal" | "warning"`). Added `resolveRowSeverity({ score, threshold, detectedSeverity })` — prefers server-assigned `detected_severity`, falls back to `getSeverity` for pre-migration rows.

- **`app/globals.css`**: Added `--sev-urgent: #9333ea` (purple) and its `@theme inline` mapping `--color-sev-urgent`.

- **`components/ui/Chip.tsx`**: Added `urgent` entry to `SEVERITY_CLASSES`.

- **`features/anomalies/components/AnomalyTable.tsx`**: Added `urgent: "Urgent"` to `SEVERITY_LABEL`.

- **`features/anomalies/api/enriched.ts`**: Added `detected_severity` to `enrichedRowSchema` and `anomalyGroupSampleSchema`.

- **`features/anomalies/api/flaggedWindow.ts`**: Added `detected_severity` to `flaggedRowSchema` (future-proofing for when `_canonical_records_to_rows` includes it).

- **`features/anomalies/hooks/useEnrichedAnomalies.ts`**, **`useFlaggedWindow.ts`**, **`useAnomalies.ts`**: All three call sites switched from `getSeverity` to `resolveRowSeverity`. `useEnrichedAnomalies` and `useFlaggedWindow` pass `detected_severity` from the API row; `useAnomalies` uses score/threshold fallback (chart rows from monitoring endpoint have no `detected_severity`).

- **`features/anomalies/components/GroupedAnomalies.tsx`**: Severity group sort now uses `SEVERITY_ORDER` as primary key (urgent first), peak score as tiebreaker.

- **`lib/severity.test.ts`**: Rewrote; removed dead-branch tests; added `resolveRowSeverity`, `SEVERITY_ORDER`, urgent label coverage, and multi-group regression test.

### Dead-branch decision
Removed. The `detector` and `agreement` branches in `getSeverity` were never reachable: the `/readings/anomalies-in-window` and `/readings/window` endpoints do not return those fields, and neither do the chart rows. Rather than making them live (would require backend changes to `_canonical_records_to_rows` to expose `trigger_detector`), they were deleted. The server-preferred `resolveRowSeverity` replaces the multi-branch logic entirely.

### Backend assumption noted in done signal
`/api/anomalies/enriched` routes through `readings_service.fetch_top_flagged_rows_by_score` → `_canonical_records_to_rows` which does NOT currently include `detected_severity`. The groups endpoint DOES include it via a different SQL path. FE codes against the documented shape; fallback path works for pre-migration rows.

### Gates status (2026-09-08)
- `pnpm typecheck`: clean
- `pnpm lint`: 0 errors (pre-existing biome.json deprecation info only)
- `pnpm test`: 259/259 (32 test files)

---

## Fix: `every sidebar entry navigates` e2e failure + silent-pass hole (2026-09-10)

### Root cause (commit a4cd41a)
`App Configuration` nav entry was added together with a new `/greenhouse-profile` page
(`ConfigurationView`). `ConfigurationBody` throws when `isError` is true (deliberate product
choice). The page has no `error.tsx`, so Next.js renders its default error page ("This page
couldn't load", h1) — sidebar disappears. The loop then times out 30 s waiting to click the
now-missing `App Configuration` sidebar link.

**Silent-pass hole:** The old assertion was `getByRole("heading", { level: 1 })` with no name.
The error page's own h1 satisfied it, so the test passed on the crashed page and only blew up
one step later.

### Fix (`web/test/e2e/smoke.spec.ts`)

1. **`setupBffMocks(page)` helper** — extracted from the `app boots` inline mocks so both tests
   share one payload definition:
   - `**/api/bff/readings/**` — prevents readings-dependent views from reaching their
     `isError → throw` path (Anomaly Detection, Thresholds).
   - `**/api/bff/config/**` — prevents `ConfigurationBody` from throwing (the critical fix).
   - `**/api/bff/live/**` — suppresses SSE probe noise on the Home page.

2. **`NAV_TARGETS` gains `heading` field** — exact h1 text per destination (differs from nav
   label in two cases: "Thresholds" → "Threshold Management").

3. **Two new assertions per iteration:**
   - `expect(page.getByRole("heading", { name: "This page couldn't load" })).not.toBeVisible()`
     — fails loudly on the crashed target instead of on the next one.
   - `expect(page.getByRole("heading", { name: target.heading, level: 1 })).toBeVisible()`
     — asserts the *expected* page loaded, not merely that some h1 is present.

### Assertion fix verified
Temporarily replaced the config mock with `route.fulfill({ status: 500 })`. Test failed on
target 7 (Greenhouse Profile) with "element(s) not found" for the heading, not on target 8's
click. Restored mock; all 54 e2e tests passed.

### Gates status (2026-09-10)
- `pnpm lint`: 0 errors
- `pnpm typecheck`: clean
- `pnpm test`: 259/259
- `pnpm exec playwright test`: 54/54

---

## Fix: `custom range form` TZ-independent assertion (2026-09-10)

### Root cause
`expect(new Date(params.get("from") ?? "").getTime()).toBe(new Date("2026-06-01T12:00").getTime())`:
- RHS `new Date("2026-06-01T12:00")` is Node.js local time — depends on the TZ env-var of the test process.
- LHS is a UTC ISO string from nuqs's `.toISOString()` — always an absolute instant, TZ-independent to parse.
- Both sides agree only when the Node.js TZ and the browser TZ are identical. Playwright normally propagates TZ env-var to the browser, but the browser's source (OS vs env-var) can diverge on macOS, and the assertion is semantically wrong regardless.

### Contract found
`DateTimeField.toDate()` uses `new Date(year, month-1, day, hour, minute)` — **browser local time**.  
nuqs `parseAsIsoDateTime.serialize()` calls `.toISOString()` → **UTC ISO string** in URL.

### Fix
Replaced `new Date("YYYY-MM-DDTHH:mm").getTime()` on the RHS with `page.evaluate(() => new Date(year, month-1, day, hour, minute).getTime())`. Both sides now use the browser's timezone: the URL param is parsed as an absolute UTC instant (TZ-independent), and the expected value is computed in the same browser context that produced it.

### Gates status (2026-09-10)
- `pnpm lint`: 0 errors
- `pnpm typecheck`: clean
- `pnpm test`: 259/259
- `pnpm exec playwright test`: 54/54
- TZ=UTC, TZ=Asia/Tokyo, TZ=America/Los_Angeles: all 1 passed

---

## UI freeze mitigations (2026-07-23)

Shipped in `27bbab9` after PROFILE metrics showed BFF `readings/window` at ~2.7–5.9 s and live-mode main-thread cascades as the freeze driver:

1. `useReadings`: no historical `refetchInterval` while live
2. `MonitoringView`: `useDeferredValue` for chart/table; enrichment bounds from `from`/`to`
3. `DataTable`: reset page on `rowCount` only
4. `LiveToggle` + `DateRangeControls`: optimistic URL updates before bounds I/O

Metrics write-up: `docs/refactor/performance-2026-07-23.md`.

---

## Security phase 2 — login + BFF lockdown (2026-09-28)

### What was done
No login or session mechanism existed anywhere in `web/` — the BFF catch-all and both SSE
routes proxied any anonymous request and attached the API key unconditionally. Closed per
`docs/security/auth-audit-phase1.md` §7 and the team-lead's phase 2 contract.

**Login — Auth.js v5 (`next-auth@5.0.0-beta.32`) + Resend magic link:**
- `auth.ts` (root) — `NextAuth()` config. `session: { strategy: "jwt" }` even though a
  Postgres adapter is configured: Next 16's `proxy.ts` defaults to the **Node.js runtime**
  (renamed from `middleware.ts` — see `node_modules/next/dist/docs/.../file-conventions/proxy.md`),
  so the classic edge-safe split-config pattern isn't needed, but jwt sessions still avoid a
  DB round trip on every request. Adapter is required regardless of session strategy — the
  Email provider persists verification tokens/users through it.
- `lib/auth/emailProvider.ts` — wraps the **built-in** `next-auth/providers/resend` (plain
  `fetch` to Resend's HTTP API, no nodemailer needed — it's an optional peer dep). Chose
  Resend over SES/Postmark: ships built into next-auth, one HTTP API key, no
  SMTP/AWS-IAM/SES-sandbox setup. `sendVerificationRequest` is overridden to log the link
  instead of calling Resend whenever `NODE_ENV !== "production"` — dev/test never hit the
  real API.
- `lib/auth/db.ts` + `lib/auth/schema.sql` — a **dedicated** Postgres (`AUTH_DATABASE_URL`,
  new env var, not yet provisioned) holds `@auth/pg-adapter`'s exact schema
  (`users`/`accounts`/`sessions`/`verification_token` — table/column names it hard-codes,
  don't rename) plus a `user_tenants(email, tenant_id)` mapping this app adds. No self-serve
  sign-up: `signIn` callback in `auth.ts` denies (before sending any mail) any email with no
  `user_tenants` row — fail closed, never a default tenant. `getPool()` must NOT throw when
  `AUTH_DATABASE_URL` is unset (`next build` imports `auth.ts` while collecting route
  metadata, executing `PostgresAdapter(getAuthPool())` at module-eval time) — only fail at
  first query.
- `features/auth/LoginView.tsx` + `app/login/page.tsx` + `app/api/auth/[...nextauth]/route.ts`
  — email-only form, single confirmation message regardless of whether `signIn` denied or
  actually sent mail (prevents user enumeration).

**Middleware -> Proxy, tenant binding:**
- `proxy.ts` (root, replaces the deprecated `middleware.ts` convention) wraps `auth()`.
  Pure routing decision extracted to `lib/auth/routeAuthDecision.ts` (unit tested) so the
  fail-closed matrix doesn't need a request object to verify: anonymous page -> redirect
  `/login`; anonymous `/api/bff/*` (including both SSE routes) -> 401; `/login` and
  `/api/auth/*` always allowed (or the sign-in flow can't run at all).
  Matcher excludes `_next/static`, `_next/image`, and common static file extensions.
- Once logged in, `proxy.ts` deletes any client-sent `x-tenant-id` header and re-sets it from
  `req.auth.user.tenantId` before calling `NextResponse.next({ request: { headers } })` — the
  **one** place "logged in" becomes "which tenant." The 3 BFF route handlers
  (`app/api/bff/[...path]/route.ts`, `.../live/events/route.ts`, `.../jobs/[id]/events/route.ts`)
  read that header, 401 defensively if it's somehow missing (defense in depth — proxy should
  have already blocked it), and forward it upstream as `X-Tenant-Id` per the agreed contract
  with `sec-p2-api` (BFF holds one `caller_type=web` credential; API honours `X-Tenant-Id`
  for that caller type, ignoring any query/body `tenant_id` it still receives from the FE's
  existing `useScope()`-driven calls — that ignoring happens API-side, not scrubbed here).
- `Session["user"].tenantId` is added via `declare module "next-auth"` in `lib/auth/types.d.ts`
  (works because `next-auth`'s `Session` is a **named** re-export of `@auth/core`'s). The JWT
  payload's `tenantId` is **not** type-augmented the same way — `next-auth/jwt` is a bare
  `export * from "@auth/core/jwt"`, and TypeScript only merges `declare module` into the
  module that originally declares the interface (`@auth/core/jwt`, which pnpm doesn't hoist
  to a resolvable path since it's not a direct dependency here). `auth.ts` reads/writes
  `token.tenantId` through a local `TokenWithTenant` type assertion instead — avoids a fragile
  augmentation entirely; got bitten by this once (a `Type '{}' is not assignable to type
  'string'` error) before finding the root cause.

**Key management:** `API_KEY`/`AUTH_RESEND_KEY`/`AUTH_DATABASE_URL` all read server-side only
(`process.env.*`, no `NEXT_PUBLIC_*`). Verified via build-output grep (see report to
team-lead for the exact commands) with distinctive fake secret markers injected through env
vars at build time — zero hits anywhere in `.next`, confirming Next never inlines a
non-`NEXT_PUBLIC_*` value into any bundle.

**E2E regression fixed:** gating every page behind login broke all 66 existing e2e specs
(redirect to `/login` on `page.goto`). Fixed with `test/e2e/global-setup.ts`, which mints a
valid Auth.js session cookie directly via `next-auth/jwt`'s `encode()` (`salt` must equal the
session cookie name, `"authjs.session-token"` unprefixed since e2e runs over plain
`http://localhost` — Auth.js only uses the `__Secure-` prefix over https) and saves it as
Playwright `storageState`. `playwright.config.ts` passes the same fixed `AUTH_SECRET`
(`test/e2e/authSecret.ts`) into the `webServer.env` so the running server can decode it. This
only works because session strategy is `jwt` — no DB round trip needed to validate an
existing session, so e2e never touches Postgres.

**Manual verification beyond the automated suite** (built server + a fake upstream SSE
emitter on `:9999`, real `next-auth/jwt` `encode()` cookie):
- Anonymous `/api/bff/live/events` and `/api/bff/jobs/:id/events` -> `401` (not just the
  catch-all).
- Anonymous `/monitoring` -> `307` to `/login?callbackUrl=%2Fmonitoring`.
- Authenticated SSE: heartbeat events arrived ~500 ms apart end-to-end through
  `proxy.ts` + the route handler — confirms the auth check does not buffer the stream.

### Known follow-ups left for the coordinator (not done here — out of scope/ownership)
- `AUTH_DATABASE_URL` Postgres plugin, `AUTH_SECRET`/`AUTH_RESEND_KEY` real values, and running
  `lib/auth/schema.sql` once — all deploy decisions, documented in `RAILWAY_HOSTING.md` §6 but
  not provisioned.
- `web/lib/api-types.gen.ts` / `web/openapi.json` NOT regenerated: `sec-p2-api`'s removal of
  `GET /api/auth/validate` is uncommitted in the shared working tree and `web/openapi.json`
  itself hasn't been refreshed from a running instance yet, so regenerating now would be a
  no-op against the stale snapshot — left alone per the task's explicit instruction.
- `TenantSelector.tsx` still lets a user pick `tenant_1/2/3` in the URL; the BFF now silently
  overrides this server-side, so picking a different tenant while a session is bound to
  another one is a confusing no-op, not a security hole. Flagged to team-lead as a UX
  follow-up, not fixed here (out of the phase 2 ask).
- Two files outside `web/` still call the API's **public** Railway domain directly, bypassing
  the BFF entirely: `.claude/skills/perf-investigation/SKILL.md` and `scripts/perf-probe.sh`
  (also `scripts/perf_flows.py`, but only for the `web` domain). Reported to team-lead per the
  "report every reference" instruction; not edited (outside file ownership).

### Gates status (2026-09-28)
- `pnpm typecheck`: clean
- `pnpm lint`: 0 errors (pre-existing biome.json `recommended`-field deprecation info only)
- `pnpm test`: 337/337 (38 test files — 1 new: `lib/auth/routeAuthDecision.test.ts`)
- `pnpm build`: clean
- `pnpm exec playwright test`: 66/66
