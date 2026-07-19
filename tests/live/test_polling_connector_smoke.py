"""Smoke tests for PollingSourceConnector base (Phase 5B).

Coverage
--------
- Readings yielded in ascending timestamp order.
- since-cursor advances to the max timestamp after each batch.
- Boundary deduplication: readings at the cursor are not re-yielded.
- close() stops the stream cleanly.
- Exponential back-off on repeated errors (1 s → 2 → 4 → … → 300 s).
- Empty batch does not advance the cursor.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.live.events import SourceReading
from src.live.sources.base import SourceConfig
from src.live.sources.polling import PollingSourceConnector

TENANT = "t1"
GH = "gh_A"


# ------------------------------------------------------------------
# Test helpers
# ------------------------------------------------------------------


def _ts(second: int) -> datetime:
    """Build a UTC datetime with a distinct second value."""
    return datetime(2024, 1, 1, 0, 0, second, tzinfo=timezone.utc)


def _reading(second: int, channel: str = "temperature") -> SourceReading:
    return SourceReading(
        source_id="echo",
        vendor="echo",
        tenant_id=TENANT,
        greenhouse_id=GH,
        timestamp=_ts(second),
        channels={channel: 22.0},
    )


def _make_config(poll_interval: float = 0.005) -> SourceConfig:
    return SourceConfig(
        source_id="echo",
        vendor="echo",
        tenant_id=TENANT,
        greenhouse_id=GH,
        channels=["temperature", "humidity"],
        poll_interval_seconds=poll_interval,
    )


class _EchoConnector(PollingSourceConnector):
    """Concrete polling connector for testing; returns pre-defined batches."""

    vendor = "echo"

    def __init__(
        self,
        config: SourceConfig,
        batches: list[list[SourceReading]],
    ) -> None:
        super().__init__(config)
        self._batches = list(batches)
        self._batch_index: int = 0
        self.seen_since: list[datetime | None] = []

    async def fetch_since(self, since: datetime | None) -> list[SourceReading]:
        self.seen_since.append(since)
        if self._batch_index < len(self._batches):
            result = self._batches[self._batch_index]
            self._batch_index += 1
            return result
        # All batches consumed — signal close so tests terminate cleanly.
        self._closed = True
        return []


async def _collect(connector: PollingSourceConnector, count: int) -> list[SourceReading]:
    """Consume *count* readings from the stream with a 2-second hard cap."""
    items: list[SourceReading] = []

    async def _run() -> None:
        async for reading in connector.stream():
            items.append(reading)
            if len(items) >= count:
                break

    try:
        await asyncio.wait_for(_run(), timeout=2.0)
    except TimeoutError:
        pass
    return items


async def _drain(connector: PollingSourceConnector, timeout: float = 2.0) -> list[SourceReading]:
    """Run the stream until it exits cleanly or times out."""
    items: list[SourceReading] = []
    try:
        async def _run() -> None:
            async for r in connector.stream():
                items.append(r)
        await asyncio.wait_for(_run(), timeout=timeout)
    except TimeoutError:
        pass
    return items


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


async def test_yields_readings_in_timestamp_order():
    """stream() sorts each batch ascending by timestamp before yielding."""
    r2, r1 = _reading(2), _reading(1)
    connector = _EchoConnector(_make_config(), batches=[[r2, r1]])
    await connector.connect()
    items = await _collect(connector, 2)
    assert [r.timestamp for r in items] == [_ts(1), _ts(2)]


async def test_since_cursor_is_none_on_first_fetch():
    """The initial since cursor passed to fetch_since is None."""
    connector = _EchoConnector(_make_config(), batches=[[_reading(1)]])
    await connector.connect()
    await _drain(connector)
    assert connector.seen_since[0] is None


async def test_since_cursor_advances_to_max_timestamp():
    """After the first batch, since equals the max timestamp seen."""
    connector = _EchoConnector(_make_config(), batches=[[_reading(1), _reading(3)], []])
    await connector.connect()
    await _drain(connector)
    # Second fetch should carry since = T3.
    assert len(connector.seen_since) >= 2
    assert connector.seen_since[1] == _ts(3)


async def test_deduplicates_boundary_timestamp():
    """A reading at the cursor boundary is not yielded a second time."""
    r1, r2, r3 = _reading(1), _reading(2), _reading(3)
    # Second batch re-delivers r2 (boundary) alongside the new r3.
    connector = _EchoConnector(_make_config(), batches=[[r1, r2], [r2, r3]])
    await connector.connect()
    items = await _drain(connector)
    timestamps = [r.timestamp for r in items]
    assert timestamps.count(_ts(2)) == 1
    assert _ts(1) in timestamps
    assert _ts(3) in timestamps


async def test_empty_batch_does_not_advance_cursor():
    """An empty batch leaves since unchanged; next fetch still uses old cursor."""
    connector = _EchoConnector(_make_config(), batches=[[], [_reading(5)]])
    await connector.connect()
    items = await _drain(connector)
    assert connector.seen_since[0] is None
    # Second call: since still None because empty batch advanced nothing.
    assert connector.seen_since[1] is None
    assert len(items) == 1
    assert items[0].timestamp == _ts(5)


async def test_close_stops_stream():
    """close() causes stream() to stop cleanly without CancelledError."""

    class _InfiniteConnector(PollingSourceConnector):
        vendor = "infinite"
        _call_count: int = 0

        async def fetch_since(self, since: datetime | None) -> list[SourceReading]:
            self._call_count += 1
            return [_reading(self._call_count)]

    config = _make_config(poll_interval=0.005)
    connector = _InfiniteConnector(config)
    await connector.connect()

    items = await _collect(connector, 3)
    await connector.close()
    assert len(items) == 3
    assert connector._closed is True


async def test_exponential_backoff_on_errors(monkeypatch: pytest.MonkeyPatch):
    """Back-off doubles on each consecutive error; resets after success."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    error_count = 0

    class _FlakyConnector(PollingSourceConnector):
        vendor = "flaky"

        async def fetch_since(self, since: datetime | None) -> list[SourceReading]:
            nonlocal error_count
            if error_count < 3:
                error_count += 1
                raise RuntimeError("simulated network error")
            # Success on the 4th call; stop loop.
            self._closed = True
            return []

    connector = _FlakyConnector(_make_config(poll_interval=0.0))
    await connector.connect()
    await _drain(connector, timeout=2.0)

    # Back-off sleeps should be 1.0, 2.0, 4.0 (then poll sleep 0.0 after success).
    backoff_sleeps = [s for s in slept if s > 0.0]
    assert backoff_sleeps == [1.0, 2.0, 4.0]


async def test_backoff_caps_at_max():
    """Back-off does not exceed _MAX_BACKOFF_SECONDS (300 s)."""
    from src.live.sources.polling import _MAX_BACKOFF_SECONDS

    slept: list[float] = []
    call_count = 0
    MANY_ERRORS = 15

    class _AlwaysFailConnector(PollingSourceConnector):
        vendor = "fail"

        async def fetch_since(self, since: datetime | None) -> list[SourceReading]:
            nonlocal call_count
            call_count += 1
            if call_count > MANY_ERRORS:
                self._closed = True
                return []
            raise RuntimeError("always fails")

    import asyncio as _asyncio

    orig_sleep = _asyncio.sleep

    async def capturing_sleep(seconds: float) -> None:
        slept.append(seconds)

    import unittest.mock as _mock

    with _mock.patch("asyncio.sleep", side_effect=capturing_sleep):
        connector = _AlwaysFailConnector(_make_config(poll_interval=0.0))
        await connector.connect()
        await _drain(connector, timeout=5.0)

    backoff_sleeps = [s for s in slept if s > 0.0]
    assert max(backoff_sleeps) <= _MAX_BACKOFF_SECONDS
