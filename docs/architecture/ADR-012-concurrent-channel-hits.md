# ADR-012: Concurrent-Channel Hits — `anomaly_columns` Now, a Hits Table Next

Status: **Accepted** — decision recorded; **no code, tests or migrations ship
with this ADR.** Implementing the interim is a separate task.
Date: 2026-10-15
Scope (files the decision concerns, none of them changed here):
`src/api/services/cold_run_upload.py`, `src/api/services/csv_persist.py`,
`src/api/services/anomaly_group_queries.py`,
`src/api/services/anomaly_ranking.py`, `src/db/models.py`,
`tests/api/test_recovery_tag_fixture_a.py`

Supersedes the "backlog, not work" status that ADR-011 gave this gap under
**Known limitations**. The design exploration behind it is
`docs/architecture/BACKLOG-1-concurrent-channel-storage.md`, which is
deliberately untracked (`docs/architecture/` tracks only `ADR-*.md`,
`upload_detector_convergence.md`, `episode_grouping.md`); everything from it
that the decision rests on is reproduced here.

## Problem

`sensor_readings` is unique on `(tenant_id, greenhouse_id, timestamp)` and
carries four scalar attribution columns — `trigger_channel`,
`trigger_detector`, `detected_severity`, `trigger_value` (`src/db/models.py`).
One timestamp, one channel.

The collapse happens in `build_trigger_data_by_timestamp`
(`src/api/services/cold_run_upload.py:127`), which keeps the single
highest-ranked hit per timestamp under `anomaly_rank_key`
(`src/api/services/anomaly_ranking.py` — severity, then detector tier, then
|z_score|, then score, then timestamp, then column). Every other channel that
fired at that timestamp is dropped from the persisted scalar columns.

Why it matters beyond cosmetics: ADR-011's `is_recovery` tagging deliberately
never suppresses an episode, because a real fault can coincide with a recovery.
But if the coinciding fault scores below another channel at the same
timestamps, the persisted view hides it *before* tagging ever runs. The storage
model masks exactly the case the tagging policy exists to preserve.

## What was measured

All figures below come from the production upload path —
`run_cold_run_upload(..., persist_db=session, chunk_size=500)` — plus the real
`GET /api/anomalies/episodes` endpoint, using the same harness the Task 5 pins
use (`tests/api/_episode_view_fixtures.py`). No `StuckCarry` is hand-built
anywhere, so the imputed-channel phantom-freeze trap does not apply. The
measurement scripts (`bl1_measure.py`, `bl1_episodes.py`, `bl1_raw.py`,
`bl1_rel.py`) lived in the scratchpad and are not in the repo; each was run as
`cd /Users/eugenesidorov/Projects/ai-agronomist && ./venv/bin/python <script>`.

Four harness properties were checked before any number was trusted:

- **`high_anomalies` really is the pre-collapse set.** `_merge_by_timestamp`
  (`cold_run_upload.py`) dedupes only the *LSTM* side per timestamp; it
  iterates `stat_hits` and appends each one, so N statistical columns at one
  timestamp produce N entries. Stuck alerts are appended afterwards
  (`upload_portions.py`).
- **`high_anomalies` is not capped.** The "display cap" named in
  `anomaly_ranking.py`'s header comment lives in `anomalies_page_data.py`; the
  upload response schema (`src/api/schemas/sensor_data.py`) has no limit.
- **The carry path engages.** Fixture A persists 3387 rows at `chunk_size=500`
  = 7 chunks. (`UploadInferenceResponse` has no `total_portions` field, so a
  `getattr(response, "total_portions", None)` probe returns `None` because the
  attribute is absent, not because the carry was idle. Portion count was
  derived from persisted rows instead.)
- **Detector labels were matched by containment, never equality** —
  `"lstm+statistical"` exists. Both methods were run; they agree here only
  because this harness produces no LSTM hits (see the scope limit below).

And the collapse function really is what the database holds: for every flagged
timestamp, the persisted `trigger_channel` equals
`build_trigger_data_by_timestamp`'s winner — **0 mismatches / 89 timestamps**.

### Fixture A — `tests/fixtures/native_dec_stuck_fault.csv`

| quantity | value |
|---|---|
| raw CSV data lines | 3457 |
| rows persisted (post-`CSVAdapter`) | 3387 (70 dropped for NaN temp/humidity) |
| chunks at `chunk_size=500` | 7 |
| hits in `high_anomalies` | 106 (statistical 102, stuck_fault 4, lstm 0) |
| distinct flagged timestamps carrying a channel | **89** |
| channels-per-timestamp distribution | **{1: 72, 2: 17}** — never 3 or more |
| timestamps with ≥2 concurrent channels | **17 (19.1%)** |
| suppressed (losing) channel-rows | **17** |

Winners on collided timestamps: `measured_hd` 6, `measured_pipe_1_temp` 6,
`measured_pipe_2_temp` 5. Losers: `measured_pipe_1_temp` 9,
`measured_pipe_2_temp` 6, `measured_plant_temp` 2. Roughly one flagged
timestamp in five loses a channel, and the loss concentrates in the
pipe-temperature channels.

### Fixture B — `tests/fixtures/native_july_clearsky.csv`

**Zero.** 2016 rows persisted (none dropped), 5 chunks, **0 hits**, 0 flagged
timestamps, **0 collisions**, 0 episodes.

Stated plainly so it is not misread later: the clean July fixture produces no
anomalies at all, so it contributes **no evidence for or against any option**.
It is a true null, not a "near-zero collision rate" result — there is nothing
there to collide. Every frequency claim below rests on fixture A alone.

### Episode-level impact — the number that actually matters

Matched by span overlap on the same channel (see **Two corrections** below for
why the obvious `(channel, start_ts)` match key is wrong):

- 10 statistical detection-time episodes, **102 rows**
- query-time coverage of them: **85 rows** → **17 rows lost**, exactly equal to
  the 17 suppressed channel-rows (an independent cross-check)
- fates: **INTACT 4 · TRUNCATED 5 · SPLIT 1 · ABSENT 0**
- **6 of 10 statistical episodes (60%) are damaged in the query view**

| fate | channel | detection span | n | query result |
|---|---|---|---|---|
| INTACT | calculated_heating_temp | 12-25 09:55→12:05 | 27 | 27 |
| INTACT | measured_hd | 12-16 09:25→10:15 | 11 | 11 |
| INTACT | measured_hd | 12-18 12:40→12:50 | 3 | 3 |
| INTACT | measured_hd | 12-25 09:10→09:50 | 9 | 9 |
| TRUNCATED | measured_pipe_1_temp | 12-16 09:10→09:40 | 7 | 3 (ends 09:20) |
| SPLIT | measured_pipe_1_temp | 12-21 13:00→13:40 | 9 | 2 pieces: n=1, n=6 |
| TRUNCATED | measured_pipe_1_temp | 12-24 13:40→14:10 | 7 | 4 (ends 13:55) |
| TRUNCATED | measured_pipe_2_temp | 12-21 12:15→13:25 | 15 | 11 (ends 13:10) |
| TRUNCATED | measured_pipe_2_temp | 12-24 13:50→14:40 | 11 | 9 (**starts** 14:00) |
| TRUNCATED | measured_plant_temp | 12-18 12:45→12:55 | 3 | **1** (12:55 only) |

Three things the row counts alone do not show:

1. **The damage is systematic per channel, not scattered.** Every INTACT
   episode sits on a winner channel (`measured_hd` ×3,
   `calculated_heating_temp`); every damaged one sits on a loser channel. A
   channel that habitually loses rank is habitually mis-measured.
2. **The collapse can SPLIT one event into two,** manufacturing a gap that is
   not in the data — the same failure mode `anomaly_episode_service.py`'s "ONE
   CONSTRAINT THAT MUST NOT BE VIOLATED" docstring forbids for top-N
   subsetting. The collapse reaches it by another route.
3. **Truncation hits the start as often as the end.** `measured_pipe_2_temp` on
   12-24 appears to begin 10 minutes late; `measured_plant_temp` on 12-18 is
   reduced to a **single row**, whose `duration_seconds` is then 0.0. Start
   shifts matter for ADR-011 specifically, because `is_recovery` is
   start-anchored.

### Does widening flip an `is_recovery` verdict? On fixture A, no.

Measured stuck releases (all on `humidity`, from the persisted
`anomaly_episodes` rows): 12-15 10:55, 12-16 09:15, 12-18 11:20, 12-25 08:55,
against `EPISODE_GAP_SECONDS=600` and `RECOVERY_KNOCKON_WINDOW_HOURS=4.0`
(`src/config.py`). Deriving from those two measured inputs — this is arithmetic
on measurements, not a separate measurement — every damaged episode's
detection-time start falls in the same tag/no-tag bucket as its truncated
start. So on this fixture widening is a **span-only correction with no tagging
churn**. The general risk is real, because tagging is start-anchored and the
collapse moves starts, so **re-running the ADR-011 tagging assertions is
required under whichever option ships, not optional.**

### Scope limit of these measurements

Stated because the numbers above will outlive the memory of how they were
taken:

- The Task 5 harness's `mock_detector()` returns a constant 0.05, so **zero
  LSTM hits** arise. This does not bias the channel-collision counts — LSTM
  hits carry `column=None`, so they never compete for a channel, and `lstm` is
  the lowest tier in `anomaly_rank_key`, so a real LSTM run cannot change which
  channel wins; it would only relabel `trigger_detector` to
  `"lstm+statistical"`. But **collision frequency on production traffic is not
  measured by this work.**
- Fixture A is **one engineered stuck-fault file**, not a sample of production
  traffic. "19.1% of flagged timestamps" is a property of that file.

## The reframing: this is not a storage gap

This is the heart of the ADR, and it inverts the original problem statement.

`build_anomaly_metadata_by_timestamp` (`src/api/services/cold_run_upload.py:109`)
already collects **every** channel that fired at each timestamp, and
`csv_persist` writes the complete set to `raw_readings.data["anomaly_columns"]`
(`src/api/services/csv_persist.py:152` on the insert path and `:387` on the
read-modify-write merge path).

Measured on fixture A: of the 17 collided timestamps, **17 of 17 already carry
the complete channel set**. Example, 2026-12-16 09:25 —
`anomaly_columns: ["measured_hd", "measured_pipe_1_temp"]` while
`sensor_readings.trigger_channel` holds `measured_hd` alone.

**The information is not lost from the database. It is lost from the query
path.** Episode grouping reads only `sensor_readings`, via
`_fetch_minimal_flagged_rows` (`src/api/services/anomaly_group_queries.py:110-127`),
whose column projection selects no JSON column at all.

That single fact is why the interim below needs **zero migration**.

**The decisive caveat:** the JSON carries channel **names only** — no
per-channel `score`, `z_score`, `severity` or `value`. So it can rebuild
episode **spans** but not `peak_score`. That limit, and nothing about cost, is
why the interim is an interim.

## Decision

**Interim, now: option 2b — extend the existing `anomaly_columns` JSON.**
`RawReading.data` is already a schemaless blob, so carrying per-channel objects
(score / z / severity / value) instead of bare names needs **no migration at
all**, and the names half of it is **already present in production data** —
the only option of the five with a partial backfill already sitting in the
database. The write cost is already being paid: on fixture A, 89 of 3387 rows
get a read-modify-write of `data` today, inside the measured ~23 ms per
500-row raw upsert (the sensor upsert alongside it measures ~50 ms per 500
rows).

**Planned fix, next: option 1 — a secondary hits table.** One row per
`(timestamp, channel, detector)` hit, e.g. `anomaly_channel_hits` carrying
`tenant_id, greenhouse_id, timestamp, channel, detector, severity, value,
score, score_raw, z_score`, unique on `(tenant, gh, timestamp, channel,
detector)` and indexed on `(tenant, gh, timestamp)`.

Implementing 2b is a separate task and is not started here.

### Why 2b is the interim and not the destination

2b reaches roughly the same end state as option 1 with strictly worse query
properties, in exchange for a cheaper migration:

- The grouper needs one logical row per `(ts, channel)`, so the blob must be
  fetched and exploded in Python on **every** request. The episode query, which
  today touches only `sensor_readings`, must additionally read `raw_readings`
  and pull each flagged row's **entire** blob (all ~12 CSV columns) to use two
  keys.
- `RawReading.data` is SQLAlchemy `JSON`, which is Postgres `json` and **not
  `jsonb`**: no GIN index, no efficient containment query, no SQL-side
  filtering. The hits can never be *queried*, only materialised.
- Against that, option 1 stores the fact being lost — "channel C fired at time
  T with score S" — exactly once, in the shape it is consumed in, queryable and
  indexable, at a measured write amplification of ~3% of rows (~15 hit rows per
  500-row chunk on fixture A, 0 on fixture B) against that same existing 23 ms
  raw upsert. It is the only option that adds an insert to the hot path, and it
  is a small one.

Both options share the same downstream story, which is smaller than it looks:
`anomaly_episode_grouping.build_episodes` is **already** keyed on
`(channel, detector, direction)` and consumes row dicts, so feeding it one dict
per hit needs **no change**; only `_fetch_minimal_flagged_rows` and its row-dict
construction move. `src/models/episode_grouper.py` (detection-time) already
sees every hit and changes under no option here.

### Why not the other three

- **Option 2a — a new JSON column (`trigger_hits`) on `sensor_readings`.**
  Rejected relative to 2b: it costs a migration on the hot table (metadata-only
  and fast on Postgres, but a full table rebuild under SQLite's
  `batch_alter_table`) and buys nothing 2b does not already have, since it
  shares the explode-in-Python query path and has **no** backfill available.
- **Option 4 — persist detection-time statistical episodes, as stuck faults
  already do** (extend `anomaly_episodes` / `AnomalyEpisodeRecord`, migration
  026, which `_augment_stuck_fault_episodes` already reads back to repair
  stuck-fault spans). Genuinely attractive: the lowest write amplification of
  any option (~10 extra rows per fixture-A upload versus 106 for option 1) and
  the only one that makes the endpoint *faster*. Rejected as the fix because
  **detection-time episodes are per-upload**: an event spanning two uploads
  becomes two rows needing stitching, and today's row-derived grouping stitches
  across uploads correctly — that is *why* query-time grouping exists. Swapping
  a narrow-but-correctly-stitched view for a complete-but-seamed one is a
  different bug, not a fix. It also leaves per-*row* attribution unanswered,
  which is the part 2b partly answers. **Option 4 is kept as the stuck-fault
  mechanism it already is**; it complements option 1 and must not replace it.
- **Option 3 — one row per channel, by changing the unique constraint**
  (`(tenant_id, greenhouse_id, timestamp)` →
  `(tenant_id, greenhouse_id, timestamp, trigger_channel)`). **Rejected on
  correctness, not on cost.** Three independent reasons, any one sufficient:
  1. **It breaks the constraint it claims to extend.** `trigger_channel` is
     NULL on 3298 of fixture A's 3387 rows, and in Postgres a NULL is not equal
     to itself in a unique index — so the constraint would stop preventing
     duplicate *unflagged* readings entirely. The table's core invariant, one
     reading per timestamp, would be silently lost for 97% of rows.
  2. **It changes what a row means.** `temperature`, `humidity`, `co2` and
     `light` are properties of the timestamp, not of a channel. One row per
     triggering channel duplicates the sensor values, so every `SUM`/`AVG`/
     `COUNT` over readings starts double-counting, and every consumer that
     assumes one row per timestamp silently starts seeing duplicates —
     `anomaly_group_service`, the
     `row_number() over (partition by day order by anomaly_score desc)` date
     bucketing in `anomaly_group_queries.py`, the dashboard sample builders,
     the readings endpoints.
  3. Its migration is the worst available: Postgres needs a full unique-index
     rebuild on the largest table, and doing that safely means
     `CREATE INDEX CONCURRENTLY`, which must run outside a transaction —
     precisely the pattern `project_alembic_migration_hazards` warns against
     inside a migration. SQLite requires a whole-table copy.

  Cost (3) alone would not have decided it. (1) and (2) do.

## Two corrections to the original problem statement

Both are recorded because both were wrong in the framing this work inherited,
and a future reader will otherwise re-derive the wrong version.

**1. Episodes do not vanish.** A first pass matched detection-time to
query-time episodes on `(channel, start_ts)` and reported "2 episodes vanished,
3 query-only episodes appeared". That is an **artifact of the match key**: when
an episode's first row is lost to a collision its start moves, so one event
looks like one disappearance plus one arrival. Re-matched by span overlap:
**ABSENT 0**. The query view in fact holds **more** episodes than detection —
**15 vs 14** — because the collapse **splits** one (`measured_pipe_1_temp`,
12-21, one 9-sample episode at detection; n=1 at 13:00 plus n=6 from 13:15 at
query time). "The query view loses episodes" is backwards.

**2. The 0.806 / 0.693 figures are episode peaks, not per-row scores.** The
earlier BACKLOG-1 note read as though `measured_hd` beats
`measured_pipe_1_temp` row by row on canonical score. It does not: the winner
at each timestamp is chosen by `anomaly_rank_key` — severity, then detector
tier, then |z_score| — and canonical score enters only as a later tiebreak.
Quoting a pair of episode peaks as the reason a row was won conflates two
different quantities.

## What the ADR-011 pins must become

`tests/api/test_recovery_tag_fixture_a.py::TestTheTwoViewsOfThePipe1Event`
currently pins the narrow view as correct-for-its-input, and says so in its own
class docstring: *"The query-time view of this episode is narrower, and that is
not a bug... A future change that widens the query-time view will fail here and
should update these numbers deliberately."* Whichever of 2b or option 1 lands,
this is that change. Its three assertions today:

| test | asserts |
|---|---|
| `test_the_query_time_episode_covers_only_the_rows_pipe_1_won` | query `end_ts == 09:20`, `sample_count == 3` |
| `test_the_upload_time_episode_covers_the_whole_excursion` | upload `sample_count == 7` |
| `test_the_upload_time_view_of_the_same_event_is_untagged` | upload `is_recovery is False` |

Required changes, identical under 2b, 2a, option 1 and option 4, because all
four converge the two views:

- **The class premise dies.** It is no longer "two views, both correct about
  their own input"; it becomes a **view-equality** pin. It needs a rename (e.g.
  `TestTheTwoViewsOfThePipe1EventAgree`) and a **new docstring** — the current
  one would otherwise document a behaviour that no longer exists, which is
  worse than no docstring.
- **Test 1 inverts:** query `end_ts == 09:40` and `sample_count == 7`. Better
  still, assert it against the upload view rather than against a literal,
  matching the style
  `test_the_tagged_episode_reports_the_same_peak_as_the_untagged_view` already
  uses, so that neither side becomes the number of record.
- **The constants `PIPE_1_QUERY_END` and `PIPE_1_QUERY_SAMPLES` are deleted;**
  `PIPE_1_UPLOAD_SAMPLES = 7` becomes the shared value.
- **Test 2 is unchanged** (the upload view was always 7). **Test 3 is
  unchanged** under all four options: `is_recovery` stays query-time only, and
  none of these options moves tagging.
- **`TestTaggingChangesNothingElse.test_every_episode_both_views_share_reports_the_same_peak`
  must also change.** Its docstring states *"Sample counts are deliberately NOT
  compared: the row-collapse above makes them legitimately differ for some
  episodes."* That exclusion is exactly what these options remove, so the test
  **gains a `sample_count` comparison and loses that sentence.** This is the
  assertion that would actually prove the fix across the whole fixture rather
  than for pipe-1 alone.
- **Worth pinning once fixed, currently covered by nothing:** the 12-21 pipe-1
  **split** collapses back into one episode, so fixture A's query-side episode
  count drops 15 → 14 and matches detection.

Under option 3 the same inversion would have applied *plus* a harness rewrite —
`_episode_view_fixtures.py` and every test assuming one `sensor_readings` row
per timestamp. Another reason it is rejected.

## Consequences

- **2b fixes spans, not scores.** Episode `start_ts`, `end_ts`,
  `sample_count` and `duration_seconds` become correct; `peak_score` for a
  channel that never won a timestamp cannot be, because the per-channel score
  is not in the JSON. Any consumer reading `peak_score` on a widened episode
  must know this until option 1 lands. Extending the blob's entries from names
  to objects is what closes it, and that extension is the 2b task.
- **The episode query gains a second table read.** Today it touches only
  `sensor_readings`; under 2b it must also read `raw_readings` and materialise
  whole blobs for flagged rows. This is the cost being accepted for zero
  migration, and it is the reason the planned fix exists.
- **`sensor_readings.trigger_channel` needs an explicit demotion when option 1
  lands** — to "the primary hit, for single-channel displays" — or an explicit
  deprecation. Two places that know a reading's attribution, both undocumented,
  is how they drift.
- **Historical data stays partial either way.** Channel *names* can be
  backfilled from `raw_readings.data["anomaly_columns"]`, but per-channel
  scores for past uploads are gone and would stay NULL or require re-upload.
  The graceful-degradation pattern for this already exists
  (`views_without_episode_records`).
- **ADR-011's tagging assertions must be re-run** when either option lands.
  Fixture A predicts no verdict flips; that prediction is derived, not
  measured, and is not a substitute for running them.

## Known limitations of this record

- The collision frequency that motivates the work (19.1% of flagged timestamps,
  60% of statistical episodes damaged) comes from **a single engineered fixture
  with zero LSTM hits**. Production frequency is unknown. If it turns out to be
  near zero, the ordering here — cheap interim first, migration later — is
  still the right shape, but the urgency changes.
- Fixture B contributes nothing. It is cited above only to prevent someone
  later reading "0 collisions on the July fixture" as evidence of rarity.
