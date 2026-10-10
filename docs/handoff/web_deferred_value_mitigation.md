# HANDOFF — web/ useDeferredValue first-paint starvation: mitigation plan

**Status:** approved by the user, not started. Owner: **frontend-engineer** (`web/`).
**Baseline at handoff:** HEAD `706c2b0` on `main`, clean tree, all 4 CI workflows green.
Backend suite 3639 passed / 21 skipped / 4 xfailed / 0 failed. Web: unit 46 files /
422 tests, e2e **66** tests.

Two commits already landed and are NOT to be undone: `5b74ad4` (MonitoringView) and
`d1fdba5` (HomeView). This document is the follow-up work they left open.

---

## 1. The bug, precisely

`HomeView` and `MonitoringView` each called `useDeferredValue(rows)` unconditionally. The
chart mounts only once `rows.length > 0`, and that gate read the deferred value.

The failure is the **first data-arrival update, not the initial mount**. `useDeferredValue`
returns the given value on first render; on updates it returns the *previous* value while
re-rendering the new one at low priority. So when `rows` goes `[]` → populated, React keeps
rendering `[]` and schedules the populated render as background work — which under CPU
contention can starve for seconds with no upper bound short of a forced sync point. The
operator sees no chart (and on monitoring, a stale table header) for multiple seconds.

The defer exists for a narrower purpose. Git history: *"Chart/table can lag a frame behind
tiles so nav/clicks stay responsive under live SSE."* That is about smoothing one-per-second
tick updates, not the one-time historical load.

Shipped fix in both components: `live ? deferredRowsForLive : rows`. The hook still runs
unconditionally (rules-of-hooks safe); only the rendered value is conditional.

**Evidence asymmetry:** MonitoringView is proven — 16/30 and 14/20 reps timed out at 6× CPU
throttle before the fix, 0/30 and 0/20 after. HomeView is **unproven**; see the dead end in
§4.

### Verified code anchors

| What | Where |
|---|---|
| HomeView fix | `web/features/home/HomeView.tsx:119-120` |
| `live` source (URL state) | `web/features/home/HomeView.tsx:98` — `useQueryState("live", parseAsBoolean.withDefault(false))` |
| Chart consumer | `web/features/home/HomeView.tsx:235` — `SensorTimeSeriesChart rows={deferredChartRows}` |
| Query-key consumers | `web/features/home/HomeView.tsx:123-124` — `windowStart`/`windowEnd` feed `useAnomalyCount` + `useFlaggedWindow` |
| MonitoringView fix | `web/features/monitoring/MonitoringView.tsx:181-186` (2 call sites: chart + table) |
| Payload bound | `web/features/monitoring/hooks/useReadings.ts:34` — `DEFAULT_MAX_POINTS = 2000` (server-side LTTB) |
| Perf budgets | `web/test/e2e/perf.spec.ts` — cold TTI 2000 ms, filter 500 ms, channel toggle 300 ms |
| CI retries | `web/playwright.config.ts` — `retries: process.env.CI ? 2 : 0` (defense in depth, NOT the fix) |

`useDeferredValue` exists in exactly two source files in `web/` (3 call sites). Confirmed by
`grep -rn "useDeferredValue" --include="*.ts" --include="*.tsx" web/ | grep -v node_modules`.

---

## 2. Risks this plan addresses

| # | Risk | Severity |
|---|---|---|
| R1 | **The fix narrows the bug, it does not eliminate it.** In live mode the defer is still active, so `/?live=true` still defers the first data arrival and can still starve. Scoped by *mode*, not by *first paint*. | High |
| R2 | **Home page has no guard of either kind.** E2E cannot discriminate (§4), *and* perf budgets cover only `/anomalies` and `/monitoring` — not `/`. Re-introduction ships silently. | High |
| R3 | **Synchronous render can block input.** The defer protected main-thread responsiveness; the historical first paint is now synchronous. Bounded at ~2000 points by LTTB, so not unbounded — but a 2000-point uPlot init plus tiles plus anomaly list in one commit is no longer interruptible on a slow device. | Medium |
| R4 | **`deferredChartRows` also feeds query keys.** `windowStart`/`windowEnd` fall back to `deferredChartRows[0]`/`.at(-1)` when `from`/`to` are absent. Historical rows are stable so churn is bounded, but the bounds now move on the same commit as the data rather than a frame later. The "zero refetches" budget exists only on `/monitoring`. | Low–Medium |
| R5 | **Pattern recurrence.** The bug was copy-pasted across two components; nothing prevents a third. No lint rule, no shared abstraction. | Medium |

---

## 3. The plan, in order

### Item 1 — gate on emptiness, not on mode (fixes R1). Highest value.

Bypass the defer until the first non-empty commit, in **both** components: defer genuine
updates, never the transition out of `[]`. This eliminates the bug class rather than
narrowing it to live mode, and is strictly more correct than what shipped.

Keep `useDeferredValue` called unconditionally. Keep live-mode tick smoothing intact — that
is the behaviour the hook was added for and it must survive. Note MonitoringView has **two**
deferred values (chart rows and table rows); both need the same treatment.

### Item 2 — unit test the rendered row source (fixes R2's correctness half).

Assert directly that with `live=false` the chart receives `rows` identity-equal, and with
`live=true` it receives the deferred value — plus, after item 1, that the empty→populated
transition is never deferred. Deterministic, no throttling, catches re-introduction.

**Do NOT attempt an e2e timing assertion.** See §4.

### Item 3 — extend perf budgets to `/` cold load (fixes R2's perf half, partially R3).

One new test in `web/test/e2e/perf.spec.ts` using the existing machinery and the 2000 ms
cold TTI budget. The home page currently has no budget at all.

### Item 4 — extract a shared hook (fixes R5).

`useLiveDeferred(rows, live)` or similar, used by both components, so there is one place to
get right and a third component adopts it by default. Structural; preferred over a lint rule.

**Sequencing:** items 1 and 2 together (same change, same test), then 3, then 4. All inside
`web/`. Realistically one frontend-engineer dispatch.

---

## 4. Dead end — do not repeat this

**An e2e spec cannot guard the HomeView first paint.** `d1fdba5`'s own message suggested "a
spec with a realistic payload" would catch it. That was attempted and does not work.

Measured: scratch spec, 600-row payload (top of the 48–600 range the monitoring specs use),
`workers=1`, `--repeat-each=20`, rebuilt each time with the fix reverted vs restored, CPU
throttled via CDP `Emulation.setCPUThrottlingRate`:

| rate | fix reverted | fix restored |
|---|---|---|
| 6× / 20× / 35× / 40× | 20/20 passed | 20/20 passed |
| 45× | 5/20 failed | 1/20 failed |

No rate separates them. Below ~40× nothing starves; at 45× both sides fail with almost
completely overlapping latencies (4.7–6.9 s vs 4.9–6.9 s) because generic hydration cost
swamps the 5 s budget. `HomeView` composes more than the monitoring chart assertions —
status bar with its own anomaly-count fetch, tiles, anomaly list — so its baseline is higher
and the window between "no starvation" and "everything swamped" is too narrow. Those numbers
were also taken on a machine running many agents concurrently, so treat any threshold from
them as unstable.

A regression guard must fail without the fix and pass with it. `smoke.spec.ts:121`'s 1-row
home assertion passes either way, which is exactly why this bug shipped unverified. **Ship no
new e2e spec here.** Item 2's unit test is the route.

**Also:** do not leave probe or scratch specs inside `web/test/e2e/`. Playwright collects
them — a previous `zzz-probe4.spec.ts` made the suite 67 tests and applied 6× CPU throttling
on a parallel worker, which very likely caused a spurious failure in an unrelated calendar
test. If throttling is needed to prove something, keep it outside `test/e2e/` and delete it
before finishing. Acceptance check is the suite count: **66** plus any tests deliberately
added.

---

## 5. Gates and conventions

Read `web/AGENTS.md` first. Run from `web/`, pnpm 9.15.9 via corepack:

```
corepack pnpm lint        # biome — 1 pre-existing info is expected, not a failure
corepack pnpm typecheck   # tsc strict, no any
corepack pnpm test        # vitest — baseline 46 files / 422 tests
corepack pnpm build       # production build; e2e needs it
corepack pnpm exec playwright test   # baseline 66 tests
```

All five must be green. Run them in the **foreground** and block — backgrounding a run and
waiting for a notification is how six agents on this project lost their turn, two of them
twice. A 5–10 minute blocking run is correct behaviour.

Report measured values; never adjust code or an expected value to make a test pass. End any
agent report with raw `git status --short` rather than a hand-written file list — that is a
CLAUDE.md rule, because agents here misreported their own blast radius twice.

Nothing is committed without the user's approval.
