---
name: perf-investigation
description: Diagnose performance degradation in AI Agronomist (BE latency, FE render, live streaming, empty-tenant hangs). Use whenever the user reports the app being slow, hanging, timing out, "degradation", a page not loading, live monitoring showing no data, or asks to gather/compare FE and BE performance metrics. Also use before shipping changes to anomaly, monitoring, or readings endpoints.
---

# Performance investigation routine

The deployed app, not localhost, is the subject. Production URLs:

- **Web** — https://web-production-cd280.up.railway.app
- **API** — https://ai-agronomist-api-production.up.railway.app
- Railway services: `web`, `ai-agronomist-api`, `RabbitMQ` (project `ai-agronomist`, env `production`)

## The one rule that matters

**Sweep window width before anything else.** Every regression found in this
project so far was sub-300 ms at 1/7/30 days and catastrophic at full range.
A probe that tests one window reports all-green on a broken deployment.

The Anomaly Detection page requests the **entire data range when no window is
selected**, and `tenant_1/gh_A` holds a full year — so the default first paint
is the worst case, not an edge case.

## The trap that will fool you

These endpoints are Redis-cached for `REDIS_CACHE_EXPIRE_SECONDS` (default
300), and `src/api/cache_keys.py` keys on the **full query string**. Probe the
same URL twice and the second run measures Redis, not the query: a 33 s
endpoint returns in 0.29 s and every budget passes.

`perf-probe.sh` defeats this by jittering the window `start` by a per-run
offset, so the query string always differs. **Never hand-curl the same URL
twice and conclude it got faster** — identical `size_download` across two runs
is the tell that you are reading cache. Warm-vs-cold is also a real user
question: the first visitor after each 5-minute expiry pays full cost.

## Order of work — cheapest first, stop when you have the answer

Do **not** open a browser first. Both P0s found on 2026-07-27 were pure
backend-contract facts, findable with curl in 90 seconds.

### Layer 1+2 — `scripts/perf-probe.sh` (start here, always)

```bash
scripts/perf-probe.sh [tenant_id] [greenhouse_id]   # default tenant_1 gh_A
```

Exit `0` within budget · `1` budget breach · `2` contract violation.
(When piping to `tail`, read `${PIPESTATUS[0]}`, not `$?`.)

- **Layer 1** — latency matrix, every read endpoint × {1d, 7d, 30d, full}.
- **Layer 2** — FE/BE contract conformance. Reconciles the FE's mirrored
  constant, `src/config.py`'s default, and the **deployed** API's enforced
  range. The deployed value wins: those limits are env-overridable, so the
  running service can disagree with both checked-in constants.

### Layer 3 — browser metrics via `chrome-devtools` MCP

Only after Layer 1 is clean, or to attribute a slow page between XHR and render.

Chrome must be listening on 9222. It is **not** by default:

```bash
nohup /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-debug-profile \
  --no-first-run --no-default-browser-check \
  https://web-production-cd280.up.railway.app/ > /dev/null 2>&1 &
curl -s http://127.0.0.1:9222/json/version   # verify before using MCP tools
```

Use a **dedicated `--user-data-dir`** so the user's real Chrome and its tabs
are untouched. The app has no auth wall, so a clean profile has full access —
do not quit their browser. (Chrome 144+ can instead attach to the live profile
via `--autoConnect` plus `chrome://inspect/#remote-debugging`; only worth it if
a real logged-in session is genuinely required.)

Then: `performance_start_trace` → drive the flow → `performance_stop_trace`
for LCP/INP/CLS, and `list_network_requests` for the waterfall.

**Layer 3's job is attribution, not discovery.** It answers "is this a slow
XHR or slow render?" If a single request dominates the waterfall, it is a
Layer 1 problem and belongs to the backend.

### Layer 4 — Railway log correlation

```bash
railway logs -s ai-agronomist-api | tail -200
railway logs -s web | tail -200
```

The API emits `request_id` in response bodies, so it joins the browser
waterfall to server logs with no new instrumentation.

## Known-bad patterns — check these first on any new report

| Symptom | Real cause | Where |
|---|---|---|
| Page slow only with no window selected | Full-range query; Python-side grouping with no SQL pushdown or pagination | `src/api/routes/anomalies.py` `get_anomaly_groups` / `get_enriched_anomalies` |
| "Could not determine anomaly count" / "narrow the window" | HTTP **422**, not a timeout. FE sent a param outside the backend's declared range. Narrowing can never help | `web/features/anomalies/api/flaggedWindow.ts` vs `src/config.py` |
| Live monitoring shows nothing forever on a new tenant | Greenhouse registry entry only created as a side effect of upload. `/api/live/status` returns **200 + empty list**, indistinguishable from "connected, no data yet" | `src/api/routes/live.py` |
| Everything green in CI, broken in prod | e2e is hermetic by design — `playwright.config.ts` pins `API_URL` to a dead port and every call is `page.route`-mocked | `web/test/e2e/**` |

## Why CI cannot catch this class of bug

The Playwright suite mocks every BFF call and measures render against stubbed
data. That is correct for determinism, but it means **no test validates that FE
query params fall inside the backend's declared ranges**, and no perf budget
reflects real backend latency. Layer 2 exists solely to close that gap — when
you fix a contract bug, add the assertion there and to a FE unit test (see
`web/features/anomalies/api/flaggedWindow.test.ts`), never to a mocked e2e spec.

## Reporting

Always give measured numbers with the window width attached — "33 s at full
range, 0.19 s at 7 days" is actionable; "the page is slow" is not. State
whether each finding is a **budget breach** (slow) or a **contract violation**
(broken), because they route to different fixes.
