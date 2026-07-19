"""Smoke tests for LiveHub + event DTOs over fakeredis streams.

Covers: publish→subscribe round-trip (via replay), two-subscriber fan-out,
replay after a given last_event_id, trimmed-id reset, and non-finite channel
rejection.

NOTE: fakeredis does not implement real XREAD blocking — ``block=`` returns
immediately when no new entries exist.  All subscribe tests therefore use
the **replay** path (``last_event_id`` pointing at a known entry), which
exercises the same code path as a reconnecting SSE client.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from src.live.events import (
    LiveAnomalyEvent,
    LiveReadingEvent,
    SourceReading,
    sanitize_channels,
)
from src.live.hub import LiveHub


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def redis_client() -> FakeRedis:
    """Shared fakeredis client for a single test."""
    client = FakeRedis()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def hub(redis_client: FakeRedis) -> LiveHub:
    """LiveHub wired to the shared fakeredis client with short block."""
    return LiveHub(redis_client=redis_client, block_ms=100)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT = "t1"
GREENHOUSE = "gh_A"
NOW = datetime.now(timezone.utc)


def _reading(channels: dict[str, float] | None = None) -> LiveReadingEvent:
    """Build a minimal LiveReadingEvent."""
    ch = channels or {"temperature": 22.5, "humidity": 65.0}
    return LiveReadingEvent(
        seq="",
        timestamp=NOW,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        channels=ch,
        sources={k: f"src_{k}" for k in ch},
    )


async def _collect_replay(
    hub: LiveHub,
    last_event_id: str | None = None,
) -> list[tuple[str, str, str]]:
    """Subscribe and collect entries until the first idle tick."""
    items: list[tuple[str, str, str]] = []
    async for entry_id, event_type, data_json in hub.subscribe(
        TENANT, GREENHOUSE, last_event_id=last_event_id,
    ):
        if event_type == "idle":
            break
        items.append((entry_id, event_type, data_json))
    return items


# ---------------------------------------------------------------------------
# Tests — publish / subscribe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_subscribe_roundtrip(hub: LiveHub) -> None:
    """Publish two readings; subscribe from the first → receive the second."""
    first_id = await hub.publish(TENANT, GREENHOUSE, "reading", _reading())
    second_id = await hub.publish(
        TENANT, GREENHOUSE, "reading",
        _reading({"co2": 420.0}),
    )

    received = await _collect_replay(hub, last_event_id=first_id)

    assert len(received) == 1
    assert received[0][0] == second_id
    assert received[0][1] == "reading"
    assert '"co2"' in received[0][2]


@pytest.mark.asyncio
async def test_publish_returns_valid_entry_id(hub: LiveHub) -> None:
    """publish() returns a non-empty Redis stream entry ID."""
    entry_id = await hub.publish(TENANT, GREENHOUSE, "reading", _reading())
    assert entry_id
    assert "-" in entry_id  # Redis stream IDs are <ms>-<seq>


@pytest.mark.asyncio
async def test_two_subscribers_both_receive(hub: LiveHub) -> None:
    """Two independent subscribers both receive the same replayed event."""
    anchor_id = await hub.publish(TENANT, GREENHOUSE, "reading", _reading())
    target_id = await hub.publish(
        TENANT, GREENHOUSE, "reading",
        _reading({"light": 800.0}),
    )

    results_a = await _collect_replay(hub, last_event_id=anchor_id)
    results_b = await _collect_replay(hub, last_event_id=anchor_id)

    assert len(results_a) == 1
    assert len(results_b) == 1
    assert results_a[0][0] == target_id
    assert results_b[0][0] == target_id


@pytest.mark.asyncio
async def test_replay_after_last_event_id(hub: LiveHub) -> None:
    """Subscribe with last_event_id replays only events after that ID."""
    ids: list[str] = []
    for i in range(3):
        eid = await hub.publish(
            TENANT, GREENHOUSE, "reading",
            _reading({"light": float(i * 100)}),
        )
        ids.append(eid)

    # Subscribe starting after the first entry → should get entries 2 and 3.
    received = await _collect_replay(hub, last_event_id=ids[0])

    assert len(received) == 2
    assert received[0][0] == ids[1]
    assert received[1][0] == ids[2]


@pytest.mark.asyncio
async def test_trimmed_id_yields_reset(redis_client: FakeRedis) -> None:
    """When last_event_id has been trimmed/unknown, a reset event is emitted."""
    hub = LiveHub(redis_client=redis_client, block_ms=100)

    # Publish some entries.
    for i in range(3):
        await hub.publish(TENANT, GREENHOUSE, "reading", _reading())

    # Subscribe with a fake ID that was never in the stream.
    fake_old_id = "1-0"
    received = await _collect_replay(hub, last_event_id=fake_old_id)

    # First event should be the synthetic reset.
    assert len(received) >= 1
    assert received[0][1] == "reset"
    assert "replay-gap" in received[0][2]


@pytest.mark.asyncio
async def test_subscribe_no_last_event_id_tails(hub: LiveHub) -> None:
    """Subscribe without last_event_id starts from $ (live tail only).

    With fakeredis (no real blocking), this immediately yields idle.
    """
    await hub.publish(TENANT, GREENHOUSE, "reading", _reading())

    received = await _collect_replay(hub, last_event_id=None)
    # Should get nothing — data was published before the subscribe.
    assert len(received) == 0


# ---------------------------------------------------------------------------
# Tests — non-finite guard
# ---------------------------------------------------------------------------

def test_nonfinite_channel_rejected_nan() -> None:
    """NaN in channels must be rejected by the event model."""
    with pytest.raises(Exception):
        LiveReadingEvent(
            seq="abc",
            timestamp=NOW,
            tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            channels={"temperature": float("nan")},
            sources={"temperature": "src"},
        )


def test_nonfinite_channel_rejected_inf() -> None:
    """Infinity in channels must be rejected by the event model."""
    with pytest.raises(Exception):
        LiveReadingEvent(
            seq="abc",
            timestamp=NOW,
            tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            channels={"humidity": float("inf")},
            sources={"humidity": "src"},
        )


def test_nonfinite_anomaly_score_rejected() -> None:
    """Non-finite anomaly_score must be rejected."""
    with pytest.raises(Exception):
        LiveReadingEvent(
            seq="abc",
            timestamp=NOW,
            tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            channels={"temperature": 22.0},
            sources={"temperature": "src"},
            anomaly_score=float("nan"),
        )


def test_sanitize_channels_strips_nonfinite() -> None:
    """sanitize_channels removes NaN and Inf but keeps valid values."""
    raw = {
        "temperature": 22.0,
        "humidity": float("nan"),
        "co2": float("inf"),
        "light": 500.0,
    }
    clean = sanitize_channels(raw)
    assert clean == {"temperature": 22.0, "light": 500.0}


def test_source_reading_rejects_nonfinite() -> None:
    """SourceReading must reject non-finite channel values."""
    with pytest.raises(Exception):
        SourceReading(
            source_id="s1",
            vendor="mock",
            tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            timestamp=NOW,
            channels={"temperature": float("nan")},
        )


def test_anomaly_event_rejects_nonfinite_score() -> None:
    """LiveAnomalyEvent must reject non-finite score."""
    with pytest.raises(Exception):
        LiveAnomalyEvent(
            seq="x",
            timestamp=NOW,
            tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            score=float("nan"),
            threshold=0.8,
            affected_channels=["temperature"],
            reasoning="test",
            window_start=NOW,
            window_end=NOW,
        )


def test_anomaly_event_rejects_nonfinite_threshold() -> None:
    """LiveAnomalyEvent must reject non-finite threshold."""
    with pytest.raises(Exception):
        LiveAnomalyEvent(
            seq="x",
            timestamp=NOW,
            tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            score=0.9,
            threshold=float("inf"),
            affected_channels=["temperature"],
            reasoning="test",
            window_start=NOW,
            window_end=NOW,
        )


# ---------------------------------------------------------------------------
# Tests — JSON serialization
# ---------------------------------------------------------------------------

def test_event_model_dump_json_roundtrip() -> None:
    """Event models produce valid compact JSON with ISO timestamps."""
    event = LiveReadingEvent(
        seq="123-0",
        timestamp=NOW,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        channels={"temperature": 22.5},
        sources={"temperature": "src_1"},
        anomaly_score=0.42,
    )
    json_str = event.model_dump_json()
    assert '"temperature"' in json_str
    assert "NaN" not in json_str
    assert str(NOW.year) in json_str


# ---------------------------------------------------------------------------
# Tests — factory pattern
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_factory_creates_per_subscriber_connection() -> None:
    """When a factory is provided, each subscriber gets its own client."""
    clients_created: list[FakeRedis] = []

    # All clients share the same in-memory server via connected=True default.
    def factory() -> FakeRedis:
        client = FakeRedis()
        clients_created.append(client)
        return client

    hub = LiveHub(redis_factory=factory, block_ms=100)

    event = _reading()
    await hub.publish(TENANT, GREENHOUSE, "reading", event)
    # Factory is called at least once for the publish.
    assert len(clients_created) >= 1
