"""Task 5 acceptance: cross-channel tagging on real pipeline output.

``test_recovery_tag_window.py`` pins the rule against hand-built rows,
where a boundary can be placed to the second.  This module pins it
against what the pipeline actually produces: one persisted upload of
``tests/fixtures/native_dec_stuck_fault.csv``, its four humidity freezes
detected and their releases written to ``anomaly_episodes`` by the real
detector, then ``GET /api/anomalies/episodes`` over the whole span.

The acceptance case is ``measured_pipe_1_temp`` on 2026-12-16: a pipe
excursion around the 09:15 humidity release, which is what the tag is
for.  It also carries the one trap in this fixture — the query-time view
of that episode is NARROWER than the upload-time view of the same event,
for a reason that has nothing to do with Task 5.  See
``TestTheTwoViewsOfThePipe1Event``.

Harness: ``tests/api/_episode_view_fixtures.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from tests.api._episode_view_fixtures import (  # noqa: F401
    all_query_episodes,
    both_views,
    instant,
    query_episodes,
)

Episode = dict[str, Any]

PIPE_1 = "measured_pipe_1_temp"
STUCK_DETECTOR = "stuck_fault"

# The 2026-12-16 humidity release, and the pipe excursion around it.
# Every value here was MEASURED from the pipeline, not chosen.
RELEASE_1216 = datetime(2026, 12, 16, 9, 15, tzinfo=timezone.utc)
PIPE_1_START = datetime(2026, 12, 16, 9, 10, tzinfo=timezone.utc)
PIPE_1_QUERY_END = datetime(2026, 12, 16, 9, 20, tzinfo=timezone.utc)
PIPE_1_QUERY_SAMPLES = 3
PIPE_1_UPLOAD_SAMPLES = 7
PIPE_1_PEAK = 0.6930225880435137

# The score cap Task 5 deleted from src/config.py. Named here only so the
# no-clamp pin below says what number it is ruling out.
DELETED_RECOVERY_SCORE_CAP = 0.4

# A standalone pipe excursion eight days after the previous release and
# nineteen hours before the next one — far outside the window on both sides.
PIPE_1_STANDALONE_START = datetime(2026, 12, 24, 13, 40, tzinfo=timezone.utc)


def _episode_at(episodes: list[Episode], channel: str, start: datetime) -> Episode:
    """The one episode on *channel* starting at *start* — asserted to exist."""
    matches = [
        ep
        for ep in episodes
        if ep["channel"] == channel and instant(ep["start_ts"]) == start
    ]
    assert len(matches) == 1, (
        f"expected exactly one {channel} episode starting {start.isoformat()}, "
        f"got {[(ep['channel'], ep['start_ts']) for ep in episodes]}"
    )
    return matches[0]


class TestTheAcceptanceCase:
    """The pipe-1 excursion around the 2026-12-16 release is tagged."""

    def test_the_pipe_1_episode_at_0910_is_tagged(self, all_query_episodes):
        episode = _episode_at(all_query_episodes, PIPE_1, PIPE_1_START)
        assert episode["is_recovery"] is True

    def test_it_starts_before_the_release_it_is_tagged_against(
        self, all_query_episodes
    ):
        """Five minutes early — inside the backward resolution bound.

        This is the case the bound exists for, and the reason plain
        start-anchoring without it got this episode wrong: the release it
        responds to had not been recorded yet when the episode started.
        """
        episode = _episode_at(all_query_episodes, PIPE_1, PIPE_1_START)
        assert instant(episode["start_ts"]) < RELEASE_1216

    def test_a_standalone_excursion_days_from_any_release_is_not_tagged(
        self, all_query_episodes
    ):
        """The discriminator: not every pipe-1 excursion is a knock-on."""
        episode = _episode_at(all_query_episodes, PIPE_1, PIPE_1_STANDALONE_START)
        assert episode["is_recovery"] is False

    def test_no_stuck_fault_episode_is_tagged(self, all_query_episodes):
        """A fault is the faulted channel, never a knock-on on another one."""
        tagged_faults = [
            ep
            for ep in all_query_episodes
            if ep["detector"] == STUCK_DETECTOR and ep["is_recovery"]
        ]
        assert tagged_faults == []

    def test_the_fixture_produces_both_tagged_and_untagged_episodes(
        self, all_query_episodes
    ):
        """Guards every other test here from passing on an all-one-way list."""
        flags = {ep["is_recovery"] for ep in all_query_episodes}
        assert flags == {True, False}


class TestTheTwoViewsOfThePipe1Event:
    """The query-time view of this episode is narrower, and that is not a bug.

    ``sensor_readings`` carries one ``trigger_channel`` per timestamp —
    whichever channel scored highest on that row — while the upload
    response carries every channel's hit on every row.  ``measured_hd``
    outscores ``measured_pipe_1_temp`` from 09:25 onward and owns those
    rows, so the query-time grouper never sees the second half of the
    pipe-1 excursion.  Both views are correct about their own input.

    Pinned rather than reconciled: the row-collapse pre-dates Task 5 and
    is orthogonal to it (``docs/handoff/task5_spec.md`` §2).  A future
    change that widens the query-time view will fail here and should
    update these numbers deliberately.
    """

    def test_the_query_time_episode_covers_only_the_rows_pipe_1_won(
        self, all_query_episodes
    ):
        episode = _episode_at(all_query_episodes, PIPE_1, PIPE_1_START)
        assert instant(episode["end_ts"]) == PIPE_1_QUERY_END
        assert episode["sample_count"] == PIPE_1_QUERY_SAMPLES

    def test_the_upload_time_episode_covers_the_whole_excursion(self, both_views):
        episode = _episode_at(both_views.all_response, PIPE_1, PIPE_1_START)
        assert episode["sample_count"] == PIPE_1_UPLOAD_SAMPLES

    def test_the_upload_time_view_of_the_same_event_is_untagged(self, both_views):
        """``is_recovery`` is query-time only; the upload path computes none."""
        episode = _episode_at(both_views.all_response, PIPE_1, PIPE_1_START)
        assert episode["is_recovery"] is False


class TestTaggingChangesNothingElse:
    """Informational only, on pipeline output rather than hand-built rows."""

    def test_the_tagged_episode_reports_the_same_peak_as_the_untagged_view(
        self, both_views, all_query_episodes
    ):
        """One event, two views, one tagged — identical peak either way.

        Compared view against view rather than against a literal, so the
        pin fails when either side drifts, and holds without either side
        being the number of record.
        """
        tagged = _episode_at(all_query_episodes, PIPE_1, PIPE_1_START)
        untagged = _episode_at(both_views.all_response, PIPE_1, PIPE_1_START)
        assert tagged["is_recovery"] is True
        assert untagged["is_recovery"] is False
        assert tagged["peak_score"] == untagged["peak_score"]

    def test_the_tagged_episode_peak_is_not_clamped(self, all_query_episodes):
        """What the deleted cap would have done, stated as what must not happen.

        ``RECOVERY_SCORE_CAP`` clamped a tagged episode's peak to 0.4.
        This episode's measured peak is 0.693, so a returning clamp fails
        here rather than quietly rewriting the ranking.
        """
        episode = _episode_at(all_query_episodes, PIPE_1, PIPE_1_START)
        assert episode["is_recovery"] is True
        assert episode["peak_score"] == pytest.approx(PIPE_1_PEAK)
        assert episode["peak_score"] > DELETED_RECOVERY_SCORE_CAP

    def test_every_episode_both_views_share_reports_the_same_peak(
        self, both_views, all_query_episodes
    ):
        """Generalises the above across the fixture, tagged or not.

        Sample counts are deliberately NOT compared: the row-collapse
        above makes them legitimately differ for some episodes.  Peak
        score is the value tagging used to change, and it must not.
        """
        query_by_key = {
            (ep["channel"], instant(ep["start_ts"])): ep for ep in all_query_episodes
        }
        shared = [
            (upload, query_by_key[(upload["channel"], instant(upload["start_ts"]))])
            for upload in both_views.all_response
            if (upload["channel"], instant(upload["start_ts"])) in query_by_key
        ]
        assert len(shared) >= 2, "too few shared episodes for the pin to mean anything"
        assert any(query["is_recovery"] for _, query in shared), (
            "no shared episode is tagged — the pin would be vacuous"
        )
        for upload, query in shared:
            assert query["peak_score"] == upload["peak_score"], (
                f"{upload['channel']} @ {upload['start_ts']}: "
                f"query {query['peak_score']} != upload {upload['peak_score']}"
            )

    def test_the_stuck_fault_still_outranks_every_tagged_episode(
        self, all_query_episodes
    ):
        """Tagging is applied after ranking and must not reorder anything."""
        tagged = [ep for ep in all_query_episodes if ep["is_recovery"]]
        faults = [
            ep for ep in all_query_episodes if ep["detector"] == STUCK_DETECTOR
        ]
        assert tagged and faults, "both sides must exist for the comparison"
        assert min(ep["peak_score"] for ep in faults) > max(
            ep["peak_score"] for ep in tagged
        )

    def test_the_returned_episodes_stay_sorted_by_peak_score(
        self, all_query_episodes
    ):
        """Tagging runs after the rank-and-cap step and must not disturb it."""
        scores = [ep["peak_score"] for ep in all_query_episodes]
        assert scores == sorted(scores, reverse=True)
