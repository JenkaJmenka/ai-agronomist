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
