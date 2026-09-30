"""Which of the two duration sources a stuck episode actually uses.

Step-5 verification of spec items 9 and 11. ``_stuck_fault_duration`` has two
strategies and they mean different things:

* a matching completed ``FaultWindow`` gives the full frozen span
  (``is_ongoing=False``) — an exact duration;
* otherwise the freeze was still open at detection, so the alert's own
  gate-crossing hours are a LOWER BOUND (``is_ongoing=True``).

Only the second strategy changed on this branch (it reads
``frozen_hours_at_detection``, falling back to ``z_score`` for legacy rows).
``tests/models/test_episode_duration_fallback.py`` pins that strategy's four
field combinations. This file pins the two things that file leaves open: that
the lower bound really is the gate value and not the full elapsed span, and
that fixture A does not reach the fallback at all — so "unchanged vs main"
there is a statement about the untouched strategy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.config import STUCK_WALLCLOCK_MIN_HOURS
from src.models.episode_grouper import group_alerts_into_episodes
from src.models.stuck_fault_detector import StuckCarry, WallclockStuckDetector
from tests.api.test_fixture_a_stuck_contract import (
    REFERENCE_CHUNK_SIZE,
    fixture_a_response,
)

SECONDS_PER_HOUR = 3600.0
GATE_HOURS = float(STUCK_WALLCLOCK_MIN_HOURS)
FROZEN_CHANNEL = "humidity"
FROZEN_VALUE = 100.0
CADENCE_SECONDS = 300

# Long enough that the elapsed span is unmistakably larger than the gate
# (~16.6 h vs 6 h), so a duration revised at end-of-stream cannot be confused
# with one stamped at the gate.
NEVER_RELEASED_ROW_COUNT = 200

# Freeze durations measured from the CSV itself in
# tests/fixtures/NATIVE_FIXTURES.md:31 onward — ground truth independent of
# this code, unlike the statistical hit count on the same fixture.
EXPECTED_EPISODE_HOURS = {
    "2026-12-14T00:00:00+00:00": 34.83,
    "2026-12-15T11:05:00+00:00": 22.08,
    "2026-12-16T11:20:00+00:00": 47.92,
    "2026-12-18T13:00:00+00:00": 163.83,
}


# ---------------------------------------------------------------------------
# 9 — a freeze still open at end of stream is stamped once, never revised
# ---------------------------------------------------------------------------

def _never_released_frame() -> pd.DataFrame:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return pd.DataFrame(
        {
            "timestamp": [
                start + timedelta(seconds=index * CADENCE_SECONDS)
                for index in range(NEVER_RELEASED_ROW_COUNT)
            ],
            "temperature": [
                20.0 + 0.1 * (index % 50) for index in range(NEVER_RELEASED_ROW_COUNT)
            ],
            FROZEN_CHANNEL: [FROZEN_VALUE] * NEVER_RELEASED_ROW_COUNT,
            "co2": [400.0 + (index % 20) for index in range(NEVER_RELEASED_ROW_COUNT)],
            "light": [0.0] * NEVER_RELEASED_ROW_COUNT,
        }
    )


@pytest.fixture(scope="module")
def never_released_alert() -> tuple[dict, StuckCarry, float]:
    """One alert from a freeze that never recovers, plus the full elapsed span."""
    detector = WallclockStuckDetector()
    carry = StuckCarry()
    frame = _never_released_frame()
    raw_alerts = detector.detect_chunk(frame, carry)
    raw_alerts.extend(detector.flush(carry))

    assert len(raw_alerts) == 1, f"Expected 1 stuck alert, got {len(raw_alerts)}"
    elapsed_hours = (
        (NEVER_RELEASED_ROW_COUNT - 1) * CADENCE_SECONDS / SECONDS_PER_HOUR
    )
    return raw_alerts[0], carry, elapsed_hours


class TestOpenFreezeKeepsItsAtDetectionValue:
    """Spec 9. The alert is stamped at the first gate crossing and never revised."""

    def test_the_freeze_is_still_open(self, never_released_alert) -> None:
        """Premise: no completed window, so the fallback is the only source."""
        _, carry, _ = never_released_alert
        assert carry.completed_faults == []

    def test_the_stamped_value_is_the_gate_duration(self, never_released_alert) -> None:
        alert, _, _ = never_released_alert
        assert alert["duration_hours"] == pytest.approx(GATE_HOURS, abs=0.1)

    def test_the_stamped_value_is_not_the_full_elapsed_span(
        self, never_released_alert
    ) -> None:
        """The assertion that makes "never revised" mean something.

        The stream runs ~16.6 h past the onset. If the value were recomputed
        at flush time it would be that, not 6 h — and a test that only
        compared the episode against the alert's own field would agree with
        either number.
        """
        alert, _, elapsed_hours = never_released_alert
        assert elapsed_hours > GATE_HOURS * 2
        assert alert["duration_hours"] < elapsed_hours

    def test_the_episode_duration_equals_the_stamped_value(
        self, never_released_alert
    ) -> None:
        alert, carry, _ = never_released_alert
        hit = UploadHighAnomalyDetail(
            score=1.0,
            timestamp=alert["timestamp"],
            detector="stuck_fault",
            column=FROZEN_CHANNEL,
            severity="urgent",
            method="wallclock_stuck",
            value=FROZEN_VALUE,
            z_score=None,
            frozen_hours_at_detection=alert["duration_hours"],
        )
        episodes = group_alerts_into_episodes(
            [hit], completed_faults=carry.completed_faults
        )
        assert len(episodes) == 1
        assert episodes[0].duration_seconds > 0.0
        assert episodes[0].duration_seconds == pytest.approx(
            alert["duration_hours"] * SECONDS_PER_HOUR
        )

    def test_the_episode_is_marked_ongoing(self, never_released_alert) -> None:
        """The flag is what tells a reader the duration is a lower bound."""
        alert, carry, _ = never_released_alert
        hit = UploadHighAnomalyDetail(
            score=1.0,
            timestamp=alert["timestamp"],
            detector="stuck_fault",
            column=FROZEN_CHANNEL,
            severity="urgent",
            z_score=None,
            frozen_hours_at_detection=alert["duration_hours"],
        )
        episodes = group_alerts_into_episodes(
            [hit], completed_faults=carry.completed_faults
        )
        assert episodes[0].is_ongoing is True


# ---------------------------------------------------------------------------
# 11 — fixture A takes the completed-window strategy, not the fallback
# ---------------------------------------------------------------------------

class TestFixtureAUsesCompletedFaultWindows:
    """Spec 11, stated as what it is rather than as a lower-bound check.

    All five freeze runs in fixture A end inside the fixture window, so every
    stuck episode there matches a completed ``FaultWindow`` and reports an
    exact span with ``is_ongoing=False``. The changed fallback is therefore
    unreachable on this fixture — which is why "unchanged vs main" here is
    expected rather than reassuring, and why the fallback needs the synthetic
    cases above and in ``test_episode_duration_fallback.py``.
    """

    @pytest.fixture(scope="class")
    def stuck_episodes(self) -> list:
        response = fixture_a_response(REFERENCE_CHUNK_SIZE)
        return sorted(
            (
                episode for episode in (response.episodes or [])
                if episode.detector == "stuck_fault"
            ),
            key=lambda episode: episode.start_ts,
        )

    def test_there_are_four_stuck_episodes(self, stuck_episodes) -> None:
        assert len(stuck_episodes) == len(EXPECTED_EPISODE_HOURS)

    def test_no_stuck_episode_is_ongoing(self, stuck_episodes) -> None:
        """The premise: not one of them reaches the fallback."""
        assert [episode.is_ongoing for episode in stuck_episodes] == [False] * 4

    def test_each_duration_is_the_freeze_span_measured_from_the_csv(
        self, stuck_episodes
    ) -> None:
        """Checked against NATIVE_FIXTURES.md, not against this code's output.

        Those hours were measured from the CSV directly, so agreement here is
        independent corroboration rather than a self-referential pin.
        """
        measured = {
            str(episode.start_ts): round(
                episode.duration_seconds / SECONDS_PER_HOUR, 2
            )
            for episode in stuck_episodes
        }
        assert measured == EXPECTED_EPISODE_HOURS

    def test_no_duration_collapsed_to_the_gate_value(self, stuck_episodes) -> None:
        """Non-vacuity: every span exceeds 6 h, so the fallback is visibly unused.

        If a duration ever equalled exactly 6 h here, the episode would have
        silently switched to the gate-crossing lower bound.
        """
        assert all(
            episode.duration_seconds > GATE_HOURS * SECONDS_PER_HOUR
            for episode in stuck_episodes
        )

    def test_the_sub_gate_freeze_run_has_no_episode(self, stuck_episodes) -> None:
        """Run #4 (1.00 h) never alerted, so it can produce no episode."""
        assert "2026-12-18T11:30:00+00:00" not in {
            str(episode.start_ts) for episode in stuck_episodes
        }
