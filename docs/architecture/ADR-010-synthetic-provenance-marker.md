# ADR-010: `source` as the Synthetic Provenance Marker

Status: **Accepted** — implemented in this change (migration 025 + code).
Date: 2026-10-04
Scope: `src/api/services/synthetic_anomalies.py`,
`src/api/services/synthetic_anomaly_generators.py`,
`src/api/services/tenant_data_wipe.py`, `src/db/models.py`, `src/db/provenance.py`,
`src/db/upsert.py`, `alembic/versions/025_tag_synthetic_source.py`

## Problem

`sensor_readings.anomaly_type` had exactly one writer of a non-NULL value:
the inject-test endpoint (`synthetic_anomalies.py`). Two call sites treated
`anomaly_type IS NOT NULL` as a de-facto "this row is synthetic" flag:

- `delete_synthetic_anomalies` ("remove all/one synthetic") keyed removal
  off it.
- `wipe_live_demo_data`'s fallback branch used `anomaly_type IS NULL` as an
  inverse guard to avoid wiping inject rows.

Real anomaly detection is about to start persisting `anomaly_type` too
(UPPER_CASE labels from `setpoint_scada_detector` / `band_breach_detector`,
per `fault_classification.py`). Once it does, both call sites break: removal
destroys genuine detections, and the live-wipe fallback leaks live rows that
happen to carry a label.

## Decision

Mark synthetic rows with `SensorReading.source = "synthetic:<type>"`
(`"synthetic:context"` for the background filler rows), mirroring the
`RawReading` convention already in place at `synthetic_anomalies.py`.
`anomaly_type` keeps recording *what kind* of label a row carries (injected
type or future detector fault name); `source` records *provenance* — which
is what removal and wipe logic actually need.

Migration 017 added `source` to `sensor_readings` for exactly this kind of
provenance tagging (it already carries `'live'`). Reusing it needs no new
column and no new concept.

### Alternatives considered

- **New dedicated column (e.g. `is_synthetic` boolean).** Rejected: a second
  column tracking the same concept `source` already tracks would let the two
  drift apart, and gives migration 025 a DDL step instead of a pure backfill.
- **A flag inside `RawReading.data`.** Rejected: `SensorReading` is the row
  that removal and wipe actually query; deriving its provenance from a
  sibling table's JSON blob means a join or a second query on every call,
  and `RawReading` rows do not always exist (CSV uploads without raw column
  data).
- **Reusing `score_scale == "synthetic"`.** Rejected: `score_scale` already
  carries a different, overlapping-but-distinct meaning (the scale the score
  was computed on: live/upload/batch/synthetic/legacy/unknown), and ties
  removal to a column that can legitimately take other values for a
  synthetic row's lifecycle. `source` is the column the project already
  documents as the provenance tag (migration 017).

## The backfill predicate: P0 / P1 / P2

Migration 025's `UPDATE ... WHERE` predicate went through three drafts.
Each is wrong in a different direction, and the two failure directions are
opposites: **P0 deletes real data** (it backfills `source` onto rows that
were never synthetic, so a later "remove all synthetic" destroys genuine
detections); **P1 and P2 leave a synthetic row undeletable** (they backfill
fewer rows than were actually synthetic, so some synthetic rows keep
`source IS NULL` forever and "remove all synthetic" can never reach them).
An undeletable test row is a nuisance; deleted real data is not recoverable
— which is why P0 was rejected outright rather than shipped with a caveat.

- **P0** — `anomaly_type IS NOT NULL OR score_scale = 'synthetic'`. Once real
  detection starts persisting `anomaly_type`, this wrongly tags `real_detection`,
  `real_lstm_only`, and `real_BATCH_INFERRED` rows as synthetic.
- **P1** — `score_scale = 'synthetic'` alone. Misses `syn_BATCH_INFERRED` and
  `syn_BATCH_NO_RAW`: both are injected rows whose `score_scale` was later
  overwritten to `'batch'` by `apply_anomaly_scores`, so the column no longer
  says "synthetic" even though the row is.
- **P2 (shipped)** — `score_scale = 'synthetic' OR EXISTS(sibling raw_readings
  row with source LIKE 'synthetic:%' at the same tenant/greenhouse/timestamp)`.
  Corroborating with the sibling `RawReading` recovers `syn_BATCH_INFERRED`
  (its raw row still carries the marker). It misses only `syn_BATCH_NO_RAW` —
  see residual (b) below.

| Row shape            | P0 tags it? | P1 tags it? | P2 tags it? |
|-----------------------|:-----------:|:-----------:|:-----------:|
| `real_detection`      | **wrong — yes** | no | no |
| `real_lstm_only`      | **wrong — yes** | no | no |
| `real_BATCH_INFERRED` | **wrong — yes** | no | no |
| `syn_context` / `syn_spike` | yes | yes | yes |
| `syn_BATCH_INFERRED`  | yes | **wrong — no** | yes |
| `syn_BATCH_NO_RAW`    | yes | **wrong — no** | **no (known residual)** |

## Query plan

`EXPLAIN (ANALYZE, BUFFERS)` on the shipped P2 predicate against a 100k-row
`sensor_readings` table: the planner picks a **hash semi-join** against
`raw_readings`, scanning `raw_readings` exactly once (`loops=1`). Total
runtime ~260 ms, of which ~125 ms is JIT compilation. No new index is
needed: `uq_raw_readings_tenant_greenhouse_timestamp` exists and would serve
a nested-loop plan instead, but the planner correctly prefers the hash join
over it at this table size, so adding an index to "help" this query would be
solving a problem the planner doesn't have.

## Enforcement layering (`PROVENANCE_TRACKED`)

The ADR-010 reset rule is **declared**, not inferred. `SensorReading.PROVENANCE_TRACKED
= True` (`src/db/models.py`) is a flag on the model; `bulk_upsert_rows`
(`src/db/upsert.py`) checks it and, if set, layers in the CASE expressions
from `src/db/provenance.py` on top of whatever `update_cols`/`case_update_cols`
the caller passed — the rule is not opt-in at the call site.

This sits **below** the call sites deliberately: a future `SensorReading`
upsert added to `upload.py`, `csv_persist.py`, or anywhere else gets the
rule automatically, without its author needing to know the rule exists (the
"future-caller guarantee"). The alternative — each caller remembering to ask
for the provenance CASE columns — fails exactly the way the original
`anomaly_type IS NOT NULL` coupling failed: silently, the day someone adds a
new caller who didn't read this ADR.

The domain knowledge stays out of the generic layer: `src/db/provenance.py`
is the only module under `src/db` that imports `SYNTHETIC_SOURCE_PREFIX` and
the only one that knows which columns carry a detection label or score band.
`src/db/upsert.py` imports no domain constants — it only knows how to ask a
model "do you opt into this rule" and take SET expressions from whoever
answers yes.

## The upsert CASE contract

For every `SensorReading` upsert through `bulk_upsert_rows` (`src/db/provenance.py:case_cols_for`):

- **`source`**: last writer wins. Any non-NULL existing `source` (live or
  `synthetic:*`) is replaced by the incoming value (NULL for a plain CSV
  upload today). An untagged (`NULL`) existing row is left exactly as-is.
- **A provenance column the caller explicitly named in `update_cols`** takes
  the incoming value, unconditionally. The caller owns that column for this
  write; the reset must not reach back over an explicit update.
- **A provenance column the caller omitted** keeps its existing value,
  *except* when the reset below fires.
- **The reset** (take the incoming value — NULL for an upload that omits the
  column) fires only when the existing row's source is synthetic AND the
  incoming row's source is not.

Concretely: an existing `'live'` row plus an upload incoming has `source`
cleared, while `anomaly_type` and the scoring columns are left alone for the
normal rescoring path to handle — the reset only ever fires starting from a
synthetic existing row, never from a live one.

## Rescoring

A synthetic-row-turned-real (the reset case above) lands in exactly the same
state as any fresh upload row — no special-casing. This holds because
`src/api/routes/upload.py` never calls any scoring function itself;
`apply_anomaly_scores` has exactly two callers, `cold_run_upload.py:342` and
`upload_consumer.py:529`, and both treat every row they see identically
regardless of what its `source` was a moment ago.

## Ordering constraint

Migration 025 (the backfill) **must land and be verified before** any code
path starts persisting real `anomaly_type` values to `SensorReading`. If
real detection ships first, a window opens where `anomaly_type IS NOT NULL`
rows exist that are neither synthetic nor backfilled, and the old
`delete_synthetic_anomalies` behavior (this change replaces it) would delete
them. Because of this, the anomaly-episodes migration that accompanies real
`anomaly_type` persistence takes `revision = "026"`, `down_revision = "025"`
— strictly after this one in the chain, never parallel to it.

This ordering is an **API-deploy-time** constraint, not a runtime one: the
worker process never runs `alembic` (only the API does, on startup). Revision
ordering in the alembic chain therefore cannot bind worker code at all — a
worker can run newer or older code than the schema its database is on
regardless of migration order, and the two deploy independently. See
`[[project_api_migration_deploy_skew]]`-style skew for the general hazard
this creates.

## Consequence

A synthetic row written before this fix (`source IS NULL`, `anomaly_type`
set) is unreachable by "remove all synthetic" until migration 025 backfills
it. This is accepted as intended-conservative: silently deleting a row that
does not carry the new marker would reopen the exact ambiguity this ADR
closes. See `test_should_not_delete_a_legacy_unbackfilled_synthetic_row` in
`tests/api/test_synthetic_marker_contract.py`.

## Residual follow-ups

a. **Atomic sensor+raw upserts in upload.py.** Ticket text, verbatim:

   > Make the sensor_readings and raw_readings upserts in upload.py one
   > transaction. On raw failure, roll back both and surface the error
   > instead of logging a warning. When an upload has no numeric raw columns
   > but lands on an existing raw row, define what happens to that row's
   > source.

   Context: today they are separate commits, and the raw one either swallows
   its exception (`upload.py:188-190`) or is skipped entirely when the CSV
   has no numeric raw columns — so "Remove Go Live data" can still delete a
   row that a CSV upload already overwrote. Prod exposure 0 (no `source='live'`
   rows in prod).

b. **`syn_BATCH_NO_RAW`.** Four preconditions must hold simultaneously: (1)
   injected before this fix shipped, (2) into a tenant with no `RawReading`
   history at injection time (`has_raw_cols` was false, so no sibling raw row
   was ever written), (3) later batch-inferred (`score_scale` no longer reads
   `'synthetic'`), all (4) before migration 025 runs. Prod exposure 0.

c. **Live `on_conflict_do_nothing`.** `src/live/persistence.py:384,391` — a
   live event landing at exactly the same timestamp as an injected row is
   silently dropped, and the synthetic row wins (first-writer-wins, not
   last-writer-wins, unlike the `bulk_upsert_rows` path above). Reachable
   only via microsecond timestamp equality between a live event and a
   synthetic injection; effectively zero organic probability.

## Deploy notes

Prod counts at time of writing: 107,637 total `sensor_readings` rows; 2,716
need tagging under predicate P2; 0 rows where `score_scale='synthetic'` but
P2's EXISTS leg would have missed them (the "score_scale-only gap"); the
database role's `statement_timeout` is already 0 (the migration's `SET
LOCAL statement_timeout = 0` is a no-op in prod, present for other
environments); 0 rows are both already carrying a real detector label and
matched by P2 (the "labelled-rows-not-synthetic" check). Expected post-deploy
split: `synthetic:*` on 2,716 rows, `NULL` on 104,921, and `synthetic:unknown`
on 0 rows (the `synthetic:unknown` branch in the migration's CASE only fires
for a row with `anomaly_type IS NULL` matched by the EXISTS leg, i.e. a
`syn_BATCH_NO_RAW`-shaped row recovered some other way — none exist in this
dataset). The post-deploy check against these numbers is run by the user,
not by this agent (no production access from this session).

**Rollback procedure.** A DB-only rollback (`alembic downgrade` to before
025, keeping the new application code deployed) clears every `synthetic:%`
`source` migration 025 set. Because `delete_synthetic_anomalies` filters on
`source LIKE 'synthetic:%'`, "Remove all synthetic" becomes a **silent
no-op** in this state — it returns `deleted_sensor: 0` with HTTP 200, not an
error, since the predicate simply matches zero rows. The no-op property
belongs to `upgrade -> upgrade` (re-running the backfill on an
already-tagged database touches 0 rows, verified on Postgres), **not** to
`upgrade -> downgrade -> upgrade`: the second upgrade in that sequence
re-tags every row the downgrade just cleared, so it is a real (idempotent)
write, not a no-op. Recovering from a DB-only rollback is therefore just
running `alembic upgrade head` again — the backfill is safe to re-run.
