"""The 6 h same-channel post-release window, unchanged by Task 5.

Two mechanisms meet after a stuck fault releases, and Task 5 touched only
one of them:

* **Same channel** — a statistical hit on the FAULTED channel inside
  ``[onset, release + COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS]`` is
  dropped at upload time by ``_suppress_fault_window_hits`` and never
  flagged, persisted, or grouped at all.  Pre-existing; this module pins
  it against the Task 5 change.
* **Another channel** — kept, and now TAGGED ``is_recovery`` at query
  time if it starts inside the knock-on window
  (``test_recovery_tag_window.py``).

The suppression window is the longer of the two (6 h vs 4 h), which is
why the faulted channel never produces an episode for the tagging to
consider either way, and why the old same-channel reading of
``is_recovery`` had become unreachable before Task 5 repurposed it.

``tests/api/test_upload_portions_onset_fired.py`` covers the OPEN-freeze
branch of the same function; the completed-fault branch exercised here
had no coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.api.schemas.sensor_data import StatisticalAnomalyDetail
from src.api.services.upload_portions import _suppress_fault_window_hits
from src.config import COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS
from src.models.stuck_fault_detector import FaultWindow, StuckCarry

FAULT_CHANNEL = "humidity"
OTHER_CHANNEL = "temperature"
FROZEN_VALUE = 100.0

ONSET = datetime(2026, 12, 18, 13, 0, tzinfo=timezone.utc)
LAST_FROZEN = datetime(2026, 12, 25, 8, 50, tzinfo=timezone.utc)
RELEASE = datetime(2026, 12, 25, 8, 55, tzinfo=timezone.utc)
SUPPRESS_WINDOW = timedelta(hours=COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS)
SAMPLE_SPACING = timedelta(minutes=5)


def _released_fault_carry() -> StuckCarry:
    """A carry holding one completed, released freeze on ``humidity``."""
    return StuckCarry(
        completed_faults=[
            FaultWindow(
                channel=FAULT_CHANNEL,
                onset_ts=ONSET.isoformat(),
                onset_dt=ONSET,
                release_ts=RELEASE.isoformat(),
                release_dt=RELEASE,
                last_frozen_ts=LAST_FROZEN.isoformat(),
                last_frozen_dt=LAST_FROZEN,
                frozen_sample_count=1979,
                raw_slot_count=1979,
                has_fired=True,
            )
        ]
    )


def _hit(when: datetime, *, column: str = FAULT_CHANNEL) -> StatisticalAnomalyDetail:
    return StatisticalAnomalyDetail(
        timestamp=when.isoformat(),
        column=column,
        value=FROZEN_VALUE,
        z_score=7.5,
        severity="high",
        method="zscore",
    )


def _survivors(*hits: StatisticalAnomalyDetail) -> list[StatisticalAnomalyDetail]:
    return _suppress_fault_window_hits(list(hits), _released_fault_carry())


class TestFaultedChannelAfterRelease:
    """The faulted channel stays silent for 6 h after it releases."""

    def test_a_hit_an_hour_after_release_is_dropped(self):
        assert _survivors(_hit(RELEASE + timedelta(hours=1))) == []

    def test_a_hit_exactly_at_the_end_of_the_window_is_dropped(self):
        """The bound is inclusive — ``hit <= release + N``."""
        assert _survivors(_hit(RELEASE + SUPPRESS_WINDOW)) == []

    def test_a_hit_one_sample_past_the_window_survives(self):
        """Pairs with the test above to pin which side the bound falls on."""
        hit = _hit(RELEASE + SUPPRESS_WINDOW + SAMPLE_SPACING)
        assert _survivors(hit) == [hit]

    def test_a_hit_before_the_onset_survives(self):
        """The window runs forward from onset, not backward."""
        hit = _hit(ONSET - timedelta(hours=1))
        assert _survivors(hit) == [hit]


class TestOtherChannelsAreUntouched:
    """Suppression is by exact column — this is what leaves tagging a job."""

    def test_another_channel_inside_the_window_survives(self):
        """The hit Task 5 tags instead of suppressing.

        Same instant as the dropped faulted-channel hit above, a
        different column: it reaches the grouper, becomes an episode, and
        the query path marks it ``is_recovery``.
        """
        hit = _hit(RELEASE + timedelta(hours=1), column=OTHER_CHANNEL)
        assert _survivors(hit) == [hit]

    def test_both_rules_apply_to_one_batch_independently(self):
        """One timestamp, two channels, two different outcomes."""
        when = RELEASE + timedelta(hours=1)
        suppressed = _hit(when)
        kept = _hit(when, column=OTHER_CHANNEL)
        assert _survivors(suppressed, kept) == [kept]


class TestTheWindowIsLongerThanTheTaggingWindow:
    def test_suppression_outlasts_the_knock_on_window(self):
        """Why no faulted-channel episode ever reaches the tagging step.

        Not a tuning assertion — it is the structural reason the old
        same-channel meaning of ``is_recovery`` was unreachable, recorded
        so that shortening one constant below the other surfaces here
        instead of quietly resurrecting that case.
        """
        from src.config import RECOVERY_KNOCKON_WINDOW_HOURS

        assert (
            COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS > RECOVERY_KNOCKON_WINDOW_HOURS
        )
