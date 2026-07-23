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

## UI freeze mitigations (2026-07-23)

Shipped in `27bbab9` after PROFILE metrics showed BFF `readings/window` at ~2.7–5.9 s and live-mode main-thread cascades as the freeze driver:

1. `useReadings`: no historical `refetchInterval` while live
2. `MonitoringView`: `useDeferredValue` for chart/table; enrichment bounds from `from`/`to`
3. `DataTable`: reset page on `rowCount` only
4. `LiveToggle` + `DateRangeControls`: optimistic URL updates before bounds I/O

Metrics write-up: `docs/refactor/performance-2026-07-23.md`.
