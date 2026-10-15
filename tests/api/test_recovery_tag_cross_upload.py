"""Task 5: a release from one upload tags a hit from a later one.

This is why the tagging is computed at query time and not inside
``src/models/episode_grouper.py``.  The detection-time grouper's only
source for "which channel just released" is ``stuck_carry``, which is
in-memory state scoped to a single upload — it cannot see a fault that
released while a previous file was being processed.  The query path reads
the persisted ``anomaly_episodes`` table instead, which outlives any one
upload (``docs/handoff/task5_spec.md`` §2).

The two writes below share nothing but the database: separate sessions,
separate transactions, no carry object and no Python state passed between
them.  That is the structural property under test — if the tagging ever
moved back onto per-upload state, the second writer's episode would come
back untagged however close it sat to the release.

Harness: ``tests/api/_recovery_tag_fixtures.py``.
"""

from __future__ import annotations

from datetime import timedelta

from tests.api._recovery_tag_fixtures import (
    RELEASE,
    add_flagged_run,
    add_released_fault,
    query_episodes,
    recovery_tag_engine,
    session_for,
    single_episode,
)

KNOCK_ON_START = RELEASE + timedelta(minutes=20)


class TestCrossUploadTagging:
    async def test_a_release_persisted_earlier_tags_a_hit_written_later(self):
        """Fault written by one session, hit by another, tagged by the query."""
        async with recovery_tag_engine() as engine:
            async with session_for(engine) as earlier_upload:
                await add_released_fault(earlier_upload)
            async with session_for(engine) as later_upload:
                await add_flagged_run(
                    later_upload, first_ts=KNOCK_ON_START, sample_count=4
                )
            async with session_for(engine) as reader:
                assert (await single_episode(reader))["is_recovery"] is True

    async def test_the_same_hit_is_untagged_when_the_earlier_upload_is_missing(self):
        """The discriminator: without the persisted release, nothing tags it.

        Identical to the test above with the first session removed, so a
        True there cannot come from anything but the cross-upload lookup.
        """
        async with recovery_tag_engine() as engine:
            async with session_for(engine) as later_upload:
                await add_flagged_run(
                    later_upload, first_ts=KNOCK_ON_START, sample_count=4
                )
            async with session_for(engine) as reader:
                assert (await single_episode(reader))["is_recovery"] is False

    async def test_the_tag_is_recomputed_per_query_not_frozen_at_write_time(self):
        """A release arriving AFTER the hit was written still tags it.

        The reverse order of the first test: the excursion is persisted
        first and reads untagged, then the fault's release lands, and the
        very same episode reads tagged.  Nothing rewrote the stored row —
        the field has no column; it is derived on every request.
        """
        async with recovery_tag_engine() as engine:
            async with session_for(engine) as later_upload:
                await add_flagged_run(
                    later_upload, first_ts=KNOCK_ON_START, sample_count=4
                )
            async with session_for(engine) as reader:
                before = await single_episode(reader)
                assert before["is_recovery"] is False

            async with session_for(engine) as backfilled_upload:
                await add_released_fault(backfilled_upload)
            async with session_for(engine) as reader:
                after = await single_episode(reader)

            assert after["is_recovery"] is True
            assert after["start_ts"] == before["start_ts"]
            assert after["peak_score"] == before["peak_score"]
            assert after["sample_count"] == before["sample_count"]


class TestCrossUploadLeavesOtherEpisodesAlone:
    async def test_only_the_episode_inside_the_window_is_tagged(self):
        """A second, distant excursion from the later upload stays untagged."""
        async with recovery_tag_engine() as engine:
            async with session_for(engine) as earlier_upload:
                await add_released_fault(earlier_upload)
            async with session_for(engine) as later_upload:
                await add_flagged_run(
                    later_upload, first_ts=KNOCK_ON_START, sample_count=4
                )
                await add_flagged_run(
                    later_upload,
                    first_ts=RELEASE + timedelta(days=1),
                    sample_count=4,
                )
            async with session_for(engine) as reader:
                episodes = await query_episodes(reader)
            assert [ep["is_recovery"] for ep in episodes] == [True, False]
