# `anomaly_episodes` schema handoff (Part B implementer spec)

Schema source of truth: migration `alembic/versions/026_add_anomaly_episodes.py`
and the `AnomalyEpisodeRecord` model in `src/db/models.py`. This document is a
self-contained spec for the Part B work that schema deliberately leaves
undone — it should be readable without the review thread that produced it.

Part B is: wiring the stuck-fault detector to populate the two sample-count
columns, performing the actual INSERT / UPDATE-on-conflict at fault
onset/continuation/release, and adding the application-layer `fault_type`
validation this schema does not enforce in the database. None of that exists
yet — as of this writing nothing in the codebase INSERTs into this table.

---

## 1. Two "episode" concepts — do not conflate them

| | `anomaly_episodes` (this table) | `GET /api/anomalies/episodes` |
|---|---|---|
| Written | by a detector, at detection time | never — computed on every request |
| Persisted | yes, one durable row per fault window | no |
| Grouping key | `(tenant_id, greenhouse_id, channel, detector, onset_ts)` | `(channel, detector, direction)` over already-flagged `sensor_readings` rows |
| Code | `src/db/models.py::AnomalyEpisodeRecord` | `src/api/services/anomaly_episode_service.py` |

Two more unrelated, pre-existing, in-memory-only concepts share the English
word "episode" but have no table and are NOT this schema:
`AnomalyEpisode` (dataclass, `src/models/episode_grouper.py:40`, upload-path
grouping) and `_AnomalyEpisode` (`src/live/sources/mock_episodes.py:68`, live
mock source). The ORM class for this table is named `AnomalyEpisodeRecord`
specifically so it never has to be imported under an alias alongside either
of those two. The TABLE NAME collision with the query-time path's output
shape (`anomaly_episodes`) is a separate, deliberate, already-reviewed
decision — don't rename the table to "fix" it.

---

## 2. Full schema

### Table: `anomaly_episodes`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `String(36)` PK | no | UUID4 string, matches `Incident`'s convention |
| `tenant_id` | `String(255)` | no | Multi-tenant owner; every query must filter on it |
| `greenhouse_id` | `String(255)` | no | Zone within a tenant; default `'default'` |
| `channel` | `String(255)` | no | Raw column name the fault was detected on, e.g. `'temperature'` |
| `detector` | `String(64)` | no | Detector name that wrote this row, e.g. `'stuck_fault'`, `'band_breach'`, `'calibration_drift'` |
| `fault_type` | `String(64)` | no | Fault label, e.g. `stuck_fault`, `calibration_drift`, `temp_band_high`. **No DB CHECK** — see §5 |
| `onset_ts` | `DateTime(tz)` | no | First sample inside the fault window |
| `end_ts` | `DateTime(tz)` | no | Last frozen sample — see §3, do not confuse with `release_ts` |
| `detected_ts` | `DateTime(tz)` | no | When the detector actually wrote the row |
| `release_ts` | `DateTime(tz)` | yes | First non-frozen sample after the freeze ends; NULL while `is_ongoing` |
| `duration_seconds` | `Integer` | yes | Stored-and-derived, `end_ts - onset_ts`; see §3 |
| `frozen_sample_count` | `Integer` | yes | Computed by `WallclockStuckDetector`, persisted by `anomaly_episode_writer.py`; see §6 |
| `raw_slot_count` | `Integer` | yes | Computed by `WallclockStuckDetector`, persisted by `anomaly_episode_writer.py`; see §6 |
| `severity` | `String(16)` | no | e.g. `critical`, `warning`, assigned at detection |
| `trigger_value` | `Float` | yes | Representative raw value, e.g. the frozen reading; NULL for reconstruction-error-based detectors with no single value |
| `is_ongoing` | `Boolean` | no | Default `false`. See §4 for the CHECK tying this to `release_ts` |
| `source` | `String(64)` | yes | Provenance tag, mirrors `sensor_readings.source`; NULL for a real detection, `'synthetic:<type>'` for an inject-test row |

### Constraints

- `pk_anomaly_episodes` — PRIMARY KEY (`id`)
- `uq_anomaly_episodes_tenant_gh_channel_detector_onset` — UNIQUE
  (`tenant_id`, `greenhouse_id`, `channel`, `detector`, `onset_ts`) — **this
  is the upsert key**, see §7.
- `ck_anomaly_episodes_release_ts_null_while_ongoing` — CHECK
  (`NOT is_ongoing OR release_ts IS NULL`) — one direction only, see §4.

### Indexes

- `ix_anomaly_episodes_tenant_gh_channel_onset`
  (`tenant_id`, `greenhouse_id`, `channel`, `onset_ts`) — the detector-
  agnostic time-range scan: "episodes for this tenant/greenhouse/channel in
  `[start, end]`", regardless of which detector wrote them. Kept alongside
  the unique constraint above rather than dropped as redundant: the unique
  index's column order puts `detector` before `onset_ts`, so a query that
  ranges over `onset_ts` without also filtering on `detector` can only use
  the unique index's first three columns and loses ordering on `onset_ts`.
  This index exists specifically to keep that scan a single ordered index
  scan.
- `ix_anomaly_episodes_ongoing_tenant_gh` — **partial** index on
  (`tenant_id`, `greenhouse_id`) `WHERE is_ongoing` — "which episodes are
  currently open for this tenant/greenhouse". Both `postgresql_where=` and
  `sqlite_where=` are set on the same predicate (`is_ongoing`); verified
  present on both a real Postgres 15 container (`\d anomaly_episodes` shows
  `... btree (tenant_id, greenhouse_id) WHERE is_ongoing`) and SQLite
  (`sqlite_master.sql` for the index literally ends `WHERE is_ongoing`).
  Partial, not a global `(is_ongoing, onset_ts)` boolean-leading index,
  because there is no reader of this table yet to justify indexing the
  (eventually) large majority of rows where `is_ongoing=false`, and every
  read in this app filters by tenant anyway.
- `ix_anomaly_episodes_fault_type_detector` (`fault_type`, `detector`) —
  reporting queries like "all `stuck_fault` episodes from the `statistical`
  detector".
- No standalone `tenant_id` index — the first composite index's leftmost
  prefix covers tenant-only lookups.

---

## 3. Timestamp contract (mirrors `FaultWindow`, `src/models/stuck_fault_detector.py:172`)

Two names that sound similar and are **not** interchangeable:

- **`end_ts`** = `FaultWindow.last_frozen_ts` = the LAST FROZEN sample. Known
  as soon as the fault is declared; updated as an ongoing fault continues.
- **`release_ts`** = the FIRST NON-FROZEN sample after the freeze ends. NULL
  while `is_ongoing` is true — the release hasn't happened yet.

- **`onset_ts`** — first sample inside the fault window.
- **`detected_ts`** — when the detector actually wrote this row, lagging
  `onset_ts` by whatever consecutive-sample threshold the detector requires
  before calling it a fault.
- **`detection_latency`** (`detected_ts - onset_ts`) is deliberately NOT a
  column — compute it at query time. Don't add a stored column for it.
- **`duration_seconds`** is stored-and-derived from `end_ts - onset_ts` (the
  frozen span observed so far), recomputed on every writer update — not
  independently authoritative, and not `release_ts - onset_ts` (that longer
  stuck-to-recovery span is for alert suppression, not for describing freeze
  length).

---

## 4. The `release_ts` / `is_ongoing` CHECK — know which direction is enforced

`ck_anomaly_episodes_release_ts_null_while_ongoing`:
`CHECK (NOT is_ongoing OR release_ts IS NULL)`

This enforces **only**: an ongoing fault cannot already have a release
timestamp. It does **not** enforce the converse — `is_ongoing = false` does
NOT require `release_ts IS NOT NULL`. Drift and band-breach episodes can
close (`is_ongoing = false`) without ever observing a first normal sample to
release against, unlike a stuck-fault freeze, which always has one — that is
the legitimate closed-but-unreleased case this CHECK must allow.
**Do not tighten this to a two-way constraint** — doing so would reject that
legitimate case.

**Explicit contract — do not confuse with the above:** an open freeze that is
still frozen when the input file ends is **`is_ongoing = true`**, `release_ts`
NULL, `end_ts` = the last frozen sample. It is NOT closed. Reaching
end-of-file does not flip `is_ongoing` to false; only an actual release (a
first normal sample observed) does that.

---

## 5. `fault_type` has no DB CHECK — know the cost

Unlike `incidents.ck_incidents_fault_type` (migrations 011/016/019),
`fault_type` on this table has no CHECK constraint. Mirroring that pattern
would create a THIRD hand-edited vocabulary location, and a migration must
not import the application's fault-type list (risk of migration history
breaking if that list is later renamed/moved — see migration 025's
docstring). **Consequence: a typo'd `fault_type` is silently accepted by the
database today.** Part B owns adding application-layer validation (e.g. a
Pydantic/enum check in the writer path) before any detector writes rows for
real. This is a known, accepted gap, not an oversight to "fix" by adding a
CHECK later without reopening the vocabulary-duplication trade-off above.

---

## 6. The two sample counts: where they come from

`FaultWindow` (`src/models/stuck_fault_detector.py:172`) carries `channel`,
`onset_ts`/`onset_dt`, `release_ts`/`release_dt`, `last_frozen_ts`/
`last_frozen_dt`, `frozen_sample_count`, `raw_slot_count` and `has_fired`.
`WallclockStuckDetector` computes both counts inside its consume loop as
each freeze progresses — neither is derived afterwards from the file or
from `sensor_readings`. `src/api/services/anomaly_episode_writer.py`
persists both columns, and rejects a stuck row whose counts are null,
which is why `nullable` in the schema (§2) is still the correct type and
not a leftover from before the counts were wired up.

Definitions:

- **`frozen_sample_count`** — the number of *finite* samples the detector
  actually observed stuck at the frozen value, counted over
  `[onset_ts, end_ts]`.
- **`raw_slot_count`** — expected readings at the pinned cadence across the
  freeze: `(end_ts - onset_ts) / cadence + 1`. **Not** a tally of file rows.
- Cadence is pinned once per freeze and never recomputed: branch 1, the
  pre-onset median when at least `CADENCE_PIN_MIN_INTERVALS` (3) intervals
  exist; otherwise branch 2, the median of the freeze's own intervals at
  gate fire.
- The gap (`raw_slot_count - frozen_sample_count`) counts readings missing
  FOR THIS CHANNEL, whatever the cause. That includes whole rows the
  upload adapter dropped because a *different* required sensor was null.
  It is **not** proof this sensor went offline.
- `CSVAdapter` drops rows with a null required sensor before detection,
  which is why the definition is expected slots, not rows counted.

---

## 7. Upsert key — the conflict target for the write path

`uq_anomaly_episodes_tenant_gh_channel_detector_onset`:
UNIQUE (`tenant_id`, `greenhouse_id`, `channel`, `detector`, `onset_ts`)

This is the key Part B's writer must use as the `ON CONFLICT` / merge target:

- On fault **onset**: INSERT a new row. If a row with the same
  `(tenant_id, greenhouse_id, channel, detector, onset_ts)` already exists
  (e.g. because the same input file was re-uploaded), the insert must not
  create a duplicate episode.
- On fault **continuation**: UPDATE `end_ts` (and the derived
  `duration_seconds`) on the same conflict target, rather than inserting a
  new row per sample.
- On fault **release**: UPDATE `release_ts` and flip `is_ongoing` to false
  on the same conflict target.

This also means a re-upload of a file that was already ingested is
idempotent with respect to this table: the same onset re-triggers an UPDATE
of the existing row, not a second row.

Episode persistence is gated the same way the surrounding persistence
already is: `persist_db is not None` in the sync path
(`src/api/services/cold_run_upload.py`) and the `persist_data` payload flag
in the async path (`src/workers/upload_consumer.py`), so a preview-only
upload writes NO `anomaly_episodes` rows. This is approved behaviour, not a
caveat.

---

## 8. Part B scope summary

Everything below is explicitly out of scope for migration 026 and is owned
by whoever implements Part B:

1. Add `frozen_sample_count` / `raw_slot_count` tracking to the stuck-fault
   detector's state (§6).
2. Implement the actual INSERT / UPDATE-on-conflict writer against the
   upsert key (§7) at fault onset, continuation, and release.
3. Add application-layer `fault_type` validation (§5) — the database will
   not catch a typo.
4. Generalize the writer so band-breach and calibration-drift detectors can
   also write this table (the schema is already detector-agnostic; the
   writer code is not written yet for any detector).
