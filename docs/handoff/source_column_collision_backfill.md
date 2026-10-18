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

## Why old rows still matter after items 1 and 2

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

Any existing `RawReading` row whose `source` happens to equal `'live'` or
start with `'live:'` — because that was the CSV's timestamp-column header,
written before item 1 shipped — still matches this filter and is still
deleted by a live-data wipe. Item 1 stops the bleeding for new uploads; it
does not retag what is already in the table. The backfill below is what
closes that gap for old rows.

## Measurement — SELECT only

Counts every `raw_readings.source` value that is not one of the three
reserved provenance markers (`'live'`, `'live:%'`, `'synthetic:%'`) and is not
`NULL`, grouped per tenant/greenhouse/value:

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
  AND source <> 'live'
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
  AND source <> 'live'
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

```sql
UPDATE raw_readings
SET source = 'csv'
WHERE source IS NOT NULL
  AND source <> 'live'
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

Not proposed as a migration: a migration's `upgrade()` runs unconditionally
on every deploy, including against tenants an operator has not reviewed. An
ad-hoc, manually-run `UPDATE` after reviewing the measurement queries' output
per-tenant is the appropriate level of ceremony for a data correction with no
schema change behind it.

## What breaks while old header values remain (i.e., if nobody runs the backfill)

- **`wipe_live_demo_data`'s `RawReading` filter** (see "Why old rows still
  matter" above) — the only path with a real, measurable blast radius. A
  row whose header happened to be `'live'` or start with `'live:'` is deleted
  by any live-data wipe on that scope, exactly like a real live row would be.
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
