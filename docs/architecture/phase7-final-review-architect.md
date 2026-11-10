# Phase 7 Final Co-Review — Architect Half

> **Historical — point-in-time record.** Written 2026-07-24. Status: record only — the architect half of the Phase 7 co-review of an uncommitted working tree (findings #3–#9); line counts and test totals are as of that date. Not maintained against current code.

**Date:** 2026-07-24
**Reviewer:** architect (co-review with tech-lead)
**Scope:** Full uncommitted working tree (findings #3–#9), excluding `models/*.pt`.
**QA context:** `docs/coverage/phase4-6-remaining-findings.md` — 2516 backend tests green,
web lint/typecheck/vitest green.

---

## 1. Layering: routes → services → data

**PASS.** `src/api/routes/upload.py` (815 → 292 lines) is now I/O-only: each of the four
data-management routes is a thin delegate into `src/api/services/tenant_data_wipe.py` and
`src/api/services/synthetic_anomalies.py`. Dependencies flow one direction
(routes → services → `src/db/models`); no service imports a route, and no circular imports
were introduced. The `bulk_upsert_rows` inline imports were correctly hoisted to the top of
`upload.py`.

Framework decoupling is done right in `tenant_data_wipe.py`: the route passes
`getattr(request.app.state, "live_runtime", None)` in, so the service never touches
FastAPI's `Request`/`app.state`. The live-hub and scope-control fallbacks stay behind lazy
imports inside best-effort helpers, matching the pre-split behavior exactly.

**Notes (non-blocking):**

- **N1 — `HTTPException` in the service layer.** `synthetic_anomalies.py` raises
  `fastapi.HTTPException` for 422 validation errors. This leaks an entry-layer type one
  level down; `tenant_data_wipe.py` and `cache_invalidation.py` are framework-free. A
  purist boundary would raise a domain error translated in the route. Accepted here
  because the 422 messages are contractually byte-identical to the pre-split responses
  (verified against tests), and translating would add an abstraction with a single call
  site. Revisit only if a second consumer of the service appears.
- **N2 — dead helpers carried over.** `_daily_cycle` and `_anomaly_value` in
  `synthetic_anomalies.py` are unused in the injection flow (QA flagged `_daily_cycle`;
  `_anomaly_value` is likewise dead — only `_anomaly_value_directed` is called). Delete in
  a follow-up rather than carrying them into the new module's public history.
- **N3 — `_TYPE_PROFILES` consolidation is sound.** The five parallel per-type dicts are
  now one table with identical values and key order; `ANOMALY_TYPES = list(_TYPE_PROFILES)`
  preserves ordering, so 422 detail strings are unchanged. Grep confirms no external module
  imports the moved symbols (the web UI keeps its own `INJECT_ANOMALY_TYPES` copy — a
  pre-existing duplication, out of scope here).

## 2. `SensorReading.source` vs `RawReading.source` semantics

**PASS.** The live pipeline (`src/live/persistence.py`) now writes the same provenance
value — the literal `"live"` — to both tables, and the wipe service filters both with the
same primary predicate. `SensorReading.source` domain is `{'live', NULL}`;
`RawReading.source` remains the wider vendor/synthetic namespace (`'synthetic:<type>'`,
`'synthetic:context'`, CSV adapter values). The `remove_live_demo_data` raw filter
(`source == 'live' OR source LIKE 'live:%'`) is a defensive superset of what the writer
produces today — acceptable forward-compatibility for per-source tagging.

The `delete_synthetic_anomalies` simplification (dropping the `anomaly_type != 'live'`
guard) is correct **post-migration**: live rows carry `anomaly_type=NULL` and are excluded
by `is_not(None)` already. The timestamp fallback in `wipe_live_demo_data` was correctly
tightened with `SensorReading.source.is_(None)` so tagged rows are owned only by the
primary filter. Read paths (`readings.py`, `anomalies.py`) never filtered on the old
`anomaly_type='live'` sentinel, so no read-path drift.

**Notes (non-blocking):**

- **N4 — duplicated protocol constant.** `LIVE_RAW_SOURCE = "live"` is defined in
  `tenant_data_wipe.py` while `persistence.py` hardcodes `"source": "live"`. Writer and
  eraser now hold independent copies of the same wire value. Move it to one shared home
  (e.g. next to the models or a `src/live` constants module) so they cannot drift.
- **N5 — `live-backfill` rows are not owned by the live wipe.**
  `scripts/backfill_live_raw_readings.py` writes `source='live-backfill'`, which matches
  neither `'live'` nor `'live:%'`. Pre-existing and arguably intentional (backfill of
  historical canonical rows), but worth an explicit line in the script docstring.
- **N6 — column doc slightly incomplete.** `SensorReading.source` doc says "None for CSV
  uploads"; synthetic inject rows are also `source=NULL` (they are tagged via
  `anomaly_type` instead). Trivial doc touch-up.

## 3. Alembic 017 — backfill correctness under live traffic

**PASS WITH NOTES.** The migration itself is correct: nullable `String(64)` add matches
the model; the backfill (`SET source='live', anomaly_type=NULL WHERE anomaly_type='live'`)
and downgrade are exact inverses; both are idempotent (re-running either UPDATE is a
no-op); the `016 → 017` chain is verified. On Postgres, a nullable ADD COLUMN is
metadata-only and the UPDATE takes row locks only on live-tagged rows — safe under
concurrent readers. `batch_alter_table` (full table recreate) only bites on SQLite, which
is dev-only.

**Notes (non-blocking):**

- **N7 — deploy-ordering window.** "Under live traffic" here really means "across the
  deploy boundary," and both orderings have a bounded gap:
  - *New code before migration:* `PersistenceBatcher` INSERTs include the `source` column
    → flush fails until 017 runs. The supervised loop backs off and re-queues events, so
    this self-heals after migration, but events past the queue bound are lost.
  - *Migration before new code (normal order):* old workers keep writing
    `anomaly_type='live', source=NULL` after the backfill. Those straggler rows are
    invisible to the new wipe's primary filter **and** its timestamp fallback
    (`anomaly_type IS NULL` required), and the "all synthetic" delete
    (`anomaly_type IS NOT NULL`) would mis-own them.

  Given the documented single-worker deployment (migration + process restart happen in one
  deploy), the window is seconds and the affected rows are demo data. **Recommendation:**
  add a one-line ops note "run `alembic upgrade head` immediately before starting the new
  process; re-run the 017 backfill UPDATE (idempotent) if old workers overlapped" to the
  deploy runbook.
- **N8 — no index on `source`.** Wipe deletes filter via the `tenant_id` index first, so
  this is fine at current scale; add a partial index only if live-row wipes ever slow down.

## 4. `scope_control` multi-worker documentation honesty

**PASS.** Verified against the actual docs: `docs/live_scada_connectors.md` §8
("Deployment Constraints") exists and says what the docstring claims — single-worker
guidance, N×-runtime failure modes, and both scale-out options (Redis leader lock,
`src/workers/` extraction). The `hub.py` docstring now correctly separates the worker-safe
claim (Redis Streams fan-out) from the non-worker-safe one (process-local suppression set)
and points at `scope_control.py`. The previous blanket "multi-uvicorn-worker safe" line is
gone. The `redis_factory` docstring fix (synchronous zero-arg factory returning an async
client) matches the actual call pattern in `subscribe`.

The `_supervised → supervised`, `_run_connector → run_connector`, `_ScopeContext →
ScopeContext` renames are consistent across `runtime.py`, `runtime_builder.py`, and all
test call sites; no remaining underscore-private cross-module imports.

## 5. Web hook extraction boundaries

**PASS.** Both extractions are faithful:

- **`useChartRangeSelect`** (`web/lib/`): line-for-line the inline logic from
  MonitoringView/AnomaliesView — synchronous `beginWindowChange` dim, debounced URL write
  (magic 200 now the named `RANGE_WRITE_DEBOUNCE_MS`), cancel on unmount. Two call sites
  justify the shared hook. Unit-tested (3 tests).
- **`useChannelVisibility`** (`web/features/monitoring/hooks/`): the session-memory /
  filter / jump-preselect / manual-toggle state machine moved verbatim. Only two
  identity-level deltas, neither behavioral: the unfiltered branch returns a defensive
  copy (`[...discovered]`), and `toggleChannel` is now `useCallback`-memoized.
  `measureInteraction` profiling retained.
- **Chart baseline scoping** is the one *intended* behavior change: baselines are now
  keyed `pageId:tenant:gh` instead of a single global sessionStorage slot, so Reset /
  double-click can no longer restore another page's or scope's window. The `scopeKey` /
  `pageId` props are required (not optional) on `ChartResetButton`, `ScoreTimeline`,
  `SensorTimeSeriesChart`, `SharedViewControls`, and `DateRangeControls`, so tsc enforces
  full threading — all five pages pass one. `UPlotChart` correctly guards double-click on
  both the handler and the scope key. Inverted-window and cross-scope tests added.
- **`useTableDragPan` / `DataTable`**: replacing the implicit `tbody tr[tabindex]` probe
  with an explicit `data-row-selectable` contract is a genuine boundary improvement — the
  gesture owner is now declared by the row producer, not inferred from a styling/behavior
  side effect. Both sides changed in lockstep.

**Note (non-blocking):**

- **N9 — scope-key composition duplicated.** The template literal
  `` `${pageId}:${tenant}:${gh}` `` is written in four places (DateRangeControls,
  LiveToggle, MonitoringBody, AnomaliesBody). Extract a `chartScopeKey(pageId, tenant, gh)`
  helper next to `chartBaseline.ts` so the key format cannot drift between writer and
  reader. (QA's recommendation to add `useChannelVisibility` unit tests also stands.)

## 6. Security: tenant scoping in `tenant_data_wipe` extraction

**PASS.** Compared predicate-by-predicate against the pre-split route code: every DELETE
retains its `tenant_id` (+ `greenhouse_id` where the table has one) filter —
SensorReading, RawReading, Incident, LearningLog scoped to tenant+greenhouse;
IncidentAction scoped through tenant-scoped incident IDs; OperatorFeedback via
`customer_id` (legacy tenant key, commented); MetricsSnapshot tenant-wide with the
intentional-blast-radius WHY comment (finding #14). The timestamp fallback in
`wipe_live_demo_data` remains inside the tenant+greenhouse-scoped outer WHERE. Suppression
and stream-clear keep their never-raise, warn-with-context semantics; no new inputs reach
SQL outside SQLAlchemy expressions; nothing sensitive is logged. No security regression.

## 7. Cache-key fallback (`_safe_args_repr`)

**PASS.** Minor #12 fix is architecturally clean: primitives repr'd verbatim, everything
else collapsed to a stable type name, single-element tuple repr preserved. The primary
request-URL path is untouched; only the no-request fallback changes, and the tests pin
both stability (identity changes → same key) and sensitivity (value changes → different
key).

---

## Pre-push observations (outside review scope)

- `git status` shows `models/customer_e2e_model.pt` and `models/pipeline_cust_model.pt`
  modified. They are excluded from this review per instructions — confirm the checkpoint
  changes are intentional before push (guardrail: no checkpoint without metadata).

---

## Verdict

**PASS WITH NOTES**

No blocking findings. All nine notes (N1–N9) are non-blocking hygiene items; the only one
with an operational edge is **N7** (alembic 017 deploy-ordering window), which is mitigated
by the documented single-worker deploy and an idempotent backfill — record the re-run step
in the deploy runbook. Safe to push to main.
