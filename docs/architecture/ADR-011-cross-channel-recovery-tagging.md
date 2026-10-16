# ADR-011: `is_recovery` as Cross-Channel Recovery Tagging

Status: **Accepted** — implemented in this change (Task 5, branch
`task-5-is-recovery-tagging`).
Date: 2026-10-15
Scope: `src/api/services/anomaly_episode_service.py`,
`src/models/episode_grouper.py`, `src/api/services/upload_portions.py`,
`src/api/schemas/anomaly_episodes.py`, `src/api/schemas/sensor_data.py`,
`src/config.py`

## Problem

`is_recovery` used to mean: *a non-stuck hit on the SAME channel as a stuck
fault that has just released*. Its sole effect was to clamp the episode's
score — `peak = min(peak, RECOVERY_SCORE_CAP)` inside `_flush_group`
(`src/models/episode_grouper.py`). That code is deleted; the description of
it here comes from `docs/handoff/task5_spec.md` §4, which recorded it.

The flag was **dead by construction**: no hit it described could ever reach
the grouper.

- `_suppress_fault_window_hits` (`src/api/services/upload_portions.py`) drops
  every statistical hit on the **faulted channel** inside
  `[onset_ts, release_ts + COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS]` — 6 h
  after the release — and `finalize_scores` calls it *before*
  `group_alerts_into_episodes`, in the same function.
- The cap's own window was 4 h after the release
  (`RECOVERY_KNOCKON_WINDOW_HOURS`), strictly inside those 6 h. Every hit the
  cap existed for had already been removed.
- LSTM-only alerts carry no `column`, so `_channel_key` groups them under
  `__lstm__` and they can never match a faulted channel's name either. A
  statistical hit merged with an LSTM one does carry a column, but the merge
  (`_merge_by_timestamp`) runs *after* suppression, so that hit is gone too.

So the field cost a parameter on two function signatures, a config constant
and a rescoring rule, and fired zero times.

## Decision

`is_recovery` now marks an episode **on a channel OTHER than the faulted one**
that starts within `RECOVERY_KNOCKON_WINDOW_HOURS` after that fault's
`release_ts` — a controller response to the fault clearing (a pipe-temp or
heating-setpoint excursion after a humidity freeze releases), not an
independent event. The predicate, from `_is_cross_channel_recovery_hit`:

```
release_ts - EPISODE_GAP_SECONDS  <=  episode.start_ts  <=  release_ts + RECOVERY_KNOCKON_WINDOW_HOURS
```

evaluated against every released stuck fault on a different channel. A
stuck-fault episode is never tagged: it *is* the faulted channel.

Tagging is **informational only** — it never suppresses a hit, never changes
`peak_score`, and never reorders anything. `RECOVERY_SCORE_CAP` is deleted
from `src/config.py` accordingly: a flag that rescores cannot be read as a
label. `_apply_cross_channel_recovery_tags` therefore runs *after* the
rank-and-cap step in `fetch_anomaly_episodes`, which is safe precisely because
it changes neither ranking nor score.

`RECOVERY_KNOCKON_WINDOW_HOURS` (default `4.0`) was reused rather than
replaced by a new constant: it already carried this exact semantic in its
comment and had no production reader, so reusing it keeps the env-var override
key stable.

## Why query time, not detection time

Cross-channel tagging needs `release_ts` for a fault that may have released in
an **earlier upload**. The detection-time grouper's only source for "which
channel just released" is `stuck_carry.recovery_channels`
(`src/models/stuck_fault_detector.py`), which is in-memory state scoped to one
upload — it cannot see that release. The `anomaly_episodes` table (migration
026) is the only cross-upload source for `release_ts`, and
`anomaly_episode_service.py` already reads it (for stuck-fault span
augmentation), so the tagging lives where the data is.

Two consequences follow, both deliberate:

- `AnomalyEpisode.is_recovery` (`src/models/episode_grouper.py`) and
  `AnomalyEpisodeSchema.is_recovery` (the upload response) are permanently
  `False`. They are kept only so the upload response keeps a stable shape.
  The old same-channel computation was deleted outright rather than replaced
  with an upload-time cross-channel one, because no sound upload-time version
  exists.
- The tag has no column. It is derived on every request, so a release
  persisted *after* an excursion was written still tags that excursion on the
  next read — pinned by
  `tests/api/test_recovery_tag_cross_upload.py::TestCrossUploadTagging`.

## The backward bound is `EPISODE_GAP_SECONDS`, and deliberately not `ANOMALY_EPISODE_GAP_SECONDS`

Tagging anchors on the episode's `start_ts`, which can sit slightly *before*
the release it responds to. `release_ts` is "the first non-frozen sample after
the freeze ends" (`AnomalyEpisodeRecord`, `src/db/models.py`), so it lags the
true end of the fault by up to one sample. An episode already underway at that
point is within the pipeline's measurement resolution, not a response that
predates its cause — so the bound is a resolution bound, not a tuned fudge
factor, and reuses the pipeline's own existing definition of "how large a gap
still counts as contiguous".

That definition is `EPISODE_GAP_SECONDS`, the **detection-side** constant —
not `ANOMALY_EPISODE_GAP_SECONDS`, the query-time grouper's own gap, which is
the default of `fetch_anomaly_episodes`'s `gap_seconds` parameter in the very
same module and which `src/config.py` is explicit must stay a separate knob.
The grace compensates for lag in `release_ts`, which the stuck-fault detector
writes at **detection** time, so its resolution is the detection cadence — not
the query-side regrouping knob that happened to produce this episode's
`start_ts`. Both default to `600` today, so the distinction bites only under
an override of `ANOMALY_EPISODE_GAP_SECONDS`, which would otherwise regroup
episodes at one gap while granting recovery grace at another.

## Rejected alternative: overlap-based membership

Tag any episode whose time **span** intersects the window, rather than
anchoring on its start. Rejected: a long excursion on another channel that
began well before the fault and merely happens to still be running when it
releases is not a response to the release, and tagging it attributes someone
else's event to the recovery. Start-anchoring with the resolution bound above
excludes it, because its `start_ts` sits far outside the bound while its span
still crosses the release.

Pinned by
`tests/api/test_recovery_tag_window.py::TestStartAnchoring` — an episode
running from 30 minutes before a release to 25 minutes after it, straddling
the release, must read `is_recovery=False`. Overlap-based tagging would tag it.

## The upload-path and query-time groupers see different events

This is pre-existing and **not introduced by Task 5**, but it is what a future
reader of this ADR will most likely trip over.

`sensor_readings` has `UniqueConstraint(tenant_id, greenhouse_id, timestamp)`,
and `trigger_channel` holds "the raw column name with the highest-|z| hit at
this timestamp" (`src/db/models.py`). The query-time view therefore attributes
each timestamp to exactly **one** channel. The upload response's
`high_anomalies` — the input to `src/models/episode_grouper.py` — is not
collapsed that way and carries every channel's hit on every row.

On fixture A (`tests/fixtures/native_dec_stuck_fault.csv`) the
`measured_pipe_1_temp` excursion around the 2026-12-16 09:15 humidity release
is one episode starting 09:10 in both views, but:

| View | Span | `sample_count` |
|---|---|---|
| upload-path grouper | 09:10 – 09:40 | 7 |
| query-time grouper | 09:10 – 09:20 | 3 |

`measured_hd` outscores `measured_pipe_1_temp` from 09:25 onward and owns the
single `trigger_channel` slot on those rows, so the query-time grouper never
sees the second half of the excursion. Both views are correct about their own
input; no gap constant reconciles them, and this ADR does not try to. The
numbers above are measured and pinned by
`tests/api/test_recovery_tag_fixture_a.py::TestTheTwoViewsOfThePipe1Event`.

This is also why the backward resolution bound is load-bearing rather than
decorative: the query-time episode legitimately starts five minutes *before*
the release that caused it, because the row-collapse already discarded its
after-release rows.

The disagreement above is the observable symptom. Its cause sits one layer
below this decision and can defeat the decision's intent — see **Known
limitations**.

## The contract

From `web/openapi.json`, `AnomalyEpisodeSummary.is_recovery` — the live field,
populated by the query path:

> True when this episode is on a channel other than a stuck-fault channel and
> starts no earlier than EPISODE_GAP_SECONDS before, and within
> RECOVERY_KNOCKON_WINDOW_HOURS after, that fault's release. Informational
> only; does not suppress or rescore.

The same field exists on `AnomalyEpisodeSchema`, the upload-path response,
where it is permanently `False`. Its description is the sentence above plus
one more clause, also quoted from `web/openapi.json`:

> Always False in this upload-path response -- the live computation is
> query-time only; see AnomalyEpisodeSummary.is_recovery.

## Consequences

- **No index for the new query.** `_fetch_recovery_release_events` filters on
  `tenant_id`, `greenhouse_id`, `detector`, and a `release_ts` range. The
  `anomaly_episodes` indexes are
  `ix_anomaly_episodes_tenant_gh_channel_onset`, a partial index on
  `is_ongoing`, and `ix_anomaly_episodes_fault_type_detector`; the first
  covers only the two leading columns of this filter, and `release_ts` carries
  no index at all. Accepted as out of scope rather than overlooked — flagged
  here in case `anomaly_episodes` volume ever makes the scan visible.
- **The query is deliberately not channel-scoped**, unlike the neighbouring
  `_fetch_stuck_fault_episode_records`: cross-channel tagging needs releases on
  channels *other* than the episode's own, including channels with no
  stuck-fault alert row inside the requested window.
- **Same-channel suppression is untouched.** The 6 h post-release suppression
  described under Problem is a separate, pre-existing mechanism that prevents
  the row from being flagged at all. It is why a faulted channel shows no
  post-release statistical hit to tag either way.

## Known limitations

**The storage model can hide the very case this policy exists to preserve.**

> **Resolved as a decision, not yet as code, by
> [ADR-012](./ADR-012-concurrent-channel-hits.md).** Read it before acting on
> anything in this section: it measures the effect, reframes the cause, and
> records the chosen fix. Two claims below are narrowed by it, noted inline.

The policy above is built on never suppressing: a real fault on another
channel may genuinely coincide with a recovery, so the tag labels the episode
and leaves its score, rank and rows alone. That holds for everything the
tagging can see. But one channel per timestamp survives into
`sensor_readings` — `UniqueConstraint(tenant_id, greenhouse_id, timestamp)`,
with `trigger_channel` keeping a single hit (`src/db/models.py`). A
**concurrent** fault that scores below another channel at the same timestamps
is therefore already gone from the view `_apply_cross_channel_recovery_tags`
reads, before it runs. Tagging cannot preserve, label or even notice what the
query path dropped.

Two refinements from ADR-012, which measured this:

- **The loss is in the query path, not the database.** `raw_readings.data["anomaly_columns"]`
  already carries the complete channel set for every collided timestamp —
  measured 17 of 17 on fixture A. Episode grouping simply never reads it
  (`_fetch_minimal_flagged_rows`, `anomaly_group_queries.py`). The JSON holds
  channel **names only**, so it can rebuild spans but not `peak_score`.
- **The winner is not chosen by |z| alone,** despite the `src/db/models.py`
  docstring quoted above and in the section before it. `build_trigger_data_by_timestamp`
  ranks by `anomaly_rank_key` — severity, then detector tier, then |z_score|,
  with score only a later tiebreak. The model docstring is stale; it is
  reproduced verbatim above rather than silently corrected, because it is a
  quote.

This is not a flaw in the decision recorded here, and this ADR does not
resolve it: it is a gap one layer beneath, pre-existing and explicitly out of
scope for Task 5. The fixture A split in the section above is the observable
instance of it — one of 6 damaged episodes out of 10, per ADR-012's
measurement. Its backlog heading remains the stable reference for the history:

> `AGENT_MEMORY.md` → **BACKLOG-1 — multi-channel hits per timestamp in the
> persisted view** (under "Backlog — three follow-ups out of Task 5
> (Orchestrator, 2026-10-15)")

There is no issue tracker. The behaviour is pinned, not fixed, by
`tests/api/test_recovery_tag_fixture_a.py::TestTheTwoViewsOfThePipe1Event`, so
a future change that widens the query-time view fails there and updates these
numbers deliberately. ADR-012 spells out exactly what that class and
`TestTaggingChangesNothingElse` must become when it does, and notes that the
tagging assertions in this ADR must be re-run — on fixture A no `is_recovery`
verdict is expected to flip, but that is derived, not measured.

## Residual follow-up

**Resolved — the field is gone.** `StuckCarry.recovery_channels` was removed
from `src/models/stuck_fault_detector.py` in `d0bcf0d`, and the four test
assertions that named it were retired in `7ef81c5`. It had no reader anywhere
in `src/`. A runtime `__getattribute__` trap over the full suite found
accesses from exactly six sites: `to_dict`/`from_dict` serialization, the
single write, and those four assertions. At the write site the *same*
`FaultWindow` object was appended to `completed_faults` on the next line, so
the field was redundant rather than merely unread — and held strictly less,
being channel-keyed and therefore overwriting when a channel faulted twice.

**This section previously said the opposite, and that is worth recording.**
It claimed the field was "left in place because it still has that reader",
naming the statistical detector's baseline purge as the reader. That was never
true at the time it was written. The purge — `_purge_fault_from_stat_carry` —
had been deleted in `7347f86` (2026-09-22) and replaced by
`_exclude_faults_from_baseline` (`src/api/services/upload_portions.py:476`),
which reads `completed_faults`, not `recovery_channels`. This ADR wrote the
claim down on 2026-10-15, three weeks later.

**The lesson:** the claim was not independently derived. It came from reading
`recovery_channels`' own docstring and restating it — and that docstring was
the very artifact the follow-up existed to correct. A comment under audit is
the one source that cannot corroborate itself. Settle "does anything read
this?" against call sites, never against the prose attached to the
declaration.
