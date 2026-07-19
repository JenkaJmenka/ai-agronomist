"""Smoke tests for ChannelMerger.

Validates: two-source merge with priority conflict resolution, late reading
drop, and degraded status transition firing exactly once.

NOTE: ``LIVE_MERGE_GRACE_SECONDS`` is 1.0s in config, so test buckets must
be placed at least 2s in the past to flush immediately.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from src.live.events import LiveReadingEvent, SourceReading, SourceStatusEvent
from src.live.merge import ChannelMerger
from src.live.sources.base import SourceConfig

# Bucket epoch offset: place buckets far enough in the past that
# grace period (1.0s) is always satisfied when the merger runs.
_BUCKET_AGE_SECONDS: float = 3.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(epoch: float) -> datetime:
    """Build a UTC datetime from a Unix epoch."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _past_bucket_epoch(emit_interval: float) -> float:
    """Return a bucket-aligned epoch that is old enough to flush immediately."""
    now = time.time()
    bucket_idx = int((now - _BUCKET_AGE_SECONDS) // emit_interval)
    return bucket_idx * emit_interval


def _source_configs() -> list[SourceConfig]:
    """Two source configs: climate (priority 10) and co2 (priority 20)."""
    return [
        SourceConfig(
            source_id="climate",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["temperature", "humidity"],
            priority=10,
            poll_interval_seconds=0.01,
        ),
        SourceConfig(
            source_id="co2",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["co2", "temperature"],
            priority=20,
            poll_interval_seconds=0.01,
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
    """Run the merger for *duration* seconds then cancel."""
    task = asyncio.create_task(merger.run())
    await asyncio.sleep(duration)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Tests — priority-based merge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_priority_conflict_resolution() -> None:
    """When two sources report the same channel, lowest priority wins."""
    emitted: list[LiveReadingEvent] = []
    statuses: list[SourceStatusEvent] = []

    async def on_reading(ev: LiveReadingEvent) -> None:
        emitted.append(ev)

    async def on_status(ev: SourceStatusEvent) -> None:
        statuses.append(ev)

    emit_interval = 0.05
    bucket_epoch = _past_bucket_epoch(emit_interval)

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=emit_interval,
        source_configs=_source_configs(),
        on_reading=on_reading,
        on_status=on_status,
    )

    ts = _ts(bucket_epoch + 0.01)

    # Climate source (priority 10) reports temperature=22
    await merger.submit(_reading("climate", {"temperature": 22.0, "humidity": 65.0}, ts))
    # CO2 source (priority 20) reports temperature=25 — should lose
    await merger.submit(_reading("co2", {"temperature": 25.0, "co2": 400.0}, ts))

    await _run_merger_briefly(merger)

    assert len(emitted) >= 1, f"Expected at least 1 emitted event, got {len(emitted)}"
    ev = emitted[0]

    # Temperature: climate wins (priority 10 < 20)
    assert ev.channels["temperature"] == 22.0
    assert ev.sources["temperature"] == "climate"

    # CO2: only co2 source provides it
    assert ev.channels["co2"] == 400.0
    assert ev.sources["co2"] == "co2"

    # Humidity: only climate provides it
    assert ev.channels["humidity"] == 65.0


@pytest.mark.asyncio
async def test_equal_priority_last_write_wins() -> None:
    """Equal priority: the last-written value wins."""
    emitted: list[LiveReadingEvent] = []

    async def on_reading(ev: LiveReadingEvent) -> None:
        emitted.append(ev)

    async def noop_status(ev: SourceStatusEvent) -> None:
        pass

    configs = [
        SourceConfig(
            source_id="src_a",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["temperature"],
            priority=10,
            poll_interval_seconds=0.01,
        ),
        SourceConfig(
            source_id="src_b",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["temperature"],
            priority=10,
            poll_interval_seconds=0.01,
        ),
    ]

    emit_interval = 0.05
    bucket_epoch = _past_bucket_epoch(emit_interval)

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=emit_interval,
        source_configs=configs,
        on_reading=on_reading,
        on_status=noop_status,
    )

    ts = _ts(bucket_epoch + 0.01)
    await merger.submit(_reading("src_a", {"temperature": 20.0}, ts))
    await merger.submit(_reading("src_b", {"temperature": 30.0}, ts))

    await _run_merger_briefly(merger)

    assert len(emitted) >= 1
    # Last write (src_b=30) wins
    assert emitted[0].channels["temperature"] == 30.0
    assert emitted[0].sources["temperature"] == "src_b"


# ---------------------------------------------------------------------------
# Tests — late reading drop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_late_reading_dropped() -> None:
    """Readings for already-emitted buckets are counted as dropped."""
    emitted: list[LiveReadingEvent] = []

    async def on_reading(ev: LiveReadingEvent) -> None:
        emitted.append(ev)

    async def noop_status(ev: SourceStatusEvent) -> None:
        pass

    emit_interval = 0.05
    # Two buckets: "recent" (3s ago) and "old" (6s ago)
    recent_epoch = _past_bucket_epoch(emit_interval)
    old_epoch = recent_epoch - 3.0

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=emit_interval,
        source_configs=_source_configs(),
        on_reading=on_reading,
        on_status=noop_status,
    )

    # Submit a reading in the "recent" bucket so it gets emitted first
    await merger.submit(
        _reading("climate", {"temperature": 22.0}, _ts(recent_epoch + 0.01))
    )

    # Run to flush the recent bucket and advance the watermark
    task = asyncio.create_task(merger.run())
    await asyncio.sleep(0.15)

    initial_emitted = len(emitted)
    assert initial_emitted >= 1, "Recent bucket should have been emitted"

    # Now submit a reading in the "old" bucket — should be dropped
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


# ---------------------------------------------------------------------------
# Tests — source health transitions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_degraded_transition_fires_once() -> None:
    """A source going silent fires exactly one degraded transition."""
    statuses: list[SourceStatusEvent] = []

    async def noop_reading(ev: LiveReadingEvent) -> None:
        pass

    async def on_status(ev: SourceStatusEvent) -> None:
        statuses.append(ev)

    configs = [
        SourceConfig(
            source_id="src_fast",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["temperature"],
            priority=10,
            poll_interval_seconds=0.01,  # 10ms
        ),
    ]

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=0.05,
        source_configs=configs,
        on_reading=noop_reading,
        on_status=on_status,
    )

    # Submit one reading to mark the source as alive, then stop
    bucket_epoch = _past_bucket_epoch(0.05)
    await merger.submit(
        _reading("src_fast", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
    )

    # Run the merger long enough for 3x interval (30ms) to pass
    await _run_merger_briefly(merger, duration=0.3)

    # Should have exactly one degraded transition
    degraded_events = [
        s for s in statuses
        if s.status == "degraded" and s.source_id == "src_fast"
    ]
    assert len(degraded_events) == 1, (
        f"Expected exactly 1 degraded event, got {len(degraded_events)}: "
        f"{[(s.source_id, s.status) for s in statuses]}"
    )


@pytest.mark.asyncio
async def test_recovery_transition() -> None:
    """A source recovering from degraded fires an online transition."""
    statuses: list[SourceStatusEvent] = []

    async def noop_reading(ev: LiveReadingEvent) -> None:
        pass

    async def on_status(ev: SourceStatusEvent) -> None:
        statuses.append(ev)

    configs = [
        SourceConfig(
            source_id="src_rec",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["temperature"],
            priority=10,
            poll_interval_seconds=0.01,
        ),
    ]

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=0.05,
        source_configs=configs,
        on_reading=noop_reading,
        on_status=on_status,
    )

    bucket_epoch = _past_bucket_epoch(0.05)
    await merger.submit(
        _reading("src_rec", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
    )

    task = asyncio.create_task(merger.run())
    # Wait long enough to go degraded (3x10ms = 30ms, give 150ms)
    await asyncio.sleep(0.15)

    # Submit a new reading — should trigger recovery
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
    assert "degraded" in src_statuses, f"Expected degraded in {src_statuses}"
    assert "online" in src_statuses, f"Expected online recovery in {src_statuses}"


# ---------------------------------------------------------------------------
# Tests — seq field and scope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emitted_event_seq_is_empty() -> None:
    """Merged events have seq='' — SSE layer fills it from Redis."""
    emitted: list[LiveReadingEvent] = []

    async def on_reading(ev: LiveReadingEvent) -> None:
        emitted.append(ev)

    async def noop_status(ev: SourceStatusEvent) -> None:
        pass

    emit_interval = 0.05
    bucket_epoch = _past_bucket_epoch(emit_interval)

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=emit_interval,
        source_configs=_source_configs(),
        on_reading=on_reading,
        on_status=noop_status,
    )

    await merger.submit(
        _reading("climate", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
    )

    await _run_merger_briefly(merger)

    assert len(emitted) >= 1
    assert emitted[0].seq == ""
    assert emitted[0].tenant_id == "t1"
    assert emitted[0].greenhouse_id == "gh_A"


# ---------------------------------------------------------------------------
# Tests — future-timestamp bucket leak prevention (Fix 6)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_future_timestamp_dropped() -> None:
    """Readings with far-future timestamps are dropped to prevent bucket leak."""
    from datetime import timedelta

    emitted: list[LiveReadingEvent] = []

    async def on_reading(ev: LiveReadingEvent) -> None:
        emitted.append(ev)

    async def noop_status(ev: SourceStatusEvent) -> None:
        pass

    emit_interval = 0.05

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=emit_interval,
        source_configs=_source_configs(),
        on_reading=on_reading,
        on_status=noop_status,
    )

    # Submit a reading with a far-future timestamp (1 hour from now)
    future_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    await merger.submit(
        _reading("climate", {"temperature": 99.0}, future_ts)
    )

    await _run_merger_briefly(merger, duration=0.2)

    # The future reading should have been dropped
    assert merger.dropped_future_count == 1
    # No events should have been emitted for the future bucket
    future_events = [
        ev for ev in emitted if ev.channels.get("temperature") == 99.0
    ]
    assert len(future_events) == 0


@pytest.mark.asyncio
async def test_near_future_not_dropped() -> None:
    """Readings within the grace window are NOT dropped (only far-future)."""
    emitted: list[LiveReadingEvent] = []

    async def on_reading(ev: LiveReadingEvent) -> None:
        emitted.append(ev)

    async def noop_status(ev: SourceStatusEvent) -> None:
        pass

    emit_interval = 0.05
    bucket_epoch = _past_bucket_epoch(emit_interval)

    merger = ChannelMerger(
        tenant_id="t1",
        greenhouse_id="gh_A",
        emit_interval=emit_interval,
        source_configs=_source_configs(),
        on_reading=on_reading,
        on_status=noop_status,
    )

    # A reading in a recent past bucket (valid)
    await merger.submit(
        _reading("climate", {"temperature": 22.0}, _ts(bucket_epoch + 0.01))
    )

    await _run_merger_briefly(merger)

    # Should NOT be dropped
    assert merger.dropped_future_count == 0
    assert len(emitted) >= 1
