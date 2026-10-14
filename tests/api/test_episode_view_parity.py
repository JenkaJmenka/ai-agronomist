"""Equality pin: the two views of a stuck fault must report the same span.

Three unrelated things in this repo are called an "episode"
(``docs/handoff/task3_4_spec.md`` §1).  Two of them are VIEWS of the same
real-world event, and this module pins those two against each other:

* **detection-time** — ``src/models/episode_grouper.py`` groups the alerts
  ``finalize_scores`` produced; the result is returned once in the upload
  response and never persisted;
* **query-time** — ``GET /api/anomalies/episodes`` recomputes episodes per
  request from flagged ``sensor_readings``
  (``src/api/services/anomaly_episode_service.py``) and, for a stuck fault
  only, sources the real span from the ``anomaly_episodes`` table
  (migration 026, ``AnomalyEpisodeRecord``).

The bug being guarded: a freeze emits exactly ONE flagged row, so a
stuck-fault episode's row-derived span is a single-row group, and
``duration_seconds`` read 0.0 for every stuck fault at query time — a
frozen channel reported as having recovered instantly.

Every equality test here reads BOTH sides inside the test and compares them
to each other, never to a literal, so the pin fails when EITHER side
drifts.  The one place literals appear is ``TestMeasuredSpans``, which
checks the documented durations against what fixture A actually produces.
Harness and fixtures: ``tests/api/_episode_view_fixtures.py``.

Three defects were found by these pins and are fixed; each equality test
below is also their regression guard:

* the record lookup keyed on a tz-naive ``onset_ts`` under SQLite and so
  never matched — now normalized by ``anomaly_episode_service._as_utc``.
  Both driver shapes are unit-pinned in
  ``tests/api/test_episode_record_keying.py``, because every test here runs
  on SQLite and would not notice the aware (asyncpg) branch breaking;
* ``_augment_stuck_fault_episode`` replaced ``duration_seconds`` but not
  ``end_ts``, so a query-time stuck episode ended at its own onset;
* ``AnomalyEpisodeSchema`` declared neither count, and Pydantic v2 drops
  unmodeled keys silently, so the upload response lost both counts that
  ``AnomalyEpisode.to_dict()`` had supplied.

A fourth, found while verifying the second, is fixed the same way: the
query side wrote ``end_ts`` through ``_iso`` alone, which drops the UTC
offset for the naive datetimes SQLite returns — see
``TestTimestampsDeclareTheirZone``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tests.api._episode_view_fixtures import (  # noqa: F401
    both_views,
    instant,
    query_episodes,
    query_episodes_without_records,
    views_without_episode_records,
)

# Documented spans of fixture A's four freezes
# (``tests/workers/test_stuck_async_parity.py::EXPECTED_DURATIONS``) —
# CHECKED against the pipeline below, never forced onto it.
DOCUMENTED_DURATIONS = (125400.0, 79500.0, 172500.0, 589800.0)

Episode = dict[str, Any]


def _paired(
    detection: list[Episode], query: list[Episode]
) -> list[tuple[Episode, Episode]]:
    """Match the two views by (channel, start instant); require every pair.

    Pairing on identity rather than on list position means a missing or an
    extra episode on either side fails the pin instead of silently
    shortening the comparison.
    """
    query_by_key = {(ep["channel"], instant(ep["start_ts"])): ep for ep in query}
    pairs: list[tuple[Episode, Episode]] = []
    for detected in detection:
        key = (detected["channel"], instant(detected["start_ts"]))
        assert key in query_by_key, f"query-time view is missing {key}"
        pairs.append((detected, query_by_key[key]))
    assert len(pairs) == len(query), "query-time view has episodes detection-time lacks"
    assert pairs, "no stuck-fault episodes to compare — the pin would be vacuous"
    return pairs


class TestViewIdentity:
    """The two views describe the same set of events."""

    def test_both_views_report_the_same_stuck_episodes(
        self, both_views, query_episodes
    ):
        pairs = _paired(both_views.detection, query_episodes)
        assert len(pairs) == 4, [ep["start_ts"] for ep in both_views.detection]

    def test_every_stuck_episode_is_humidity_on_both_sides(
        self, both_views, query_episodes
    ):
        """Fixture A freezes humidity only — temperature must stay at zero."""
        assert {ep["channel"] for ep in both_views.detection} == {"humidity"}
        assert {ep["channel"] for ep in query_episodes} == {"humidity"}

    def test_response_and_dataclass_agree_on_the_episode_set(self, both_views):
        """The response is the dataclass list, so a drift here is a serializer bug."""
        assert [ep["start_ts"] for ep in both_views.response] == [
            ep["start_ts"] for ep in both_views.detection
        ]


class TestSpanEquality:
    """One source of truth: identical values, or the pin fails."""

    def test_start_instant_is_identical_on_both_sides(
        self, both_views, query_episodes
    ):
        for detected, queried in _paired(both_views.response, query_episodes):
            assert instant(queried["start_ts"]) == instant(detected["start_ts"])

    def test_end_instant_is_identical_on_both_sides(self, both_views, query_episodes):
        for detected, queried in _paired(both_views.response, query_episodes):
            assert instant(queried["end_ts"]) == instant(detected["end_ts"]), (
                detected["start_ts"]
            )

    def test_duration_seconds_is_identical_on_both_sides(
        self, both_views, query_episodes
    ):
        for detected, queried in _paired(both_views.response, query_episodes):
            assert queried["duration_seconds"] == detected["duration_seconds"], (
                detected["start_ts"]
            )

    def test_severity_is_identical_on_both_sides(self, both_views, query_episodes):
        for detected, queried in _paired(both_views.response, query_episodes):
            assert queried["severity"] == detected["severity"], detected["start_ts"]

    def test_sample_count_is_one_on_both_sides(self, both_views, query_episodes):
        """``sample_count`` is the ALERT count: one alert per freeze, both sides."""
        for detected, queried in _paired(both_views.response, query_episodes):
            assert detected["sample_count"] == 1, detected["start_ts"]
            assert queried["sample_count"] == 1, detected["start_ts"]

    def test_both_counts_are_identical_on_both_sides(self, both_views, query_episodes):
        for detected, queried in _paired(both_views.response, query_episodes):
            assert queried["frozen_sample_count"] == detected["frozen_sample_count"], (
                detected["start_ts"]
            )
            assert queried["raw_slot_count"] == detected["raw_slot_count"], (
                detected["start_ts"]
            )

    def test_response_carries_the_dataclass_counts(self, both_views):
        """Regression: Pydantic v2 silently dropped these two unmodeled keys."""
        for response_ep, dataclass_ep in zip(both_views.response, both_views.detection):
            assert (
                response_ep["frozen_sample_count"]
                == dataclass_ep["frozen_sample_count"]
            ), response_ep["start_ts"]
            assert response_ep["raw_slot_count"] == dataclass_ep["raw_slot_count"], (
                response_ep["start_ts"]
            )


class TestSpanInvariant:
    """``end_ts - start_ts == duration_seconds`` — on EACH side independently.

    ``episode_grouper``'s decision 5.  Held on both sides it is also what
    makes the equality tests above non-trivial: three fields that agree
    across the views but contradict each other within one view would still
    be wrong.
    """

    def test_detection_side_span_matches_its_own_duration(self, both_views):
        for episode in both_views.detection:
            span = (
                instant(episode["end_ts"]) - instant(episode["start_ts"])
            ).total_seconds()
            assert span == episode["duration_seconds"], episode["start_ts"]

    def test_query_side_span_matches_its_own_duration(self, query_episodes):
        for episode in query_episodes:
            span = (
                instant(episode["end_ts"]) - instant(episode["start_ts"])
            ).total_seconds()
            assert span == episode["duration_seconds"], episode["start_ts"]


class TestMeasuredSpans:
    """The documented figures, checked against what the pipeline produces."""

    def test_detection_side_reports_the_documented_durations(self, both_views):
        measured = tuple(ep["duration_seconds"] for ep in both_views.detection)
        assert measured == DOCUMENTED_DURATIONS

    def test_query_side_reports_the_documented_durations(self, query_episodes):
        measured = tuple(ep["duration_seconds"] for ep in query_episodes)
        assert measured == DOCUMENTED_DURATIONS

    def test_detection_side_counts_are_measured_not_none(self, both_views):
        """Arm (a) of the span resolution supplies both counts for a closed freeze."""
        for episode in both_views.detection:
            assert episode["frozen_sample_count"] is not None, episode["start_ts"]
            assert episode["raw_slot_count"] is not None, episode["start_ts"]
            assert episode["raw_slot_count"] >= episode["frozen_sample_count"]


class TestAbsentEpisodeRecord:
    """No ``anomaly_episodes`` row: the span is UNKNOWN, and unknown is not 0.0.

    This is the case most likely to regress into the original bug, so each
    field is asserted ``is None`` rather than for falsiness — ``0.0`` and
    ``None`` are both falsy and the entire distinction is that they differ.
    ``AnomalyEpisodeSummary.duration_seconds`` was widened from ``float`` to
    ``Optional[float]`` for exactly this.
    """

    def test_duration_is_unknown_rather_than_zero(
        self, query_episodes_without_records
    ):
        assert query_episodes_without_records, "no stuck episode to check"
        for episode in query_episodes_without_records:
            assert episode["duration_seconds"] is None, episode["start_ts"]

    def test_frozen_sample_count_is_unknown(self, query_episodes_without_records):
        for episode in query_episodes_without_records:
            assert episode["frozen_sample_count"] is None, episode["start_ts"]

    def test_raw_slot_count_is_unknown(self, query_episodes_without_records):
        for episode in query_episodes_without_records:
            assert episode["raw_slot_count"] is None, episode["start_ts"]

    def test_sample_count_is_still_one(self, query_episodes_without_records):
        """The alert count does not depend on the table row existing."""
        for episode in query_episodes_without_records:
            assert episode["sample_count"] == 1, episode["start_ts"]

    def test_four_stuck_episodes_survive_the_missing_rows(
        self, query_episodes_without_records
    ):
        """The episodes themselves come from the readings, not from the table."""
        assert len(query_episodes_without_records) == 4


class TestZeroDurationNeverReturns:
    """The original bug, guarded whichever branch the lookup takes."""

    def test_query_side_never_reports_zero_duration_for_a_stuck_fault(
        self, query_episodes
    ):
        for episode in query_episodes:
            assert episode["duration_seconds"] != 0.0, episode["start_ts"]

    def test_query_side_never_reports_zero_duration_without_a_record(
        self, query_episodes_without_records
    ):
        for episode in query_episodes_without_records:
            assert episode["duration_seconds"] != 0.0, episode["start_ts"]


class TestTimestampsDeclareTheirZone:
    """Every timestamp the query view emits must say which zone it is in.

    ``_augment_stuck_fault_episode`` writes ``_iso(_as_utc(record.end_ts))``;
    ``_iso`` only calls ``.isoformat()``, so without the ``_as_utc`` the
    offset was kept for the aware datetimes asyncpg returns and lost for
    SQLite's naive ones.  The instant was right either way — which is why
    the equality pins above, comparing instants rather than strings, passed
    while ``end_ts`` read ``"2026-12-15T10:50:00"`` beside a ``start_ts`` of
    ``"2026-12-14T00:00:00+00:00"``.  JS ``new Date()`` parses an
    offset-less date-time as LOCAL time, so a browser east of UTC rendered
    the end of a freeze early.

    Checked for the episode's own three timestamps, not just the one that
    broke.  Pre-existing and deliberately NOT covered: ``samples[].timestamp``
    is still offset-less, through ``sample_enrichment`` rather than this
    path, on every episode and every detector — one of the ``_iso()`` call
    sites logged as a deferred audit.
    """

    EPISODE_TIMESTAMPS = ("start_ts", "end_ts", "peak_ts")

    def test_query_side_timestamps_are_zone_qualified(self, query_episodes):
        for episode in query_episodes:
            for field in self.EPISODE_TIMESTAMPS:
                parsed = datetime.fromisoformat(episode[field].replace("Z", "+00:00"))
                assert parsed.tzinfo is not None, f"{field}={episode[field]!r}"

    def test_end_ts_string_is_byte_identical_on_both_sides(
        self, both_views, query_episodes
    ):
        """Stronger than an instant comparison: the two views serialize alike.

        Both sides reach this string through ``.isoformat()`` on an aware UTC
        datetime, so a consumer reading one view and then the other sees the
        same bytes.  A drift here means one view changed its wire format.
        """
        for detected, queried in _paired(both_views.response, query_episodes):
            assert queried["end_ts"] == detected["end_ts"], detected["start_ts"]
