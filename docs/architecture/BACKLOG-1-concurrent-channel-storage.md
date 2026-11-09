# BACKLOG-1 — Concurrent-channel storage

> **Historical — point-in-time record.** Status: superseded by [ADR-012-concurrent-channel-hits.md](ADR-012-concurrent-channel-hits.md), which took the decision this proposal was waiting on. The "awaiting a decision" status line below and the "deliberately untracked" paragraph are both out of date: the decision has been made, and `docs/architecture/` has tracked everything except `drafts/` since PR #101 (2026-11-08). Kept as the measurements-and-options record behind the ADR. Not maintained against current code.

Status: **design proposal, awaiting a decision.** No code, tests or migrations
were written for this. Measurements first, options second, recommendation last.

This file is deliberately untracked (`docs/architecture/*` tracks only
`ADR-*.md`, `upload_detector_convergence.md`, `episode_grouping.md`). It becomes
an ADR only once an option is chosen.

---

## 1. The problem, restated precisely

`sensor_readings` is unique on `(tenant_id, greenhouse_id, timestamp)` and
carries four scalar attribution columns — `trigger_channel`,
`trigger_detector`, `detected_severity`, `trigger_value`
(`src/db/models.py:106`+). One timestamp, one channel.

The collapse happens in `build_trigger_data_by_timestamp()`
(`src/api/services/cold_run_upload.py:127-165`), which keeps the single
highest-ranked hit per timestamp under `anomaly_rank_key`
(`src/api/services/anomaly_ranking.py:70` — severity > detector tier > |z| >
score > timestamp > column). Every other channel that fired at that timestamp
is dropped.

Why it matters beyond cosmetics: Task 5's `is_recovery` tagging deliberately
never suppresses an episode, because a real fault can coincide with a recovery.
But if the coinciding fault scores below another channel at the same
timestamps, the persisted view hides it *before* tagging ever runs. The storage
model masks exactly the case the tagging policy exists to preserve.

---

## 2. Measurements

All figures below are from the production upload path —
`run_cold_run_upload(..., persist_db=session, chunk_size=500)` — plus the real
`GET /api/anomalies/episodes` endpoint, using the same harness the Task 5 pins
use (`tests/api/_episode_view_fixtures.py`). No `StuckCarry` is hand-built
anywhere, so the imputed-channel phantom-freeze trap does not apply.

Scripts (scratchpad, not in the repo):
`bl1_measure.py`, `bl1_episodes.py`, `bl1_raw.py`, `bl1_rel.py`, each run as
`cd /Users/eugenesidorov/Projects/ai-agronomist && ./venv/bin/python <script>`.

### 2.1 Harness validity

Checked before trusting any number:

- **`high_anomalies` really is the pre-collapse set.** `_merge_by_timestamp`
  (`cold_run_upload.py:385`) dedupes only the *LSTM* side per timestamp; it
  iterates `stat_hits` and appends each one, so N statistical columns at one
  timestamp produce N entries. Stuck alerts are appended after
  (`upload_portions.py:741-745`).
- **`high_anomalies` is not capped.** The "display cap" named in
  `anomaly_ranking.py:5-6` lives in `anomalies_page_data.py`; the upload
  response schema (`src/api/schemas/sensor_data.py:437`) has no limit.
- **The carry path engages.** Fixture A persists 3387 rows at `chunk_size=500`
  = **7 chunks**. (`UploadInferenceResponse` has no `total_portions` field;
  a `getattr(response, "total_portions", None)` probe returns `None` because
  the attribute is absent, not because the carry was idle. Portion count was
  derived from persisted rows instead.)
- **Detector labels matched by containment**, never equality — `"lstm+statistical"`
  exists. Measured by both methods; they agree here only because this harness
  produces no LSTM hits (below).
- **Collapse output == what is in the DB.** For every flagged timestamp, the
  persisted `trigger_channel` equals `build_trigger_data_by_timestamp`'s
  winner: **0 mismatches / 89 timestamps**. The function under discussion is
  genuinely what the database holds.

### 2.2 Fixture A — `tests/fixtures/native_dec_stuck_fault.csv`

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
| persisted episode records (`anomaly_episodes`, stuck only) | 4 |

Winners on collided timestamps: `measured_hd` 6, `measured_pipe_1_temp` 6,
`measured_pipe_2_temp` 5.
Losers: `measured_pipe_1_temp` 9, `measured_pipe_2_temp` 6,
`measured_plant_temp` 2.

So roughly **one flagged timestamp in five loses a channel**, and the loss is
concentrated in the pipe-temperature channels.

### 2.3 Fixture B — `tests/fixtures/native_july_clearsky.csv`

**Zero.** 2016 rows persisted (none dropped), 5 chunks, **0 hits**, 0 flagged
timestamps, 0 collisions, 0 episodes, 0 episode records.

Stated plainly: the clean July fixture produces no anomalies at all, so it
contributes no evidence for or against any option. It is not a "near-zero
collision rate" result — there is nothing to collide. Any claim about typical
collision frequency rests on fixture A alone, which is a single engineered
stuck-fault file and not a sample of production traffic.

### 2.4 Episode-level impact (fixture A) — the number that actually matters

A first pass matched detection-time to query-time episodes on
`(channel, start_ts)` and reported "2 episodes vanished, 3 query-only episodes
appeared". **That reading is an artifact of the match key** and is wrong: when
an episode's first row is lost to a collision, its start moves, so the same
event looks like one disappearance plus one new episode. Re-matched by span
overlap on the same channel:

- 10 statistical detection-time episodes, **102 rows**
- query-time coverage of them: **85 rows** → **17 rows lost**, which exactly
  equals the 17 suppressed channel-rows (independent cross-check)
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
   episode is on a winner channel (`measured_hd` ×3, `calculated_heating_temp`);
   every damaged one is on a loser channel. A channel that habitually loses
   rank is habitually mis-measured.
2. **The collapse can SPLIT one event into two.** `measured_pipe_1_temp` on
   12-21 is one 9-sample episode at detection and two query episodes (n=1 at
   13:00, then n=6 from 13:15). This is why the query view reports **more**
   episodes (15) than detection (14) — "the query view loses episodes" is
   backwards. A split also manufactures a gap that is not in the data, which
   is the same failure mode `anomaly_episode_service.py`'s "ONE CONSTRAINT
   THAT MUST NOT BE VIOLATED" docstring forbids for top-N subsetting; the
   collapse reaches it by another route.
3. **Truncation hits the start as often as the end.** `measured_pipe_2_temp`
   on 12-24 appears to begin 10 minutes late; `measured_plant_temp` on 12-18
   is reduced from 3 samples to a **single row**, whose `duration_seconds` is
   then 0.0. Start shifts matter for Task 5 specifically, because
   `is_recovery` is start-anchored.

### 2.5 Does widening flip any `is_recovery` verdict? On fixture A, no.

Measured stuck releases (all on `humidity`, from the persisted
`anomaly_episodes` rows): 12-15 10:55, **12-16 09:15**, 12-18 11:20,
12-25 08:55. Constants: `EPISODE_GAP_SECONDS=600`,
`RECOVERY_KNOCKON_WINDOW_HOURS=4.0` (`src/config.py:1572,1582`).

Deriving from those two measured inputs (this is arithmetic on measurements,
not a separate measurement): each damaged episode's detection-time start falls
in the same tag/no-tag bucket as its truncated start —
`measured_pipe_1_temp` 12-16 keeps start 09:10 (truncation moved only its end)
and stays tagged; `measured_plant_temp` 12-18 moves 12:55→12:45, both within
4h of the 11:20 release, so it stays tagged; the 12-21 and 12-24 episodes are
days from any release and stay untagged either way.

So on this fixture widening is a **span-only correction with no tagging churn**.
The general risk remains real — tagging is start-anchored and the collapse
moves starts — so whichever option is chosen, re-running the Task 5 tagging
assertions is required, not optional.

### 2.6 The finding that changes the options: the data is already persisted

`build_anomaly_metadata_by_timestamp()` (`cold_run_upload.py:109-125`)
collects **every** channel that fired at each timestamp, and `csv_persist`
merges it into `raw_readings.data` as `anomaly_columns` / `anomaly_detectors`.

Measured: of the 17 collided timestamps, **17/17 already carry the complete
channel set** in `raw_readings.data["anomaly_columns"]`. Example, 2026-12-16
09:25 — `anomaly_columns: ["measured_hd", "measured_pipe_1_temp"]` while
`sensor_readings.trigger_channel` is `measured_hd` alone.

**The information is not lost from the database. It is lost from the query
path**, because episode grouping reads only `sensor_readings`
(`_fetch_minimal_flagged_rows`, `anomaly_group_queries.py:110-127`). What the
JSON does *not* carry is the per-channel `score`, `z_score`, `severity` and
`value` — only the names. So it can reconstruct episode **spans** but not
per-episode `peak_score`.

### 2.7 Scope limit of these measurements

The Task 5 harness's `mock_detector()` returns a constant 0.05, so **zero LSTM
hits** arise. This does not bias the channel-collision counts: LSTM hits carry
`column=None`, so they never compete for a channel, and `lstm` is the lowest
tier in `anomaly_rank_key`, so a real LSTM run cannot change which channel
wins. It would only relabel `trigger_detector` to `"lstm+statistical"`.
Collision frequency on real production traffic is **not** measured by this work.

---

## 3. Options

Current write-path cost, for comparison (measured, same runs):
`persist.sensor_upsert` ≈ 50ms/500 rows, `persist.raw_upsert` ≈ 23ms/500 rows.
Fixture A writes 3387 reading rows and 106 hits — a hit is ~3% of row volume.

### Option 1 — secondary hits table

One row per `(timestamp, channel, detector)` hit, e.g. `anomaly_channel_hits`:
`tenant_id, greenhouse_id, timestamp, channel, detector, severity, value,
score, score_raw, z_score`, unique on `(tenant, gh, timestamp, channel,
detector)`, indexed on `(tenant, gh, timestamp)`.

- **Migration (028):** additive — a new table. No DDL on `sensor_readings`, so
  no lock on the largest table and no rebuild on SQLite. The safest of the
  three. Historical backfill is partial at best: channel *names* can come from
  `raw_readings.data["anomaly_columns"]` (§2.6) but per-channel scores are
  gone and would have to stay NULL or be recomputed by re-upload.
- **Write path:** one extra `executemany` per chunk. Measured volume: ~15 hit
  rows per 500-row chunk on fixture A, 0 on fixture B — well under the
  existing 23ms raw-upsert. Write amplification ≈ **3% of rows**, concentrated
  entirely on flagged timestamps. This is the only option that adds an insert
  to the hot path, and it is a small one.
- **Query path:** `_fetch_minimal_flagged_rows` joins `sensor_readings` (for
  temperature/humidity/co2/light) to the hits table, yielding one logical row
  per hit instead of per reading. Indexed and filterable in SQL.
- **Groupers:** `anomaly_episode_grouping.build_episodes` is *already* keyed on
  `(channel, detector, direction)` and consumes row dicts — feed it one dict
  per hit and **it needs no change**. `_row_channel_and_detector`'s pre-024
  fallback chain (`anomaly_episode_rows.py:40-58`) stays for old rows.
  `src/models/episode_grouper.py` (detection-time) already sees every hit and
  needs no change under any option here.
- **Task 5:** `is_recovery` operates on grouped episodes and keeps working
  unchanged; it simply sees complete episodes. Re-verify per §2.5.
- **Cost:** largest code change of the three. Introduces a second place that
  knows a reading's attribution, so `sensor_readings.trigger_channel` must be
  explicitly demoted to "the primary hit, for single-channel displays" or
  deprecated — leaving both undocumented is how they drift.

### Option 2 — JSON column of all hits per timestamp

Two sub-variants, and the difference between them is large:

**2a — new JSON column on `sensor_readings`** (e.g. `trigger_hits`).
**2b — extend the existing `raw_readings.data["anomaly_columns"]`** from a list
of names to a list of objects carrying score/z/severity/value.

- **Migration:** 2a adds a nullable JSON column to the hot table (metadata-only
  and fast on Postgres; a full table rebuild under SQLite's
  `batch_alter_table`). **2b needs no migration at all** — `data` is already a
  schemaless blob, and it is the only option with a partial backfill already
  sitting in production data.
- **Write path:** 2a is one more bindparam on an UPDATE batch that already
  carries four trigger fields — effectively free. 2b is **already being paid**:
  89 of 3387 rows get a read-modify-write of `data` today, inside the measured
  23ms/500-row raw upsert.
- **Query path:** this is where it loses. The grouper needs one logical row per
  `(ts, channel)`, so the blob must be fetched and exploded in Python on every
  request. For 2b the episode query — which today touches only
  `sensor_readings` — must additionally read `raw_readings` and pull each
  flagged row's **entire** blob (all ~12 CSV columns) to use two keys.
  `RawReading.data` is SQLAlchemy `JSON`, which is Postgres `json`, **not
  `jsonb`**: no GIN index, no efficient containment query, no SQL-side
  filtering. The hits can never be queried, only materialised.
- **Groupers:** identical to Option 1 once exploded — only the fetch/explode
  layer differs.
- **Verdict:** reaches the same end state as Option 1 with strictly worse query
  properties, in exchange for a cheaper migration. 2b's real value is as an
  **immediate partial mitigation** (the channel names are already there), not
  as the destination.

### Option 3 — per-channel row (change the unique constraint)

`UniqueConstraint(tenant_id, greenhouse_id, timestamp)` →
`(tenant_id, greenhouse_id, timestamp, trigger_channel)`.

**Recommend rejecting.** Not merely expensive — incorrect.

- **It breaks the constraint it claims to extend.** `trigger_channel` is NULL
  on 3298 of 3387 fixture-A rows. In Postgres a NULL is not equal to itself in
  a unique index, so the constraint would stop preventing duplicate *unflagged*
  readings entirely. The table's core invariant — one reading per timestamp —
  would be silently lost for 97% of rows.
- **It changes what a row means.** `temperature`, `humidity`, `co2` and `light`
  are properties of the timestamp, not of a channel. One row per triggering
  channel duplicates the sensor values, so any `SUM`/`AVG`/`COUNT` over
  readings starts double-counting.
- **Migration cost is the worst available.** Postgres needs a full unique-index
  rebuild on the largest table; doing it safely means `CREATE INDEX
  CONCURRENTLY`, which must run outside a transaction — precisely the pattern
  `project_alembic_migration_hazards` warns against inside a migration. SQLite
  requires a whole-table copy.
- **Blast radius.** Every consumer that assumes one row per timestamp would
  silently start seeing duplicates: `anomaly_group_service`, the
  `row_number() over (partition by day order by anomaly_score desc)` date
  bucketing (`anomaly_group_queries.py:133-170`), the dashboard sample
  builders, and the readings endpoints.

### Option 4 (added) — persist detection-time episodes, as stuck faults already do

The repo has already solved this exact problem once. `anomaly_episodes`
(migration 026, `AnomalyEpisodeRecord`) stores one row per detection-time fault
window, and `_augment_stuck_fault_episodes` reads it back to repair stuck-fault
spans that the row-derived view gets wrong — because a freeze emits only one
flagged row, so its row-derived `duration_seconds` would always be 0.0. That is
structurally the same failure as this one: a detection-time truth the
query-time view cannot reconstruct from rows. Extend that table to carry
statistical episodes too.

- **Migration (028):** a couple of nullable columns at most, on a table holding
  **4 rows per fixture-A upload**. Cheap and safe — it never touches
  `sensor_readings`.
- **Write path:** ~10 extra rows per fixture-A upload versus 106 for Option 1 —
  the **lowest** write amplification of any option, and already on the path
  next to `persist_stuck_fault_episodes`.
- **Query path:** the only option that makes the endpoint **faster** — a
  bounded read of a small indexed table replaces a 3387-row scan plus Python
  grouping.
- **Honest downsides:**
  - It does not restore per-*row* attribution. "Which channels fired at this
    exact timestamp", outside an episode, stays unanswered (2b partly answers
    it).
  - **Detection-time episodes are per-upload.** An event spanning two uploads
    becomes two rows needing stitching — the same cross-upload reach problem
    the Task 5 docstring already records for `stuck_carry`. Today's row-derived
    grouping stitches across uploads correctly; that is *why* query-time
    grouping exists ("adds the same episode concept for historical data"). So
    this option trades a narrow-but-stitched view for a complete-but-seamed
    one, which is not obviously a net win.
  - Historical uploads have no statistical episode rows and would stay narrow
    until re-uploaded. The graceful-degradation pattern already exists
    (`views_without_episode_records`).

---

## 4. Comparison

| | 1 · hits table | 2a · JSON col | 2b · extend raw JSON | 3 · per-channel row | 4 · persist episodes |
|---|---|---|---|---|---|
| Migration cost | low (additive) | low / SQLite rebuild | **none** | **very high + unsafe** | low |
| Write amplification | ~3% rows | ~0 | **already paid** | highest | **lowest** |
| Query path | indexed SQL | explode in Python | explode + 2nd table, unindexable | duplicates everywhere | **fastest** |
| Per-row attribution | **yes** | yes | names only | yes | no |
| Cross-upload stitching | **preserved** | preserved | preserved | preserved | **lost** |
| Grouper changes | fetch layer only | fetch layer only | fetch layer only | pervasive | moderate |
| Backfill possible | names only | no | **already present** | n/a | no |

---

## 5. Recommendation

**Option 1, the secondary hits table.** Reasoning:

1. It stores the fact that is being lost — "channel C fired at time T with
   score S" — exactly once, in the shape it is consumed in, queryable and
   indexable. Options 2a/2b store the same fact in a shape that must be
   re-exploded in Python on every request and can never be filtered in SQL.
2. The measured write cost is small and bounded: ~3% row amplification, ~15
   rows per 500-row chunk, against an existing 23ms/500-row raw upsert on the
   same path. Write amplification is a real concern here and this option pays
   the least for the most.
3. It preserves cross-upload stitching, which Option 4 gives up. Option 4 is
   cheaper and faster and is the right shape for a *detector's* record, but
   swapping a narrow-but-correctly-stitched episode view for a
   complete-but-seamed one is a different bug, not a fix.
4. The downstream change is smaller than it looks: `build_episodes` is already
   keyed on `(channel, detector, direction)` and needs **no** change; only
   `_fetch_minimal_flagged_rows` and its row-dict construction move.

Suggested sequencing, if the user wants the risk split:

- **Now:** 2b as mitigation — the channel names are already persisted, so any
  consumer that needs "was another channel firing here?" can be answered today
  without a migration. This does not fix episode spans.
- **Then:** Option 1 for the durable fix.
- **Keep:** Option 4 as the stuck-fault mechanism it already is. It complements
  Option 1 (a detector's own record of a fault window) and should not replace it.

Explicitly not recommended: **Option 3**, on correctness grounds (§3), not cost.

---

## 6. What `TestTheTwoViewsOfThePipe1Event` must become

`tests/api/test_recovery_tag_fixture_a.py:114`. It currently pins the narrow
view as correct-for-its-input, and its class docstring says so: *"The
query-time view of this episode is narrower, and that is not a bug... A future
change that widens the query-time view will fail here and should update these
numbers deliberately."* This is that change.

Its three assertions today:

| test | asserts |
|---|---|
| `test_the_query_time_episode_covers_only_the_rows_pipe_1_won` | query `end_ts == 09:20`, `sample_count == 3` |
| `test_the_upload_time_episode_covers_the_whole_excursion` | upload `sample_count == 7` |
| `test_the_upload_time_view_of_the_same_event_is_untagged` | upload `is_recovery is False` |

**Under Options 1, 2a, 2b and 4 — all of which widen the query view — the same
thing happens**, because all four converge the two views:

- The class's premise dies. It is no longer "two views, both correct about
  their own input"; it becomes a **view-equality** pin. Rename accordingly
  (e.g. `TestTheTwoViewsOfThePipe1EventAgree`) and rewrite the docstring — the
  current one would otherwise document a behaviour that no longer exists.
- Test 1 inverts: query `end_ts == 09:40` and `sample_count == 7`. Better
  still, assert it against the upload view rather than a literal, matching the
  style `test_the_tagged_episode_reports_the_same_peak_as_the_untagged_view`
  already uses, so neither side is the number of record.
- The constants `PIPE_1_QUERY_END` and `PIPE_1_QUERY_SAMPLES`
  (`:43-44`) are deleted; `PIPE_1_UPLOAD_SAMPLES = 7` becomes the shared value.
- Test 2 is unchanged (the upload view was always 7).
- Test 3 is unchanged under all four options: `is_recovery` stays query-time
  only, and none of these options moves tagging.
- **`TestTaggingChangesNothingElse.test_every_episode_both_views_share_reports_the_same_peak`
  (`:177`) must also change.** Its docstring states *"Sample counts are
  deliberately NOT compared: the row-collapse above makes them legitimately
  differ for some episodes."* That exclusion is exactly what these options
  remove, so the test should gain a `sample_count` comparison and lose that
  sentence. This is the assertion that would actually prove the fix across the
  whole fixture, not just for pipe-1.
- Additionally worth pinning once fixed, from §2.4: the 12-21 `pipe_1` **split**
  collapses back to one episode, so the query-side episode count for fixture A
  drops 15 → 14 and matches detection. No current test covers the split.

**Under Option 3** the same inversion applies, plus the harness itself changes:
`_episode_view_fixtures.py` and every test that assumes one `sensor_readings`
row per timestamp would need review.

**If the user chooses to do nothing,** no test changes — the class is already
an accurate, deliberately-labelled pin of current behaviour, and ADR-011's
known-limitations link stands as written.
