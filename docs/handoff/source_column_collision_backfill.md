# `raw_readings.source` backfill — measure and propose, do NOT execute

Companion to the code fix in this branch (`fix/source-column-collision`):

- Item 1 (`src/vendors/csv_adapter.py`, `src/config.py`, `src/db/models.py`):
  new CSV uploads now write a fixed `CSV_UPLOAD_RAW_SOURCE = "csv"` marker
  into `RawReading.source` instead of the CSV's own timestamp-column header
  string.
- Item 2 (`src/api/services/tenant_data_wipe.py`): `wipe_live_demo_data`'s
  `SensorReading` fallback branch (`source IS NULL` + shared timestamp with a
  live `RawReading`) is deleted outright.

Neither item touches **existing** rows. This document is the measurement and
the proposed backfill for those — SELECT-only SQL plus reviewable `UPDATE`
text. Nothing here has been run against any database, local or remote.

**Side benefit of item 1, not previously reported:** `RawReading.source` is
`String(64)` (`src/db/models.py:459`). Before item 1, a CSV whose
timestamp-column header exceeded 64 characters wrote that header string
verbatim into `source`, which raised `StringDataRightTruncation` on
Postgres while the identical upload succeeded on SQLite (SQLite does not
enforce `VARCHAR` length). The fixed `'csv'` literal item 1 writes instead is
three characters, permanently under the limit, so this latent 500 cannot
recur on any future upload. No test added for this: asserting it would
require Postgres-only truncation behavior that SQLite cannot reproduce, so
the assertion would be skipped locally or pass vacuously everywhere this
suite actually runs.

## Why old rows still matter

`wipe_live_demo_data`'s `RawReading` delete clause is unchanged by this
branch and is not a bug — it is the correct, provenance-based filter:

```python
live_raw_filter = [
    RawReading.tenant_id == tenant_id,
    RawReading.greenhouse_id == greenhouse_id,
    or_(
        RawReading.source == LIVE_RAW_SOURCE,
        RawReading.source.like(f"{LIVE_RAW_SOURCE}:%"),
    ),
]
...
raw_result = await db.execute(delete(RawReading).where(*live_raw_filter))
```

**Correction against an earlier draft of this document:** the exact-match
arm (`source == 'live'`) is NOT reachable from a CSV header at all.
`ts_candidates` (`src/vendors/csv_adapter.py:426-431` as of this branch; `:419-424` pre-item-1) only considers a
column whose lowercased header contains `timestamp`, `datetime`, `date` or
`time`; a bare header of `live` contains none of those and is never even
inspected as a timestamp column, so it can never become `source`'s value in
the first place. Only the `LIKE 'live:%'` arm is reachable — via a header
such as `live:timestamp` (contains `timestamp`, clears the `update`/`created`
exclusions) stored **verbatim**, because `parse_raw_rows` reads
`chunk.columns` before any header-cleaning. `live:timestamp` is the realistic
exploit header, not `live`.

Any existing `RawReading` row whose `source` starts with `'live:'` —
because that was the CSV's timestamp-column header (e.g. `live:timestamp`),
written before item 1 shipped — still matches this filter and is still
deleted by a live-data wipe. Item 1 stops the bleeding for new uploads; it
does not retag what is already in the table. The backfill below is what
closes that gap for old rows.

**A fourth writer, found by grep against the measurement predicate below,
not by this reasoning:** `scripts/backfill_live_raw_readings.py:75` inserts
`'live-backfill' AS source` for `RawReading` rows it backfills from
`sensor_readings`. That value does not match the wipe filter above
(`'live-backfill'` is neither `== 'live'` nor `LIKE 'live:%'`), so it is not
a wipe-risk the way a `'live:%'` header is — but it is a real, intentional
provenance marker, not pollution, and the measurement and backfill below
must not treat it as the latter. See the whitelist in the next section.

**Pre-existing, not this branch's problem:** the same wipe filter also does
not match `'live-backfill'`, so those rows are already un-wipeable by the
live-wipe path — recorded 2026-07-24, `AGENT_MEMORY.md:2059`. Not fixed
here.

## Known `raw_readings.source` markers (complete as of this branch)

Every intentional writer of `raw_readings.source`, confirmed by grep across
`src/` and `scripts/`:

| Marker | Shape | Writer |
| --- | --- | --- |
| `'live'` | exact | `src/live/persistence.py:168,198` |
| `'live:<...>'` | prefix `'live:%'` | **not written by current code** — legacy CSV-header pollution only, see above |
| `'synthetic:<...>'` | prefix `'synthetic:%'` | `src/config.py` `synthetic_source()` (ADR-010) |
| `'live-backfill'` | exact | `scripts/backfill_live_raw_readings.py:75` |
| `'csv'` | exact | `src/config.py` `CSV_UPLOAD_RAW_SOURCE` — the fixed literal item 1 writes for every new upload |

This table is the single whitelist every statement below uses. A value that
is not one of these five shapes is pollution — a stale CSV
timestamp-column header predating item 1. An earlier draft of this document
excluded only three of these five shapes from the measurement and backfill
predicates (missing `'live-backfill'` and `'csv'` itself), which misread
every `'live-backfill'` row as pollution and would have overwritten it.
Extending this table when a new writer appears, rather than adding another
one-off `NOT LIKE` clause to a single statement, is what keeps the
measurement and the backfill from disagreeing with each other again.

## Measurement — SELECT only

Counts every `raw_readings.source` value that is not one of the five known
markers above and is not `NULL`, grouped per tenant/greenhouse/value:

```sql
SELECT
    tenant_id,
    greenhouse_id,
    source,
    COUNT(*)           AS row_count,
    MIN(timestamp)      AS earliest_ts,
    MAX(timestamp)      AS latest_ts
FROM raw_readings
WHERE source IS NOT NULL
  AND source NOT IN ('live', 'csv', 'live-backfill')
  AND source NOT LIKE 'live:%'
  AND source NOT LIKE 'synthetic:%'
GROUP BY tenant_id, greenhouse_id, source
ORDER BY tenant_id, greenhouse_id, row_count DESC;
```

Per-tenant rollup (how many rows, how many distinct header strings):

```sql
SELECT
    tenant_id,
    COUNT(*)                    AS polluted_row_count,
    COUNT(DISTINCT source)      AS distinct_header_values
FROM raw_readings
WHERE source IS NOT NULL
  AND source NOT IN ('live', 'csv', 'live-backfill')
  AND source NOT LIKE 'live:%'
  AND source NOT LIKE 'synthetic:%'
GROUP BY tenant_id
ORDER BY polluted_row_count DESC;
```

A third query worth running alongside these, since it is the one that
actually determines urgency — rows that collide with the reserved prefixes
**today**, which the two queries above exclude by construction:

```sql
SELECT tenant_id, greenhouse_id, source, COUNT(*) AS row_count
FROM raw_readings
WHERE source = 'live' OR source LIKE 'live:%' OR source LIKE 'synthetic:%'
GROUP BY tenant_id, greenhouse_id, source
ORDER BY row_count DESC;
```

Any row returned by the third query that was never actually written by the
live pipeline or the inject-test endpoint is a live wipe away from being
deleted by accident. The orchestrator-reported `tenant_1/gh_A` example
(`source='Timestamp'`, 104,921 rows) does not appear in this third query — the
literal header `"Timestamp"` does not collide with `'live'`/`'live:%'` — but
whichever tenants *do* show up in it are the ones where the defect this
branch fixes is not hypothetical.

## Proposed backfill — reviewable text only, NOT executed, NOT a migration

Two separate statements, not one. The first retags ordinary CSV-header
pollution using the whitelist above and, deliberately, does **not** touch
`'live:%'` rows. The second closes the `'live:%'` gap on its own, with its
own justification — folding it into the first statement as one more
exclusion is exactly the shape of mistake that produced the whitelist
section above.

### Statement 1 — retag CSV-header pollution

```sql
UPDATE raw_readings
SET source = 'csv'
WHERE source IS NOT NULL
  AND source NOT IN ('live', 'csv', 'live-backfill')
  AND source NOT LIKE 'live:%'
  AND source NOT LIKE 'synthetic:%';
```

`'csv'` is the literal value of `CSV_UPLOAD_RAW_SOURCE` (`src/config.py`) —
deliberately hand-typed here rather than imported, for the same reason
migration 025's docstring gives for not importing `SYNTHETIC_SOURCE_PREFIX`:
a migration (or, here, a reviewed ad-hoc statement) must stay valid against
the code as written; importing an app constant would let a later rename
silently change this statement's meaning.

Scope note: this predicate only ever touches rows where `source IS NOT NULL`.
A CSV-uploaded `RawReading` row has never had a `NULL` source historically —
every row that survives `parse_raw_rows`'s timestamp `dropna` had at least one
matching timestamp candidate column, so `source` was always a non-empty
header string, never `NULL` or empty string. `NULL` therefore is not a state
this backfill needs to touch, and is left alone.

### Statement 2 — retag `'live:%'` rows

This is the dangerous case "Why old rows still matter" describes above: a
row whose `source` starts with `'live:'` because a CSV header like
`live:timestamp` landed there verbatim before item 1 shipped. Statement 1
excludes this pattern on purpose, matching the wipe filter's own reserved
prefix — but excluded here does not mean left in place. It is a separate,
independently-justified statement, not a gap this document leaves open:

```sql
UPDATE raw_readings SET source = 'csv' WHERE source LIKE 'live:%';
```

Justification for treating every `'live:%'` row as CSV pollution rather than
a real live row, verified against the current codebase: `src/live/persistence.py:168`
and `:198` are the only two writes of `raw_readings.source = "live"`
anywhere in `src/`, and both write the bare literal `"live"` — neither ever
writes a `'live:'`-prefixed value. The only other `'live:'` string in `src/`
is `src/live/hub.py:43`'s Redis stream key, an unrelated namespace. Nothing
in the current codebase writes `'live:<...>'` to this column, so every
existing `'live:%'` row is CSV-header pollution by construction, not a real
live row this statement would be wrong to retag.

Measured against the architect review's prod snapshot (reported to this
branch, not independently re-run here per this task's no-prod-access
constraint): **zero** rows match `source LIKE 'live:%'` today. Recorded here
2026-10-19 so a future reader knows this statement was a no-op when written,
not that the gap never existed — it closes the one real risk this document
names, at zero present cost.

Not proposed as a migration, either statement: a migration's `upgrade()`
runs unconditionally on every deploy, including against tenants an operator
has not reviewed. An ad-hoc, manually-run `UPDATE` after reviewing the
measurement queries' output per-tenant is the appropriate level of ceremony
for a data correction with no schema change behind it.

## What breaks while old header values remain (i.e., if nobody runs the backfill)

- **`wipe_live_demo_data`'s `RawReading` filter** (see "Why old rows still
  matter" above) — the only path with a real, measurable blast radius. A row
  whose header happened to start with `'live:'` (e.g. `live:timestamp` — a
  bare `live` header is unreachable, see the correction above) is deleted by
  any live-data wipe on that scope, exactly like a real live row would be.
- **`gas_anomalies.py:_source_is_real`** — `or_(col.is_(None), ~col.like('synthetic:%'))`.
  Any non-`synthetic:`-prefixed value, including an old header string like
  `"Timestamp"`, passes as "real" — identical to what `'csv'` would do. No
  behavior difference; this consumer is unaffected either way.
- **`src/db/provenance.py` (ADR-010 `case_cols_for`)** — does not apply to
  `RawReading` at all; only `SensorReading` sets `PROVENANCE_TRACKED = True`
  (`src/db/models.py:42`). `RawReading` upserts pass `source` in
  `update_cols` unconditionally (`src/api/services/csv_persist.py:246-262`,
  `src/api/routes/upload.py:176-184`), so any timestamp that gets re-uploaded
  after item 1 ships self-heals to `'csv'` on that one row without the
  backfill — but only for timestamps that are actually re-uploaded, which is
  not a reliable path to close the gap tenant-wide.
- **`/api/readings/window` and `/api/readings/recent`**
  (`src/api/services/readings_service.py:_raw_records_to_rows`,
  `fetch_merged_readings`) — `source` rides through to the JSON `rows` array
  unchanged (`-> dict[str, Any]`, no `response_model` stripping it). Checked
  the web frontend (`grep -rn` across `web/` for `.source` usage, matching
  `eventsource`/`datasource` excluded): no consumer reads this field. It is
  dead weight in the payload, not a functional break.
- **Streamlit dashboard** — `src/dashboard/components/sensor_columns.py`'s
  `_METADATA_COLUMNS` set already excludes `"source"` from sensor-column
  classification, so a stale header value is never charted as if it were
  sensor data. The dashboard's one visible "Source" column
  (`src/dashboard/views/1_Active_Monitoring/2_Climate_Data.py:252`) reads
  `anomaly_events.py`'s own literals (`"persisted_score"`, `"reading"`,
  `"incident"`), not `RawReading.source` — confirmed, not re-derived, per
  this task's brief. `src/dashboard/components/charts.py:query_raw_readings`,
  the one dashboard function that does fetch raw rows with their `source`
  field, has no callers anywhere in `src/dashboard/` today — dead code, not a
  rendering path.

Net: the backfill's only real-world consequence, run or not, is whether the
`wipe_live_demo_data` `RawReading` filter can accidentally sweep up an old
CSV upload. Everything else downstream already treats the stale header
string as inert, opaque text.
