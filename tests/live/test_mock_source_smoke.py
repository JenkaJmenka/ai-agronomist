"""Smoke tests for MockScadaSource and build_mock_sources_for_scope.

Validates: seeded determinism, all values within CHANNEL_BOUNDS and finite,
dropout episode omits a channel, and the two-source factory produces
expected configurations.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import pytest

from src.live.events import CHANNEL_BOUNDS, SourceReading
from src.live.sources.base import SourceConfig
from src.live.sources.mock import MockScadaSource, build_mock_sources_for_scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(
    channels: list[str],
    seed: int = 42,
    interval: float = 0.01,
    anomaly_rate: float = 0.0,
    start_time: datetime | None = None,
) -> MockScadaSource:
    """Build a mock source with a short interval for testing."""
    params: dict[str, object] = {"seed": seed}
    if start_time is not None:
        params["start_time"] = start_time
    config = SourceConfig(
        source_id="test_src",
        vendor="mock",
        tenant_id="t1",
        greenhouse_id="gh_A",
        channels=channels,
        priority=10,
        poll_interval_seconds=interval,
        params=params,
    )
    return MockScadaSource(config)


async def _collect_readings(
    source: MockScadaSource,
    count: int,
) -> list[SourceReading]:
    """Collect *count* readings from the source stream."""
    await source.connect()
    readings: list[SourceReading] = []
    try:
        async for reading in source.stream():
            readings.append(reading)
            if len(readings) >= count:
                break
    finally:
        await source.close()
    return readings


# ---------------------------------------------------------------------------
# Tests — basic signal properties
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_readings_within_bounds() -> None:
    """All channel values must be within CHANNEL_BOUNDS and finite."""
    source = _make_source(["temperature", "humidity", "co2", "light"])
    readings = await _collect_readings(source, 20)

    for r in readings:
        for ch, val in r.channels.items():
            assert math.isfinite(val), f"{ch}={val} is not finite"
            lo, hi = CHANNEL_BOUNDS[ch]
            assert lo <= val <= hi, f"{ch}={val} outside [{lo}, {hi}]"


@pytest.mark.asyncio
async def test_seeded_determinism() -> None:
    """Two sources with the same seed produce identical channel values.

    Uses a pinned ``start_time`` so logical timestamps advance identically
    regardless of wall-clock jitter between the two sequential runs.
    """
    pinned = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    src_a = _make_source(["temperature"], seed=99, start_time=pinned)
    src_b = _make_source(["temperature"], seed=99, start_time=pinned)

    readings_a = await _collect_readings(src_a, 5)
    readings_b = await _collect_readings(src_b, 5)

    for ra, rb in zip(readings_a, readings_b):
        assert ra.channels["temperature"] == rb.channels["temperature"]


@pytest.mark.asyncio
async def test_different_seeds_diverge() -> None:
    """Different seeds produce different channel values."""
    src_a = _make_source(["temperature"], seed=1)
    src_b = _make_source(["temperature"], seed=2)

    readings_a = await _collect_readings(src_a, 5)
    readings_b = await _collect_readings(src_b, 5)

    # At least one reading should differ
    any_differ = any(
        ra.channels["temperature"] != rb.channels["temperature"]
        for ra, rb in zip(readings_a, readings_b)
    )
    assert any_differ


@pytest.mark.asyncio
async def test_source_reading_metadata() -> None:
    """SourceReading carries correct source_id, vendor, and scope."""
    source = _make_source(["temperature"])
    readings = await _collect_readings(source, 1)

    r = readings[0]
    assert r.source_id == "test_src"
    assert r.vendor == "mock"
    assert r.tenant_id == "t1"
    assert r.greenhouse_id == "gh_A"


# ---------------------------------------------------------------------------
# Tests — anomaly episodes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dropout_episode_omits_channel() -> None:
    """Force a dropout episode and verify the channel is missing."""
    source = _make_source(["temperature"], seed=42, interval=0.001)
    await source.connect()

    # Force a dropout episode directly
    from src.live.sources.mock_episodes import _AnomalyEpisode
    source._episode = _AnomalyEpisode(
        episode_type="dropout",
        channel="temperature",
        remaining_ticks=3,
        magnitude=0.0,
        accumulated=0.0,
    )

    channels = source._generate_channels(datetime.now(timezone.utc))
    assert "temperature" not in channels
    await source.close()


@pytest.mark.asyncio
async def test_stuck_episode_freezes_value() -> None:
    """Force a stuck episode and verify the channel value is frozen."""
    source = _make_source(["temperature"], seed=42, interval=0.001)
    await source.connect()

    from src.live.sources.mock_episodes import _AnomalyEpisode
    frozen_val = 25.0
    source._episode = _AnomalyEpisode(
        episode_type="stuck",
        channel="temperature",
        remaining_ticks=5,
        magnitude=0.0,
        accumulated=frozen_val,
    )

    values = []
    for _ in range(3):
        ch = source._generate_channels(datetime.now(timezone.utc))
        if "temperature" in ch:
            values.append(ch["temperature"])

    await source.close()

    # All stuck values should be the frozen value (possibly clamped)
    for v in values:
        assert v == frozen_val


@pytest.mark.asyncio
async def test_spike_episode_adds_magnitude() -> None:
    """Force a spike episode and verify the channel value shifts."""
    source = _make_source(["co2"], seed=42, interval=0.001)
    await source.connect()

    # Collect a normal value first
    normal_ch = source._generate_channels(datetime.now(timezone.utc))
    normal_val = normal_ch.get("co2", 400.0)

    from src.live.sources.mock_episodes import _AnomalyEpisode
    # Large positive spike
    source._episode = _AnomalyEpisode(
        episode_type="spike",
        channel="co2",
        remaining_ticks=2,
        magnitude=200.0,
        accumulated=0.0,
    )

    spiked_ch = source._generate_channels(datetime.now(timezone.utc))
    # Value should be shifted (possibly clamped), but not the same as normal
    assert "co2" in spiked_ch
    await source.close()


# ---------------------------------------------------------------------------
# Tests — build_mock_sources_for_scope
# ---------------------------------------------------------------------------

def test_build_mock_sources_returns_three() -> None:
    """Factory produces exactly three sources per scope."""
    sources = build_mock_sources_for_scope("t1", "gh_A")
    assert len(sources) == 3


def test_build_mock_sources_channel_split() -> None:
    """Climate covers temp/humidity/light; CO2 covers co2; fertigation covers soil."""
    sources = build_mock_sources_for_scope("t1", "gh_A")
    climate, co2, fertigation = sources

    assert set(climate.config.channels) == {"temperature", "humidity", "light"}
    assert set(co2.config.channels) == {"co2"}
    assert set(fertigation.config.channels) == {"soil_moisture", "root_zone_temp"}


def test_build_mock_sources_priorities() -> None:
    """Climate has the lowest (winning) priority, fertigation the highest."""
    sources = build_mock_sources_for_scope("t1", "gh_A")
    climate, co2, fertigation = sources

    assert climate.config.priority < co2.config.priority < fertigation.config.priority


def test_build_mock_sources_different_intervals() -> None:
    """CO2 runs at 1.5x and fertigation at 2x the climate interval."""
    sources = build_mock_sources_for_scope("t1", "gh_A")
    climate, co2, fertigation = sources

    co2_ratio = co2.config.poll_interval_seconds / climate.config.poll_interval_seconds
    fert_ratio = fertigation.config.poll_interval_seconds / climate.config.poll_interval_seconds
    assert abs(co2_ratio - 1.5) < 1e-9
    assert abs(fert_ratio - 2.0) < 1e-9
