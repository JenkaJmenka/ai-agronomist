"""The episode span contract: ``end_ts - start_ts == duration_seconds``.

``src/models/episode_grouper.py`` docstring rules 5 and 6. Both exist because
a stuck-fault episode's span does NOT come from its alert rows: a freeze emits
exactly ONE alert, stamped at the onset, so the row span is zero-length. The
duration used to be resolved on its own while ``end_ts`` stayed at the last
alert timestamp, and the upload response reported ``duration_seconds=589800``
next to a span of 0 s on the same episode. ``_stuck_fault_span`` now returns
the end boundary and the duration from one resolution of the freeze.

Two things are pinned here, and neither was guarded before:

* **Rule 5** — the cross-field invariant, for EVERY episode and EVERY
  detector. Exercised over the real upload response for fixture A (completed
  freezes plus statistical episodes) and over each of the four stuck-fault
  resolution strategies. Cross-field consistency is exactly what rots
  silently: each field on its own stays plausible.
* **Rule 6** — the resolution ORDER, first hit wins. The case that matters is
  a completed ``FaultWindow`` and an open ``ChannelFreezeState`` describing
  the same ``(channel, onset_ts)``: if that order ever inverted, a CLOSED
  episode would be rewritten as ongoing with a longer span.

Companion files, and why they do not already cover this:

* ``test_episode_duration_fallback.py`` and
  ``test_stuck_episode_duration_sources.py`` pin which source a DURATION
  comes from. They pass ``open_freezes`` to nothing and assert no ``end_ts``.
* ``tests/workers/test_stuck_async_parity.py`` pins the ongoing episode's
  ``end_ts`` and ``duration_seconds`` by value through the production call
  site (``finalize_scores``, the only caller that passes ``open_freezes``),
  which is where the synthetic ongoing fixture lives.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.config import STUCK_WALLCLOCK_MIN_HOURS
from src.models.episode_grouper import group_alerts_into_episodes
from src.models.stuck_fault_detector import ChannelFreezeState, FaultWindow
from tests.api.test_fixture_a_stuck_contract import (
    REFERENCE_CHUNK_SIZE,
    fixture_a_response,
)

SECONDS_PER_HOUR = 3600.0
FROZEN_CHANNEL = "humidity"
FROZEN_VALUE = 100.0

# Hand-built freeze, used by the resolution-order cases. The three candidate
# boundaries are far apart (6 h / 10 h / 30 h past the onset) so that the
# resolved span names its own source: no two strategies can round into each
# other.
ONSET_DT = datetime(2026, 6, 1, tzinfo=timezone.utc)
ONSET_TS = ONSET_DT.isoformat()
GATE_HOURS = float(STUCK_WALLCLOCK_MIN_HOURS)
COMPLETED_WINDOW_HOURS = 10.0
OPEN_STATE_HOURS = 30.0


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _span_seconds(episode) -> float:
    """``end_ts - start_ts`` in seconds, read off the episode's own strings.

    Takes the serialised timestamps rather than any internal datetime so the
    invariant is checked on what a consumer actually receives — the API
    response and the ``anomaly_episodes`` row are built from these.
    """
    return (_parse(str(episode.end_ts)) - _parse(str(episode.start_ts))).total_seconds()


def _stuck_alert(**overrides) -> UploadHighAnomalyDetail:
    """One stuck_fault hit at ``ONSET_TS``, carrying the gate-crossing hours."""
    fields: dict = {
        "score": 1.0,
        "timestamp": ONSET_TS,
        "detector": "stuck_fault",
        "column": FROZEN_CHANNEL,
        "severity": "urgent",
        "method": "wallclock_stuck",
        "value": FROZEN_VALUE,
        "z_score": None,
        "frozen_hours_at_detection": GATE_HOURS,
    }
    fields.update(overrides)
    return UploadHighAnomalyDetail(**fields)


def _completed_window(onset_dt: datetime = ONSET_DT) -> FaultWindow:
    """A released freeze whose last frozen sample is 10 h past the onset."""
    last_frozen_dt = onset_dt + timedelta(hours=COMPLETED_WINDOW_HOURS)
    release_dt = last_frozen_dt + timedelta(minutes=5)
    return FaultWindow(
        channel=FROZEN_CHANNEL,
        onset_ts=onset_dt.isoformat(),
        onset_dt=onset_dt,
        release_ts=release_dt.isoformat(),
        release_dt=release_dt,
        last_frozen_ts=last_frozen_dt.isoformat(),
        last_frozen_dt=last_frozen_dt,
        has_fired=True,
    )


def _open_state(onset_dt: datetime = ONSET_DT) -> ChannelFreezeState:
    """A freeze still open at end of input, last seen 30 h past the onset."""
    last_dt = onset_dt + timedelta(hours=OPEN_STATE_HOURS)
    return ChannelFreezeState(
        frozen_value=FROZEN_VALUE,
        onset_ts=onset_dt.isoformat(),
        onset_dt=onset_dt,
        last_ts=last_dt.isoformat(),
        last_dt=last_dt,
        sample_count=360,
    )


def _sole_episode(alert: UploadHighAnomalyDetail, **sources):
    episodes = group_alerts_into_episodes([alert], **sources)
    assert len(episodes) == 1, f"Expected 1 episode, got {len(episodes)}"
    return episodes[0]


# ---------------------------------------------------------------------------
# Rule 6 — resolution order, first hit wins
# ---------------------------------------------------------------------------

# (sources, expected hours past the onset, expected is_ongoing). The expected
# hours are both the end boundary AND the duration: that is the contract.
RESOLUTION_CASES = [
    pytest.param(
        {"completed_faults": [_completed_window()], "open_freezes": {FROZEN_CHANNEL: _open_state()}},
        COMPLETED_WINDOW_HOURS,
        False,
        id="completed-window-wins-over-open-state",
    ),
    pytest.param(
        {"completed_faults": [_completed_window()]},
        COMPLETED_WINDOW_HOURS,
        False,
        id="completed-window-only",
    ),
    pytest.param(
        {"open_freezes": {FROZEN_CHANNEL: _open_state()}},
        OPEN_STATE_HOURS,
        True,
        id="open-state-wins-over-alert-field",
    ),
    pytest.param(
        {"open_freezes": {FROZEN_CHANNEL: _open_state(ONSET_DT + timedelta(hours=50))}},
        GATE_HOURS,
        True,
        id="open-state-of-a-different-freeze-falls-through",
    ),
    pytest.param({}, GATE_HOURS, True, id="no-source-uses-the-alert-field"),
]


@pytest.mark.parametrize("sources,expected_hours,expected_ongoing", RESOLUTION_CASES)
def test_the_resolved_span_names_its_source(
    sources: dict, expected_hours: float, expected_ongoing: bool,
) -> None:
    """Each strategy produces its own boundary, and the order is as documented.

    The first case is the one that matters: a completed window and an open
    state for the SAME ``(channel, onset_ts)``. The window wins, so the
    episode ends 10 h past the onset and is closed. Were the order inverted
    it would end 30 h past the onset and claim ``is_ongoing=True`` — a
    released freeze rewritten as still frozen.

    The fourth case pins the matching that makes the order safe: the open
    state belongs to a LATER freeze on the same channel (a channel can
    release one freeze and open another), so it must not be laid over this
    episode. The resolution falls through to the alert's own 6 h.
    """
    episode = _sole_episode(_stuck_alert(), **sources)

    assert _parse(str(episode.end_ts)) == ONSET_DT + timedelta(hours=expected_hours)
    assert episode.duration_seconds == expected_hours * SECONDS_PER_HOUR
    assert episode.is_ongoing is expected_ongoing


@pytest.mark.parametrize("sources,expected_hours,_ongoing", RESOLUTION_CASES)
def test_every_stuck_strategy_keeps_the_span_invariant(
    sources: dict, expected_hours: float, _ongoing: bool,
) -> None:
    """Rule 5 holds whichever strategy resolved the freeze.

    The claim is per-strategy, not per-episode: the boundary and the duration
    leave ``_stuck_fault_span`` together, so no strategy may derive one
    without the other.
    """
    episode = _sole_episode(_stuck_alert(), **sources)
    assert _span_seconds(episode) == episode.duration_seconds


def test_the_zero_length_last_resort_also_agrees_with_its_duration() -> None:
    """The one case where ``end_ts == start_ts`` is truthful.

    No window, no open state and no hours on the alert: nothing about the
    freeze is recoverable, so the span collapses onto the onset — and
    ``duration_seconds`` is 0.0 to match, rather than the old pairing of a
    non-zero duration with a zero-length span.
    """
    episode = _sole_episode(_stuck_alert(frozen_hours_at_detection=None))

    assert episode.duration_seconds == 0.0
    assert str(episode.end_ts) == str(episode.start_ts)
    assert _span_seconds(episode) == 0.0


def test_a_non_stuck_episode_spans_its_alert_rows() -> None:
    """For every other detector the alert rows ARE the span.

    Pinned next to the stuck cases because rule 5 is a claim about every
    detector, and this is the branch the stuck resolution bypasses: three
    statistical alerts 5 min apart give a 600 s episode ending on the last
    of them.
    """
    alerts = [
        UploadHighAnomalyDetail(
            score=0.9,
            timestamp=(ONSET_DT + timedelta(minutes=5 * index)).isoformat(),
            detector="statistical",
            column="temperature",
            severity="warning",
            z_score=4.0,
        )
        for index in range(3)
    ]
    episodes = group_alerts_into_episodes(alerts)

    assert len(episodes) == 1
    assert _parse(str(episodes[0].end_ts)) == ONSET_DT + timedelta(minutes=10)
    assert episodes[0].duration_seconds == 600.0
    assert _span_seconds(episodes[0]) == episodes[0].duration_seconds


# ---------------------------------------------------------------------------
# Rule 5 — the invariant over the real upload response
# ---------------------------------------------------------------------------

# Last frozen sample of each of fixture A's four fired freezes, as the real
# sync upload reports it — the completed-window strategy, unaffected by
# passing open freezes. Measured against the persisted ``anomaly_episodes``
# rows for the same run.
EXPECTED_COMPLETED_END_TS = [
    "2026-12-15T10:50:00+00:00",
    "2026-12-16T09:10:00+00:00",
    "2026-12-18T11:15:00+00:00",
    "2026-12-25T08:50:00+00:00",
]
EXPECTED_COMPLETED_DURATIONS = [125400.0, 79500.0, 172500.0, 589800.0]


class TestFixtureAResponseEpisodes:
    """Every episode of a real upload response, not a hand-built one.

    ``fixture_a_response`` is the cached ``run_cold_run_upload`` run shared
    with ``test_fixture_a_stuck_contract.py`` and
    ``test_stuck_episode_duration_sources.py``, so this costs no extra
    scoring pass. Its episodes reach ``group_alerts_into_episodes`` through
    ``finalize_scores``, which is the only place ``open_freezes`` is passed.
    """

    @pytest.fixture(scope="class")
    def episodes(self) -> list:
        return sorted(
            fixture_a_response(REFERENCE_CHUNK_SIZE).episodes or [],
            key=lambda episode: (episode.detector, str(episode.start_ts)),
        )

    @pytest.fixture(scope="class")
    def stuck_episodes(self, episodes: list) -> list:
        return sorted(
            (ep for ep in episodes if ep.detector == "stuck_fault"),
            key=lambda episode: str(episode.start_ts),
        )

    def test_more_than_one_detector_is_represented(self, episodes: list) -> None:
        """Non-vacuity for "every detector": both branches are in the sweep.

        The statistical episodes are counted but their number is not pinned —
        that absolute figure is an unverified self-referential baseline and
        already lives in exactly one place
        (``tests/workers/test_stat_chunk_invariance.py``). What matters here
        is that the sweep below is not four stuck episodes alone.
        """
        detectors = {episode.detector for episode in episodes}
        assert {"statistical", "stuck_fault"} <= detectors
        assert len([ep for ep in episodes if ep.detector == "statistical"]) > 1

    def test_every_episode_span_equals_its_duration(self, episodes: list) -> None:
        """Rule 5, swept over the whole response."""
        disagreements = {
            f"{episode.detector}:{episode.channel}:{episode.start_ts}": (
                _span_seconds(episode),
                episode.duration_seconds,
            )
            for episode in episodes
            if _span_seconds(episode) != episode.duration_seconds
        }
        assert disagreements == {}, (
            f"(span, duration) disagree on {len(disagreements)} of "
            f"{len(episodes)} episodes: {disagreements}"
        )

    def test_the_stuck_spans_cannot_have_come_from_the_alert_rows(
        self, stuck_episodes: list
    ) -> None:
        """What makes the sweep above non-trivial for a stuck episode.

        Each of these episodes is ONE alert row, so ``group[-1][0] ==
        group[0][0]``: if ``end_ts`` were still taken from the last alert,
        every span here would be 0 s while the duration stayed in the tens of
        hours. A multi-row statistical episode satisfies the invariant either
        way, which is why it cannot carry this check.
        """
        assert [episode.sample_count for episode in stuck_episodes] == [1, 1, 1, 1]
        assert all(episode.duration_seconds > 0.0 for episode in stuck_episodes)

    def test_the_four_completed_freezes_keep_their_boundaries(
        self, stuck_episodes: list
    ) -> None:
        """Passing ``open_freezes`` must not disturb a RELEASED freeze.

        All four of fixture A's fired freezes end inside the file, so each
        matches a completed ``FaultWindow`` and resolves at strategy (a). The
        open-state strategy is reachable in this run — the carry is passed —
        so these four values are what says it stayed out of the way.
        """
        assert [str(ep.end_ts) for ep in stuck_episodes] == EXPECTED_COMPLETED_END_TS
        assert [
            ep.duration_seconds for ep in stuck_episodes
        ] == EXPECTED_COMPLETED_DURATIONS
        assert [ep.is_ongoing for ep in stuck_episodes] == [False] * 4
