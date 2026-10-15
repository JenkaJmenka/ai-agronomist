"""Task 5: when ``is_recovery`` is set, and what it must never change.

``is_recovery`` was repurposed from a same-channel score cap into
cross-channel tagging.  The contract
(``docs/handoff/task5_spec.md``, ``AnomalyEpisodeSummary.is_recovery``):

    True when this episode is on a channel other than a stuck-fault
    channel and starts no earlier than ``EPISODE_GAP_SECONDS`` before, and
    within ``RECOVERY_KNOCKON_WINDOW_HOURS`` after, that fault's release.
    Informational only; does not suppress or rescore.

Two properties of that sentence carry the design and are each pinned
below with a case that fails under the alternative:

* **start-anchored, not overlap-based.**  A long excursion that began well
  before the fault and merely happens to still be running when it releases
  is not a response to the release.  ``TestStartAnchoring`` builds exactly
  that episode — spanning the release, starting long before it — and
  requires it untagged.  Overlap-based tagging would tag it.
* **informational only.**  ``TestTaggingDoesNotRescore`` requires peak,
  count and rank to read the same whether an episode is tagged or not.
  The deleted ``RECOVERY_SCORE_CAP`` clamped tagged peaks to 0.4; the
  scores here are 0.9, so that clamp returning fails this class.

Harness: ``tests/api/_recovery_tag_fixtures.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.config import EPISODE_GAP_SECONDS, RECOVERY_KNOCKON_WINDOW_HOURS
from tests.api._recovery_tag_fixtures import (
    FAULT_CHANNEL,
    OTHER_CHANNEL,
    RELEASE,
    SAMPLE_SPACING,
    STUCK_DETECTOR,
    add_flagged_run,
    add_released_fault,
    query_episodes,
    recovery_tag_db,
    single_episode,
)

GRACE = timedelta(seconds=EPISODE_GAP_SECONDS)
WINDOW = timedelta(hours=RECOVERY_KNOCKON_WINDOW_HOURS)


def _instant(timestamp: str) -> datetime:
    """Parse an episode timestamp to a UTC instant.

    Compared as instants, not strings, so a formatting change on the
    service side can never read as a window result.
    """
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TestInsideTheWindow:
    """A downstream excursion around a release is tagged."""

    async def test_hit_shortly_after_a_release_is_tagged(self):
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            await add_flagged_run(
                session, first_ts=RELEASE + timedelta(minutes=15), sample_count=4
            )
            assert (await single_episode(session))["is_recovery"] is True

    async def test_hit_exactly_at_the_window_end_is_tagged(self):
        """The forward bound is inclusive: ``start - release <= window``."""
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            await add_flagged_run(session, first_ts=RELEASE + WINDOW, sample_count=4)
            assert (await single_episode(session))["is_recovery"] is True

    async def test_hit_beyond_the_window_end_is_not_tagged(self):
        """One sample past the bound and the excursion is its own event."""
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            await add_flagged_run(
                session,
                first_ts=RELEASE + WINDOW + SAMPLE_SPACING,
                sample_count=4,
            )
            assert (await single_episode(session))["is_recovery"] is False


class TestBackwardResolutionBound:
    """``release_ts`` lags the true change, so a little earlier still counts.

    ``release_ts`` is the first non-frozen sample after the freeze ends
    (``AnomalyEpisodeRecord``), so it can trail the real recovery by up to
    one sample.  ``EPISODE_GAP_SECONDS`` is the pipeline's own definition
    of that resolution and is reused here rather than a tuned constant.
    """

    async def test_hit_exactly_the_gap_before_a_release_is_tagged(self):
        """The backward bound is inclusive at exactly ``-EPISODE_GAP_SECONDS``."""
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            await add_flagged_run(session, first_ts=RELEASE - GRACE, sample_count=2)
            assert (await single_episode(session))["is_recovery"] is True

    async def test_hit_one_second_earlier_than_the_gap_is_not_tagged(self):
        """One second past the resolution bound is outside it.

        Pairs with the test above: together they pin WHICH side of
        ``-EPISODE_GAP_SECONDS`` the boundary falls on, which neither
        shows alone.
        """
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            await add_flagged_run(
                session,
                first_ts=RELEASE - GRACE - timedelta(seconds=1),
                sample_count=2,
            )
            assert (await single_episode(session))["is_recovery"] is False


class TestStartAnchoring:
    """Tagging anchors on the episode's start, never on its span."""

    async def test_episode_starting_long_before_a_release_and_spanning_it_is_not_tagged(
        self,
    ):
        """The case that separates start-anchoring from overlap-based tagging.

        This excursion is underway half an hour before the fault releases
        and continues well past it, so its span overlaps the release while
        its start sits far outside the backward bound.  It is a
        pre-existing event the release did not cause, and tagging it would
        attribute someone else's excursion to the recovery.
        """
        start = RELEASE - timedelta(minutes=30)
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            # 5-minute samples from 30 min before the release to 25 after:
            # one contiguous episode straddling it.
            await add_flagged_run(session, first_ts=start, sample_count=12)
            episode = await single_episode(session)
            assert _instant(episode["start_ts"]) < RELEASE
            assert _instant(episode["end_ts"]) > RELEASE
            assert episode["is_recovery"] is False


class TestCrossChannelRequirement:
    """Only a channel OTHER than the faulted one is ever tagged."""

    async def test_hit_on_the_faulted_channel_is_not_tagged(self):
        """Same-channel is the old meaning of the field, now the excluded one."""
        async with recovery_tag_db() as session:
            await add_released_fault(session, channel=FAULT_CHANNEL)
            await add_flagged_run(
                session,
                first_ts=RELEASE + timedelta(minutes=15),
                sample_count=4,
                channel=FAULT_CHANNEL,
            )
            assert (await single_episode(session))["is_recovery"] is False

    async def test_a_stuck_fault_episode_is_never_tagged(self):
        """A fault IS the faulted channel; it cannot be its own knock-on."""
        async with recovery_tag_db() as session:
            await add_released_fault(session, channel=OTHER_CHANNEL)
            await add_flagged_run(
                session,
                first_ts=RELEASE + timedelta(minutes=15),
                sample_count=1,
                channel=FAULT_CHANNEL,
                detector=STUCK_DETECTOR,
            )
            assert (await single_episode(session))["is_recovery"] is False


class TestUnreleasedFault:
    """An open freeze has not released, so it cannot tag anything."""

    async def test_an_ongoing_fault_tags_nothing(self):
        async with recovery_tag_db() as session:
            await add_released_fault(session, release=None)
            await add_flagged_run(
                session, first_ts=RELEASE + timedelta(minutes=15), sample_count=4
            )
            assert (await single_episode(session))["is_recovery"] is False

    async def test_no_fault_at_all_tags_nothing(self):
        """Control: the same excursion with no fault in the table."""
        async with recovery_tag_db() as session:
            await add_flagged_run(
                session, first_ts=RELEASE + timedelta(minutes=15), sample_count=4
            )
            assert (await single_episode(session))["is_recovery"] is False


class TestTaggingDoesNotRescore:
    """Informational only: tagging changes no score, no count, no rank."""

    async def test_a_tagged_episode_keeps_the_peak_score_of_its_rows(self):
        """0.9 survives; the deleted ``RECOVERY_SCORE_CAP`` was 0.4."""
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            await add_flagged_run(
                session,
                first_ts=RELEASE + timedelta(minutes=15),
                sample_count=4,
                score=0.9,
            )
            episode = await single_episode(session)
            assert episode["is_recovery"] is True
            assert episode["peak_score"] == 0.9

    async def test_a_tagged_episode_keeps_every_one_of_its_samples(self):
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            await add_flagged_run(
                session, first_ts=RELEASE + timedelta(minutes=15), sample_count=4
            )
            episode = await single_episode(session)
            assert episode["is_recovery"] is True
            assert episode["sample_count"] == 4

    async def test_a_tagged_episode_still_outranks_a_lower_scoring_untagged_one(self):
        """Ranking is by peak alone — the tag must not demote the louder event.

        Under the deleted cap the tagged 0.9 would have been clamped to
        0.4 and the untagged 0.6 would have overtaken it.
        """
        async with recovery_tag_db() as session:
            await add_released_fault(session)
            await add_flagged_run(
                session,
                first_ts=RELEASE + timedelta(minutes=15),
                sample_count=4,
                score=0.9,
            )
            # Days later, far outside the window: untagged, and quieter.
            await add_flagged_run(
                session,
                first_ts=RELEASE + timedelta(days=1),
                sample_count=4,
                score=0.6,
            )
            episodes = await query_episodes(session)
            assert [ep["is_recovery"] for ep in episodes] == [True, False]
            tagged, untagged = episodes
            assert tagged["peak_score"] > untagged["peak_score"]
