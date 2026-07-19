"""Comprehensive tests for ChannelMerger.

Covers: priority conflict + tie last-write-wins; partial-channel buckets;
provenance map correctness; late-drop counter; grace watermark boundary;
health transitions online->degraded->offline->online each fire exactly once.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from src.live.events import LiveReadingEvent, SourceReading, SourceStatusEvent
from src.live.merge import ChannelMerger, _DEGRADED_MULTIPLIER, _OFFLINE_MULTIPLIER
from src.live.sources.base import SourceConfig

# Bucket epoch offset: place buckets far enough in the past that grace period is satisfied.
_BUCKET_AGE_SECONDS: float = 3.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _past_bucket_epoch(emit_interval: float) -> float:
    now = time.time()
    bucket_idx = int((now - _BUCKET_AGE_SECONDS) // emit_interval)
    return bucket_idx * emit_interval


def _source_configs(
    poll_interval: float = 0.01,
) -> list[SourceConfig]:
    return [
        SourceConfig(
            source_id="climate",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["temperature", "humidity"],
            priority=10,
            poll_interval_seconds=poll_interval,
        ),
        SourceConfig(
            source_id="co2",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["co2", "temperature"],
            priority=20,
            poll_interval_seconds=poll_interval,
        ),
    ]


def _reading(
    source_id: str,
    channels: dict[str, float],
    ts: datetime,
) -> SourceReading:
    return SourceReading(
        source_id=source_id,
        vendor="mock",
        tenant_id="t1",
        greenhouse_id="gh_A",
        timestamp=ts,
        channels=channels,
    )


async def _run_merger_briefly(merger: ChannelMerger, duration: float = 0.2) -> None:
    task = asyncio.create_task(merger.run())
    await asyncio.sleep(duration)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _make_merger(
    emit_interval: float = 0.05,
    source_configs: list[SourceConfig] | None = None,
    on_reading=None,
    on_status=None,
) -> tuple[ChannelMerger, list[LiveReadingEvent], list[SourceStatusEvent]]:
    emitted: list[LiveReadingEvent] = []
    statuses: list[SourceStatusEvent] = []

    async def default_reading(ev: LiveReadingEvent) -> None:
        emitted.append(ev)

    async def default_status(ev: SourceStatusEvent) -> None:
        statuses.append(ev)

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=emit_interval,
        source_configs=source_configs or _source_configs(),
        on_reading=on_reading or default_reading,
        on_status=on_status or default_status,
    )
    return merger, emitted, statuses


# ---------------------------------------------------------------------------
# Priority conflict resolution
# ---------------------------------------------------------------------------


class TestPriorityConflict:

    @pytest.mark.asyncio
    async def test_lower_priority_number_wins(self) -> None:
        """When two sources report the same channel, lowest priority wins."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        ts = _ts(bucket_epoch + 0.01)

        await merger.submit(_reading("climate", {"temperature": 22.0, "humidity": 65.0}, ts))
        await merger.submit(_reading("co2", {"temperature": 25.0, "co2": 400.0}, ts))

        await _run_merger_briefly(merger)

        assert len(emitted) >= 1
        ev = emitted[0]
        assert ev.channels["temperature"] == 22.0
        assert ev.sources["temperature"] == "climate"
        assert ev.channels["co2"] == 400.0
        assert ev.sources["co2"] == "co2"

    @pytest.mark.asyncio
    async def test_higher_priority_number_loses(self) -> None:
        """Higher priority number loses — value not overwritten."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        ts = _ts(bucket_epoch + 0.01)

        # Higher priority writes first, lower priority writes after
        await merger.submit(_reading("co2", {"temperature": 30.0}, ts))
        await merger.submit(_reading("climate", {"temperature": 22.0}, ts))

        await _run_merger_briefly(merger)

        assert len(emitted) >= 1
        assert emitted[0].channels["temperature"] == 22.0


class TestTieLastWriteWins:

    @pytest.mark.asyncio
    async def test_equal_priority_last_write_wins(self) -> None:
        """Equal priority: last-written value and source win."""
        configs = [
            SourceConfig(
                source_id="src_a", vendor="mock", tenant_id="t1",
                greenhouse_id="gh_A", channels=["temperature"],
                priority=10, poll_interval_seconds=0.01,
            ),
            SourceConfig(
                source_id="src_b", vendor="mock", tenant_id="t1",
                greenhouse_id="gh_A", channels=["temperature"],
                priority=10, poll_interval_seconds=0.01,
            ),
        ]
        merger, emitted, _ = _make_merger(source_configs=configs)
        bucket_epoch = _past_bucket_epoch(0.05)
        ts = _ts(bucket_epoch + 0.01)

        await merger.submit(_reading("src_a", {"temperature": 20.0}, ts))
        await merger.submit(_reading("src_b", {"temperature": 30.0}, ts))

        await _run_merger_briefly(merger)

        assert len(emitted) >= 1
        assert emitted[0].channels["temperature"] == 30.0
        assert emitted[0].sources["temperature"] == "src_b"


# ---------------------------------------------------------------------------
# Partial-channel buckets
# ---------------------------------------------------------------------------


class TestPartialChannelBuckets:

    @pytest.mark.asyncio
    async def test_partial_channels_emitted(self) -> None:
        """Bucket with only some channels still emits."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        ts = _ts(bucket_epoch + 0.01)

        await merger.submit(_reading("climate", {"humidity": 70.0}, ts))

        await _run_merger_briefly(merger)

        assert len(emitted) >= 1
        assert "humidity" in emitted[0].channels
        assert "temperature" not in emitted[0].channels

    @pytest.mark.asyncio
    async def test_empty_bucket_not_emitted(self) -> None:
        """A bucket with no channel data should not emit."""
        merger, emitted, _ = _make_merger()
        # Don't submit anything
        await _run_merger_briefly(merger, duration=0.15)
        assert len(emitted) == 0


# ---------------------------------------------------------------------------
# Provenance map correctness
# ---------------------------------------------------------------------------


class TestProvenanceMap:

    @pytest.mark.asyncio
    async def test_sources_dict_tracks_winning_source(self) -> None:
        """sources dict shows which source provided each channel value."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        ts = _ts(bucket_epoch + 0.01)

        await merger.submit(_reading("climate", {"temperature": 22.0, "humidity": 65.0}, ts))
        await merger.submit(_reading("co2", {"co2": 400.0}, ts))

        await _run_merger_briefly(merger)

        assert len(emitted) >= 1
        ev = emitted[0]
        assert ev.sources["temperature"] == "climate"
        assert ev.sources["humidity"] == "climate"
        assert ev.sources["co2"] == "co2"

    @pytest.mark.asyncio
    async def test_provenance_updates_on_priority_win(self) -> None:
        """When a higher-priority source overrides, provenance updates."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        ts = _ts(bucket_epoch + 0.01)

        # co2 source (prio 20) writes temperature first
        await merger.submit(_reading("co2", {"temperature": 99.0}, ts))
        # climate source (prio 10) writes temperature second — wins
        await merger.submit(_reading("climate", {"temperature": 22.0}, ts))

        await _run_merger_briefly(merger)

        assert len(emitted) >= 1
        assert emitted[0].sources["temperature"] == "climate"


# ---------------------------------------------------------------------------
# Late-drop counter
# ---------------------------------------------------------------------------


class TestLateDropCounter:

    @pytest.mark.asyncio
    async def test_late_reading_increments_counter(self) -> None:
        """Reading for an already-emitted bucket increments dropped_late_count."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        old_epoch = bucket_epoch - 5.0

        # Submit recent bucket and flush
        await merger.submit(
            _reading("climate", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )
        task = asyncio.create_task(merger.run())
        await asyncio.sleep(0.15)

        initial_emitted = len(emitted)
        assert initial_emitted >= 1

        # Submit a reading for the old bucket
        await merger.submit(
            _reading("climate", {"temperature": 99.0}, _ts(old_epoch + 0.01))
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert merger.dropped_late_count >= 1

    @pytest.mark.asyncio
    async def test_multiple_late_drops(self) -> None:
        """Multiple late readings accumulate in the counter."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        old_epoch = bucket_epoch - 5.0

        await merger.submit(
            _reading("climate", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )
        task = asyncio.create_task(merger.run())
        await asyncio.sleep(0.15)

        for _ in range(3):
            await merger.submit(
                _reading("climate", {"temperature": 99.0}, _ts(old_epoch + 0.01))
            )

        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert merger.dropped_late_count >= 3


# ---------------------------------------------------------------------------
# Grace watermark boundary
# ---------------------------------------------------------------------------


class TestGraceWatermark:

    @pytest.mark.asyncio
    async def test_bucket_not_emitted_within_grace(self, monkeypatch) -> None:
        """A bucket at current time (within grace) is held until grace expires.

        The cancellation flush emits all remaining buckets, so we verify the
        bucket was NOT emitted by the regular flush path (only by final flush).
        """
        monkeypatch.setattr("src.live.merge.LIVE_MERGE_GRACE_SECONDS", 10.0)

        # Track whether the regular flush or the final-flush emitted
        regular_emitted: list[LiveReadingEvent] = []

        async def on_reading(ev: LiveReadingEvent) -> None:
            regular_emitted.append(ev)

        async def noop_status(ev: SourceStatusEvent) -> None:
            pass

        merger = ChannelMerger(
            tenant_id="t1",
            greenhouse_id="gh_A",
            emit_interval=1.0,
            source_configs=_source_configs(),
            on_reading=on_reading,
            on_status=noop_status,
        )
        now = time.time()
        await merger.submit(
            _reading("climate", {"temperature": 22.0}, _ts(now))
        )

        # Only call _flush_ready_buckets (not cancel which does _flush_all)
        merger._drain_inbox()
        await merger._flush_ready_buckets()
        # Grace is 10s, bucket just started — should not have flushed
        assert len(regular_emitted) == 0

    @pytest.mark.asyncio
    async def test_bucket_emitted_after_grace(self, monkeypatch) -> None:
        """A bucket emits after its grace period has elapsed."""
        monkeypatch.setattr("src.live.merge.LIVE_MERGE_GRACE_SECONDS", 0.0)

        merger, emitted, _ = _make_merger(emit_interval=0.05)
        bucket_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("climate", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )

        await _run_merger_briefly(merger, duration=0.2)
        assert len(emitted) >= 1


# ---------------------------------------------------------------------------
# Non-finite values filtered
# ---------------------------------------------------------------------------


class TestNonFiniteFilter:

    @pytest.mark.asyncio
    async def test_nan_channel_filtered_at_ingest(self) -> None:
        """NaN values are dropped during ingest."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        ts = _ts(bucket_epoch + 0.01)

        # Use model_construct to bypass validation and inject NaN
        reading = SourceReading.model_construct(
            source_id="climate",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            timestamp=ts,
            channels={"temperature": float("nan"), "humidity": 65.0},
        )
        await merger.submit(reading)
        await _run_merger_briefly(merger)

        assert len(emitted) >= 1
        assert "temperature" not in emitted[0].channels
        assert emitted[0].channels["humidity"] == 65.0


# ---------------------------------------------------------------------------
# Health transitions
# ---------------------------------------------------------------------------


class TestHealthTransitions:

    @pytest.mark.asyncio
    async def test_degraded_fires_once(self) -> None:
        """A source going silent fires exactly one degraded transition."""
        configs = [
            SourceConfig(
                source_id="src_fast", vendor="mock", tenant_id="t1",
                greenhouse_id="gh_A", channels=["temperature"],
                priority=10, poll_interval_seconds=0.01,
            ),
        ]
        merger, _, statuses = _make_merger(source_configs=configs)
        bucket_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("src_fast", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )
        await _run_merger_briefly(merger, duration=0.3)

        degraded = [s for s in statuses if s.status == "degraded" and s.source_id == "src_fast"]
        assert len(degraded) == 1

    @pytest.mark.asyncio
    async def test_offline_fires_once(self) -> None:
        """A source going fully silent fires exactly one offline transition."""
        configs = [
            SourceConfig(
                source_id="src_off", vendor="mock", tenant_id="t1",
                greenhouse_id="gh_A", channels=["temperature"],
                priority=10, poll_interval_seconds=0.005,
            ),
        ]
        merger, _, statuses = _make_merger(source_configs=configs)
        bucket_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("src_off", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )
        # Need 10x interval to go offline -> 50ms; run 300ms
        await _run_merger_briefly(merger, duration=0.3)

        offline = [s for s in statuses if s.status == "offline" and s.source_id == "src_off"]
        assert len(offline) == 1

    @pytest.mark.asyncio
    async def test_recovery_online_transition(self) -> None:
        """A source recovering fires an online transition."""
        configs = [
            SourceConfig(
                source_id="src_rec", vendor="mock", tenant_id="t1",
                greenhouse_id="gh_A", channels=["temperature"],
                priority=10, poll_interval_seconds=0.01,
            ),
        ]
        merger, _, statuses = _make_merger(source_configs=configs)
        bucket_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("src_rec", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )

        task = asyncio.create_task(merger.run())
        await asyncio.sleep(0.15)  # go degraded

        # Submit fresh reading to recover
        fresh_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("src_rec", {"temperature": 23.0}, _ts(fresh_epoch + 0.01))
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        src_statuses = [s.status for s in statuses if s.source_id == "src_rec"]
        assert "degraded" in src_statuses
        assert "online" in src_statuses

    @pytest.mark.asyncio
    async def test_full_cycle_online_degraded_offline_online(self) -> None:
        """Full health cycle: online → degraded → offline → online."""
        configs = [
            SourceConfig(
                source_id="src_cycle", vendor="mock", tenant_id="t1",
                greenhouse_id="gh_A", channels=["temperature"],
                priority=10, poll_interval_seconds=0.005,
            ),
        ]
        merger, _, statuses = _make_merger(source_configs=configs)
        bucket_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("src_cycle", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )

        task = asyncio.create_task(merger.run())
        # Wait for degraded (3*5ms=15ms) and offline (10*5ms=50ms) -> 200ms enough
        await asyncio.sleep(0.25)

        # Recovery: submit reading
        fresh_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("src_cycle", {"temperature": 23.0}, _ts(fresh_epoch + 0.01))
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        transitions = [s.status for s in statuses if s.source_id == "src_cycle"]
        assert "degraded" in transitions
        assert "offline" in transitions
        assert "online" in transitions


# ---------------------------------------------------------------------------
# Seq and scope fields
# ---------------------------------------------------------------------------


class TestEmittedEventFields:

    @pytest.mark.asyncio
    async def test_seq_is_empty(self) -> None:
        """Merged events have seq='' — SSE layer fills from Redis."""
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("climate", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )
        await _run_merger_briefly(merger)
        assert len(emitted) >= 1
        assert emitted[0].seq == ""

    @pytest.mark.asyncio
    async def test_scope_fields_correct(self) -> None:
        merger, emitted, _ = _make_merger()
        bucket_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("climate", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )
        await _run_merger_briefly(merger)
        assert len(emitted) >= 1
        assert emitted[0].tenant_id == "t1"
        assert emitted[0].greenhouse_id == "gh_A"


# ---------------------------------------------------------------------------
# Cancellation (final flush)
# ---------------------------------------------------------------------------


class TestCancellationFlush:

    @pytest.mark.asyncio
    async def test_cancel_flushes_remaining_buckets(self, monkeypatch) -> None:
        """On cancellation, remaining buckets are flushed."""
        monkeypatch.setattr("src.live.merge.LIVE_MERGE_GRACE_SECONDS", 999.0)
        merger, emitted, _ = _make_merger(emit_interval=0.05)
        bucket_epoch = _past_bucket_epoch(0.05)
        await merger.submit(
            _reading("climate", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
        )

        task = asyncio.create_task(merger.run())
        await asyncio.sleep(0.05)
        # Nothing should have been emitted yet due to grace
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # After cancel, the flush-all should have emitted it
        assert len(emitted) >= 1
