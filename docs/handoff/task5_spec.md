# Task 5 — `is_recovery` repurposed as cross-channel tagging

Branch `task-5-is-recovery-tagging`. This document is a self-contained spec
for what changed and why — readable without the brief that produced it.

## 1. What changed

`is_recovery` used to mean "this hit is on the SAME channel as a stuck fault
that just released, so cap its score." It now means "this episode is on a
DIFFERENT channel from a stuck fault that just released, so TAG it as a
likely knock-on effect." The field never suppresses or rescores anymore —
it is purely informational.

Exact contract sentence, shared by all three live definitions below --
verbatim in two (`AnomalyEpisode.is_recovery` and
`AnomalyEpisodeSummary.is_recovery`); the third
(`AnomalyEpisodeSchema.is_recovery`, the upload-path field, always `False`)
appends one more sentence on top of it rather than repeating it standalone
-- verified directly against the generated `app.openapi()` output for both
schema fields:

> True when this episode is on a channel other than a stuck-fault channel
> and starts no earlier than `EPISODE_GAP_SECONDS` before, and within
> `RECOVERY_KNOCKON_WINDOW_HOURS` after, that fault's release. Informational
> only; does not suppress or rescore.

`AnomalyEpisodeSchema.is_recovery`'s full description is that sentence plus:
"Always False in this upload-path response -- the live computation is
query-time only; see AnomalyEpisodeSummary.is_recovery."

(The brief that specified this task used the name `RECOVERY_TAG_WINDOW_HOURS`
for the forward-looking constant. That constant does not exist and was not
added — see §3 for why `RECOVERY_KNOCKON_WINDOW_HOURS` is used instead. An
earlier revision of this task also added a dedicated
`RECOVERY_TAG_PRE_RELEASE_GRACE_MINUTES` constant for the backward-looking
side; that was deleted in favor of reusing `EPISODE_GAP_SECONDS` — see §3.
The constant names above are the final ones, substituted in every place the
sentence appears.)

## 2. Landing: query-time only, not detection-time

The tagging is computed exclusively in
`src/api/services/anomaly_episode_service.py` (`_apply_cross_channel_recovery_tags`
and its two helpers), which backs `GET /api/anomalies/episodes`. It is
**not** computed in `src/models/episode_grouper.py` (the upload-path
grouper), even though that module keeps the `is_recovery` field on its
`AnomalyEpisode` dataclass.

Why: cross-channel tagging needs to know when some OTHER channel's stuck
fault released — `release_ts` on a persisted `anomaly_episodes` row — and a
fault on one channel can release in a different upload from the one that
produced the hit being tagged. `stuck_carry.recovery_channels`
(`src/models/stuck_fault_detector.py`) is in-memory, per-upload state; it
cannot see a release that happened in an earlier upload. The `anomaly_episodes`
table (migration 026) is the only cross-upload source for `release_ts`, so
the tagging can only be computed where that table is already read —
query time.

Consequence: `AnomalyEpisode.is_recovery` (`src/models/episode_grouper.py`)
and `AnomalyEpisodeSchema.is_recovery` (`src/api/schemas/sensor_data.py`,
the upload response) are **always `False`**. The old same-channel
computation that used to set them was deleted outright (§4), not replaced
with an equivalent upload-time cross-channel version — there is no
sound way to do that at upload time for the reason above. Both fields are
kept only so the upload response keeps a stable shape; their docstrings
point to the real, live computation.

The live field is `AnomalyEpisodeSummary.is_recovery`
(`src/api/schemas/anomaly_episodes.py`), populated by the query path.

### A consequence of landing at query time: the query-time grouper sees less than the upload-time grouper

`sensor_readings` persists exactly one `trigger_channel` per timestamp —
whichever channel scored highest at that row — even on a row where more
than one channel's statistical score exceeded threshold simultaneously. The
upload-path response (`response.high_anomalies`, the input to
`src/models/episode_grouper.py`) is NOT subject to this: it carries every
channel's hit at every row. The two groupers can therefore see a genuinely
different hit set for the same underlying event on the same uploaded data —
not a function of chunk size (verified identical at `chunk_size=500`,
`chunk_size=5000` — production's actual default, see §3 — and
`chunk_size=10**7`), but of which grouper's input the row-collapse already
happened before. This is pre-existing, orthogonal to Task 5, and not
addressed here; §3 below is the concrete case it caused.

## 3. Constants: `RECOVERY_KNOCKON_WINDOW_HOURS` (forward) and `EPISODE_GAP_SECONDS` (backward), both reused

`RECOVERY_KNOCKON_WINDOW_HOURS` (`src/config.py`, default `4.0`, env-overridable)
already existed with exactly this semantic in its doc comment — "excursions
on downstream control channels … attributed to the recovery" — and had zero
production references. Reused rather than adding
`RECOVERY_TAG_WINDOW_HOURS` so the env-var override key does not change.
Its comment was rewritten to describe tagging instead of a stale same-channel
score cap.

`RECOVERY_SCORE_CAP` was deleted from `src/config.py` entirely (§4) — tagging
must not change `peak_score` or ranking.

Tagging is start-anchored (an episode's `start_ts`, not each row inside it),
which can miss an episode already underway when the fault released. The
first implementation of this task added a dedicated constant,
`RECOVERY_TAG_PRE_RELEASE_GRACE_MINUTES` (default 15 minutes), for exactly
that gap. That constant was removed on review in favor of reusing
`EPISODE_GAP_SECONDS` (`src/config.py`, default `600`, already imported by
`src/models/episode_grouper.py`). The reasoning, recorded here because it is
not obvious from the code alone: `release_ts` is defined as "the first
non-frozen sample after the freeze ends" (`AnomalyEpisodeRecord` docstring,
`src/db/models.py`) — it can therefore lag the true end of the fault by up
to one sample/gap. An episode starting fractionally before `release_ts` is
within that measurement resolution, not actually a response that predates
the fault clearing. `EPISODE_GAP_SECONDS` is the pipeline's own existing
definition of "how large a gap still counts as contiguous," so reusing it
for this bound is not a tuned fudge factor — it is the same resolution the
pipeline already applies everywhere else, applied here too. The final
predicate:

```
release_ts - EPISODE_GAP_SECONDS  <=  episode.start_ts  <=  release_ts + RECOVERY_KNOCKON_WINDOW_HOURS
```

**`EPISODE_GAP_SECONDS`, deliberately not `ANOMALY_EPISODE_GAP_SECONDS`.**
The same module (`anomaly_episode_service.py`) also has
`ANOMALY_EPISODE_GAP_SECONDS` as the query-time grouper's OWN contiguity
gap (`fetch_anomaly_episodes`'s `gap_seconds` default) -- the knob that
produced this episode's `start_ts` in the first place. `src/config.py` is explicit
that the two gap constants are intentionally separate and must not be
unified. This task's grace nonetheless uses the OTHER one,
`EPISODE_GAP_SECONDS`, on purpose: the grace compensates for lag in
`release_ts`, which the stuck-fault detector writes at DETECTION time, so
its resolution is the detection-time cadence, not the query-side
regrouping knob that happened to produce this episode. Both constants
default to `600` today, so nothing misbehaves either way right now; the
distinction only bites if someone sets `ANOMALY_EPISODE_GAP_SECONDS` to a
different value, at which point using it here instead would silently
regroup episodes at one gap while granting recovery grace at another. See
the comment at `_apply_cross_channel_recovery_tags`'s `gap_seconds`
default in `anomaly_episode_service.py` for the same note in-code.

**Overlap-based tagging was considered and rejected.** An alternative design
would tag an episode whenever its time SPAN overlaps the fault's span,
rather than anchoring on the episode's start. This was rejected because it
would tag a long pre-existing excursion on another channel — one that began
well before the fault and simply happens to still be running when the fault
releases — as a recovery response, when it is not one. Start-anchoring
(with the small backward resolution above) avoids this: a long-running
episode that merely overlaps a release, but started far earlier, has a
`start_ts` far outside the bound and is correctly left untagged.

### Correction: the brief's pipe_1 case was right; I read the wrong view as "the hits," then misdiagnosed why

An earlier version of this document stated the brief's acceptance case —
"`measured_pipe_1_temp` hits at 12-16 09:25–09:40, minutes after a humidity
release" — did not match the pipeline. That conclusion was wrong, caught on
review, and is corrected here rather than left in place. Recorded plainly
because it is a process lesson, not just a numbers correction:

**When the brief's premise appeared not to match, it was reported as "the
hits do not exist" rather than as "my rule does not tag the hits that
exist."** Those are not the same claim, and the difference matters: the
pipe_1 statistical hits were real and present in the data the whole time.
What was actually wrong was that a start-anchored tagging rule, evaluated
against a query-time episode that starts five minutes before its triggering
release, produces `False` for an event that a reader of the raw hits would
call a recovery. Reporting "the premise is false" when the honest finding
was "my implementation does not cover this real case" inverted the
direction of the discrepancy — it should have been read as a gap in the
rule, not a gap in the brief. The general lesson: **verify a briefed premise
at the level the brief states it** (here: individual flagged hits on a
channel) **before declaring it false at the level your own code happens to
operate on** (here: query-time episode start timestamps, already narrowed
by an unrelated row-collapse — see below). Code reducing a real event to a
narrower view of it is not the same as the event not existing, and the
difference between those two should have been checked before writing the
conclusion, not after being corrected on it.

What actually happens, verified via the established harness
(`tests.api.test_fixture_a_stuck_contract.fixture_a_response`,
`REFERENCE_CHUNK_SIZE = 10**7`) and independently re-confirmed at
`chunk_size=500`, `chunk_size=5000` (production's real default, `CHUNK_SIZE`
in `src/api/routes/upload.py`), and `chunk_size=10**7`:
`measured_pipe_1_temp` has SEVEN statistical hits on 2026-12-16, at 09:10,
09:15, 09:20, 09:25, 09:30, 09:35, 09:40 — **identical at every chunk size
checked**, including 500. `measured_hd` separately hits 09:25 through
10:15. Both channels are genuinely anomalous across 09:25–09:40 at the same
time. At 5-minute spacing with `EPISODE_GAP_SECONDS=600`, the upload-path
grouper (`group_alerts_into_episodes`, called directly on
`response.high_anomalies`) merges all seven pipe_1 hits into ONE episode,
09:10 → 09:40, `n=7`. The brief's premise was correct at this level, at
every chunk size — pinned by `tests/api/test_recovery_tag_fixture_a.py`'s
`PIPE_1_UPLOAD_SAMPLES = 7`, using the `chunk_size=500` harness in
`tests/api/_episode_view_fixtures.py` (`CHUNK_ROWS = 500`).

**The actual cause of my first measurement was simpler, and chunk size was
never it.** An earlier revision of this section claimed `chunk_size=500`
changed which rows crossed threshold for pipe_1. That claim was itself
wrong — re-measured directly (above) and contradicted by the committed,
green acceptance test cited there, which pins `n=7` at `chunk_size=500`.
The chunk-size explanation has been removed rather than left to stand
alongside evidence against it.

What I had actually done was read the QUERY-TIME grouper's output
(`anomaly_episode_grouping.build_episodes`, fed from
`sensor_readings.trigger_channel` — see §2's row-collapse note) and report
it as "the hits," when the brief's claim was about hits at the level the
brief stated them — the raw per-channel hit stream, i.e. the upload-path
view. The two views differ because `sensor_readings` persists exactly one
`trigger_channel` per timestamp: pipe_1 WINS that slot only at 09:10,
09:15, 09:20; `measured_hd` wins it from 09:25 onward because its score is
higher there.

Measured directly against the real pipeline (real upload with
`persist_db` set, then the real `GET /api/anomalies/episodes`-backing
`fetch_anomaly_episodes` against that same database -- not a bare harness,
not inferred from a test constant) at `chunk_size` 500, 5000, and `10**7`:
upload-path pipe_1 hit count is `n=7` at every one of the three; query-time
pipe_1 episode is `start=2026-12-16T09:10, end=09:20, n=3` at every one of
the three. Both counts are identical across all three chunk sizes checked
-- the row-collapse is independent of chunk size, so it was never a
harness problem. The query-time pipe_1 episode genuinely is `start=09:10,
end=09:20, n=3`, five minutes BEFORE the 09:15 release; that is a real,
narrower, correct view of the same event, not a wrong measurement of it.
Reporting that narrower view as "the hits" — and from there, as "the
brief's premise is false" — is the error, and it is exactly why
start-anchored tagging (the first implementation) also missed this case:
the query-time episode legitimately starts before the release that caused
it, because the row-collapse already discarded its later, after-release
rows in favor of `measured_hd`. The backward resolution bound (§ above) is
the fix for the tagging gap; re-reading the brief's claim at the level it
was stated is what should have caught the write-up error before it needed
a second correction.

### Backward bound: confirmed at `EPISODE_GAP_SECONDS` (600 s)

The query-time pipe_1 episode starts 300 s (5 minutes) before its
triggering release (09:10 vs. 09:15) — comfortably inside the 600 s bound.

Checked, on fixture A, every `(episode, release)` pair on different
channels by gap in minutes (`episode.start_ts - release_ts`): the nearest
excluded case on the negative (before-release) side is
`measured_pipe_1_temp` starting 2026-12-24T13:40, 1155 minutes (~19 h)
before the 2026-12-25 08:55 release — itself an unrelated standalone
excursion days later, already correctly `is_recovery=False`, and nowhere
near the 10-minute (600 s) boundary. No episode anywhere in the fixture
falls between -10 and -5 minutes of any release, so dropping the backward
bound from the first pass's 15 minutes (900 s) to `EPISODE_GAP_SECONDS`'s
600 s changes no tagging outcome on this fixture — re-confirmed by direct
re-run, not by arithmetic alone: the full tagged/untagged list at 600 s is
identical, entry for entry, to the 900 s list. Fixture B has zero episodes,
so it contributes no boundary evidence either way.

### Window re-confirmation (forward side, unchanged across every revision of this task)

Fixture A has 4 humidity stuck-fault releases. The nearest other-channel hit
after each one, at the query-time grouping level:

| Release (`humidity`) | Nearest other-channel hit | Gap | Tagged? |
|---|---|---|---|
| 2026-12-15 10:55 | `measured_pipe_1_temp` @ 2026-12-16T09:10 | 22.25 h | No (no closer hit exists for this release) |
| 2026-12-16 09:15 | `measured_pipe_1_temp` @ 2026-12-16T09:10 | −300 s (5 min early) | **Yes**, within the backward bound |
| 2026-12-18 11:20 | `measured_hd` @ 2026-12-18T12:40 | 1.33 h | **Yes** |
| 2026-12-25 08:55 | `measured_hd` @ 2026-12-25T09:10 | 0.25 h | **Yes** |

The nearest hit the window does NOT tag is 22.25 h away (release 1) / ~19 h
away (release 2, the pipe_1-on-12-24 case above) / 72.9 h away (release 3)
— release 4 has no further hits in the fixture. 4.0 h stands as measured,
unchanged across every revision of this task.

Fixture B (`native_july_clearsky.csv`) is a zero-anomaly negative control —
it produces zero stuck faults and zero episodes at `threshold=0.5`, so it
offers no tagging evidence either way; this is the correct, measured result
for that fixture, not an omission.

## 4. What was removed

- `src/models/episode_grouper.py`: the same-channel `is_recovery` computation
  in `_flush_group` (recovery-channel lookup, `_RECOVERY_WINDOW_HOURS = 4.0`
  local literal, the `peak = min(peak, recovery_score_cap)` cap) — deleted
  outright, not left dormant alongside the new path. `recovery_channels` and
  `recovery_score_cap` parameters removed from `group_alerts_into_episodes`
  and `_flush_group`.
- `src/api/services/upload_portions.py`: the `recovery_channels=` /
  `recovery_score_cap=` arguments to `group_alerts_into_episodes` — the only
  call site — updated to match the narrowed signature.
  `stuck_carry.recovery_channels` itself (the `StuckCarry` field in
  `src/models/stuck_fault_detector.py`) was left alone: that file is
  ml-engineer's, not touched by this task, and the field may still be read
  elsewhere.
- `src/config.py`: `RECOVERY_SCORE_CAP` setting deleted entirely.
- `src/config.py`: `RECOVERY_TAG_PRE_RELEASE_GRACE_MINUTES` — added during
  this task, then deleted in the same task in favor of reusing
  `EPISODE_GAP_SECONDS` (§3). Never shipped alongside the reused constant.

## 5. Unchanged by this task

Same-channel suppression — a statistical hit on the SAME channel as its own
just-released stuck fault, within `COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS`
(6 h, `src/api/services/upload_portions.py`) — is a separate, pre-existing
mechanism that prevents the row from ever being flagged/persisted in the
first place. It is untouched by this task and is why fixture A's humidity
channel itself never shows a post-release statistical hit to tag either way.

## 6. Ownership exception

`src/models/episode_grouper.py` (including the `AnomalyEpisode` dataclass)
was edited by backend-engineer for this task — a documented one-off
exception to CLAUDE.md's `src/models/ → ml-engineer` row, made because the
dataclass field and the grouper function touched by this task are in the
same file. No other file under `src/models/` was touched.

## 7. Where the live logic is

- `src/api/services/anomaly_episode_service.py`:
  - `_fetch_recovery_release_events` — loads `(channel, release_ts)` for
    every released stuck fault in
    `[start - window_hours, end + gap_seconds]`, across ALL channels
    (unlike the pre-existing `_fetch_stuck_fault_episode_records`, which is
    scoped to channels already present in this batch — a deliberate
    non-reuse, since cross-channel tagging needs releases on channels OTHER
    than the episode being tagged). The `+ gap_seconds` on the upper bound
    (`gap_seconds` = `EPISODE_GAP_SECONDS`): a release can arrive slightly
    AFTER an episode already underway and still tag it.
  - `_is_cross_channel_recovery_hit` — pure predicate, one episode vs. the
    release list, checking `-gap_seconds <= (start - release) <= window_hours`.
  - `_apply_cross_channel_recovery_tags` — orchestrates the two above, called
    from `fetch_anomaly_episodes` after the rank-and-cap step (tagging never
    affects ranking, so tagging only the surviving capped episodes is
    strictly less work for the same result).
- Index note: the new query filters on `tenant_id`, `greenhouse_id`,
  `detector='stuck_fault'`, and a `release_ts` range. `release_ts` has no
  dedicated index — the existing `anomaly_episodes` indexes cover
  `(tenant_id, greenhouse_id, channel, onset_ts)` and `(fault_type,
  detector)`, neither of which fully covers this query. No new index/migration
  was added for this task (out of scope as briefed); flagging it here in case
  `anomaly_episodes` volume ever makes this scan noticeable.

## 8. Architect work blocked — model substitution for this doc, design note deferred

`arch-task5` (the architect agent dispatched to update
`docs/architecture/episode_grouping.md` and
`docs/architecture/upload_detector_convergence.md` for this task, and to
write the design note covering the landing-at-query-time decision) failed
with "out of usage credits for Fable 5.1," having written nothing.

The two stale `RECOVERY_SCORE_CAP` references left in those files
(`docs/architecture/episode_grouping.md:145`,
`docs/architecture/upload_detector_convergence.md:549`) are being corrected
as a narrow, one-off substitution on Opus — fixing the two factual
references to a setting this task deleted, not a general architecture
review and not a new default model assignment for architect work. The
design note itself (the fuller write-up of why tagging landed at query time
and not detection time) stays assigned to Fable and waits for credits; it
is not being done on Opus as a substitute.

**The Task 5 PR will be opened as DRAFT and must not merge until the design
note lands.**
