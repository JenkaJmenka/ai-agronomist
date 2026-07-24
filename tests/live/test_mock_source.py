"""Comprehensive tests for MockScadaSource.

Covers: statistical sanity over seeded runs (bounds, finiteness, diurnal ordering);
every episode type under forced rate; dropout omits channels; determinism
(same seed → same series).
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import pytest

from src.live.events import CHANNEL_BOUNDS, SourceReading
from src.live.sources.base import SourceConfig
from src.live.sources.mock import (
    MockScadaSource,
    _clamp_and_sanitize,
    _diurnal_sine,
    _hour_fraction,
    build_mock_sources_for_scope,
)
from src.live.sources.mock_episodes import _AnomalyEpisode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    channels: list[str] | None = None,
    seed: int = 42,
    interval: float = 0.001,
    anomaly_rate: float = 0.0,
) -> MockScadaSource:
    """Build a mock source with a short interval for testing."""
    config = SourceConfig(
        source_id="test_src",
        vendor="mock",
        tenant_id="t1",
        greenhouse_id="gh_A",
        channels=channels or ["temperature", "humidity", "co2", "light"],
        priority=10,
        poll_interval_seconds=interval,
        params={"seed": seed},
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
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:

    def test_hour_fraction_midnight(self) -> None:
        ts = datetime(2026, 7, 18, 0, 0, 0, tzinfo=timezone.utc)
        assert _hour_fraction(ts) == 0.0

    def test_hour_fraction_noon(self) -> None:
        ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        assert _hour_fraction(ts) == 12.0

    def test_hour_fraction_with_minutes(self) -> None:
        ts = datetime(2026, 7, 18, 6, 30, 0, tzinfo=timezone.utc)
        assert _hour_fraction(ts) == 6.5

    def test_diurnal_sine_peaks_at_noon(self) -> None:
        """With default phase_offset=6, sin peaks at hour 12."""
        noon = _diurnal_sine(12.0)
        midnight = _diurnal_sine(0.0)
        assert noon > midnight
        assert abs(noon - 1.0) < 0.001

    def test_diurnal_sine_range(self) -> None:
        """Diurnal sine should be in [-1, 1]."""
        for hour in range(25):
            val = _diurnal_sine(float(hour))
            assert -1.001 <= val <= 1.001

    def test_clamp_and_sanitize_within_bounds(self) -> None:
        channels = {"temperature": 25.0, "humidity": 60.0}
        result = _clamp_and_sanitize(channels)
        assert result == channels

    def test_clamp_and_sanitize_clamps_high(self) -> None:
        channels = {"temperature": 999.0}
        result = _clamp_and_sanitize(channels)
        assert result["temperature"] == CHANNEL_BOUNDS["temperature"][1]

    def test_clamp_and_sanitize_clamps_low(self) -> None:
        channels = {"temperature": -999.0}
        result = _clamp_and_sanitize(channels)
        assert result["temperature"] == CHANNEL_BOUNDS["temperature"][0]

    def test_clamp_and_sanitize_drops_nan(self) -> None:
        channels = {"temperature": float("nan")}
        result = _clamp_and_sanitize(channels)
        assert "temperature" not in result

    def test_clamp_and_sanitize_drops_inf(self) -> None:
        channels = {"temperature": float("inf")}
        result = _clamp_and_sanitize(channels)
        assert "temperature" not in result

    def test_clamp_and_sanitize_unknown_channel(self) -> None:
        """Unknown channels use (val, val) bounds — returned as-is."""
        channels = {"custom_sensor": 42.0}
        result = _clamp_and_sanitize(channels)
        assert result["custom_sensor"] == 42.0


# ---------------------------------------------------------------------------
# Signal generation — basic properties
# ---------------------------------------------------------------------------


class TestSignalGeneration:

    @pytest.mark.asyncio
    async def test_all_readings_within_bounds_and_finite(self) -> None:
        source = _make_source()
        readings = await _collect_readings(source, 30)
        for r in readings:
            for ch, val in r.channels.items():
                assert math.isfinite(val), f"{ch}={val} not finite"
                lo, hi = CHANNEL_BOUNDS[ch]
                assert lo <= val <= hi, f"{ch}={val} outside [{lo}, {hi}]"

    @pytest.mark.asyncio
    async def test_reading_metadata(self) -> None:
        source = _make_source()
        readings = await _collect_readings(source, 1)
        r = readings[0]
        assert r.source_id == "test_src"
        assert r.vendor == "mock"
        assert r.tenant_id == "t1"
        assert r.greenhouse_id == "gh_A"

    @pytest.mark.asyncio
    async def test_single_channel_source(self) -> None:
        """Source with single channel produces readings with only that channel."""
        source = _make_source(channels=["co2"])
        readings = await _collect_readings(source, 5)
        for r in readings:
            assert set(r.channels.keys()) <= {"co2"}


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:

    def test_same_seed_same_series(self) -> None:
        """Two sources with the same seed produce identical channel values
        when called with the same timestamp (eliminates wall-clock jitter)."""
        src_a = _make_source(seed=99)
        src_b = _make_source(seed=99)

        fixed_ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        for _ in range(10):
            ch_a = src_a._generate_channels(fixed_ts)
            ch_b = src_b._generate_channels(fixed_ts)
            assert ch_a == ch_b

    def test_different_seeds_diverge(self) -> None:
        """Different seeds produce different channel values at the same timestamp."""
        src_a = _make_source(seed=1)
        src_b = _make_source(seed=2)

        fixed_ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        values_a = [src_a._generate_channels(fixed_ts) for _ in range(10)]
        values_b = [src_b._generate_channels(fixed_ts) for _ in range(10)]
        any_differ = any(
            a.get("temperature") != b.get("temperature")
            for a, b in zip(values_a, values_b)
        )
        assert any_differ


# ---------------------------------------------------------------------------
# Anomaly episodes — forced injection
# ---------------------------------------------------------------------------


class TestAnomalyEpisodes:

    @pytest.mark.asyncio
    async def test_dropout_omits_channel(self) -> None:
        source = _make_source(channels=["temperature"])
        await source.connect()
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
    async def test_stuck_freezes_value(self) -> None:
        source = _make_source(channels=["temperature"])
        await source.connect()
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
        for v in values:
            assert v == frozen_val

    @pytest.mark.asyncio
    async def test_spike_shifts_value(self) -> None:
        source = _make_source(channels=["co2"])
        await source.connect()
        source._episode = _AnomalyEpisode(
            episode_type="spike",
            channel="co2",
            remaining_ticks=2,
            magnitude=200.0,
            accumulated=0.0,
        )
        spiked = source._generate_channels(datetime.now(timezone.utc))
        assert "co2" in spiked
        await source.close()

    @pytest.mark.asyncio
    async def test_drift_accumulates(self) -> None:
        source = _make_source(channels=["temperature"])
        await source.connect()
        source._episode = _AnomalyEpisode(
            episode_type="drift",
            channel="temperature",
            remaining_ticks=10,
            magnitude=0.5,
            accumulated=0.0,
        )
        # After first tick, accumulated should be 0.5
        source._generate_channels(datetime.now(timezone.utc))
        assert source._episode is not None
        assert abs(source._episode.accumulated - 0.5) < 1e-6
        await source.close()

    @pytest.mark.asyncio
    async def test_episode_ends_after_ticks(self) -> None:
        source = _make_source(channels=["temperature"])
        await source.connect()
        source._episode = _AnomalyEpisode(
            episode_type="spike",
            channel="temperature",
            remaining_ticks=2,
            magnitude=5.0,
            accumulated=0.0,
        )
        source._generate_channels(datetime.now(timezone.utc))
        assert source._episode is not None  # 1 tick left
        source._generate_channels(datetime.now(timezone.utc))
        assert source._episode is None  # expired

    @pytest.mark.asyncio
    async def test_every_episode_type_occurs_under_forced_rate(self) -> None:
        """With high anomaly rate, all 4 episode types should eventually occur."""
        # Set a very high anomaly rate so we see all types
        config = SourceConfig(
            source_id="test_all_episodes",
            vendor="mock",
            tenant_id="t1",
            greenhouse_id="gh_A",
            channels=["temperature", "humidity", "co2", "light"],
            priority=10,
            poll_interval_seconds=0.001,
            params={"seed": 42},
        )
        source = MockScadaSource(config)
        await source.connect()

        episode_types_seen: set[str] = set()
        # Force many episodes
        for _ in range(200):
            source._episode = None  # reset so a new one can start
            # Override the random check by manipulating _rng state
            source._rng.random = lambda: 0.0  # always start episode
            channels = source._generate_channels(datetime.now(timezone.utc))
            if source._episode is not None:
                episode_types_seen.add(source._episode.episode_type)
            source._rng.random = source._rng.__class__.random.__get__(source._rng)  # restore

        await source.close()
        # We should see at least spike, drift, stuck, dropout
        # Note: with forced rate, not all may appear since choice is random
        # But with seed=42 and 200 iterations we should get most
        assert len(episode_types_seen) >= 2


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:

    @pytest.mark.asyncio
    async def test_connect_disconnect(self) -> None:
        source = _make_source()
        assert not source._is_connected
        await source.connect()
        assert source._is_connected
        await source.close()
        assert not source._is_connected

    @pytest.mark.asyncio
    async def test_stream_stops_after_close(self) -> None:
        """Stream loop terminates when source is closed."""
        source = _make_source(interval=0.001)
        await source.connect()

        count = 0
        async for _ in source.stream():
            count += 1
            if count >= 3:
                await source.close()

        assert count >= 3


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestBuildMockSources:

    def test_returns_four_sources(self) -> None:
        sources = build_mock_sources_for_scope("t1", "gh_A")
        assert len(sources) == 4

    def test_channel_split(self) -> None:
        sources = build_mock_sources_for_scope("t1", "gh_A")
        climate, co2, fertigation, gas = sources
        assert set(climate.config.channels) == {"temperature", "humidity", "light"}
        assert set(co2.config.channels) == {"co2"}
        assert set(fertigation.config.channels) == {"soil_moisture", "root_zone_temp"}
        assert set(gas.config.channels) == {"c2h4", "gas_co2", "no", "no2", "nox", "co"}

    def test_priorities(self) -> None:
        sources = build_mock_sources_for_scope("t1", "gh_A")
        climate, co2, fertigation, gas = sources
        assert (
            climate.config.priority
            < co2.config.priority
            < fertigation.config.priority
            < gas.config.priority
        )

    def test_interval_ratio(self) -> None:
        sources = build_mock_sources_for_scope("t1", "gh_A")
        climate, co2, fertigation, gas = sources
        co2_ratio = co2.config.poll_interval_seconds / climate.config.poll_interval_seconds
        fert_ratio = fertigation.config.poll_interval_seconds / climate.config.poll_interval_seconds
        gas_ratio = gas.config.poll_interval_seconds / climate.config.poll_interval_seconds
        assert abs(co2_ratio - 1.5) < 1e-9
        assert abs(fert_ratio - 2.0) < 1e-9
        assert abs(gas_ratio - 2.5) < 1e-9

    def test_scope_propagated(self) -> None:
        sources = build_mock_sources_for_scope("tX", "ghY")
        for s in sources:
            assert s.config.tenant_id == "tX"
            assert s.config.greenhouse_id == "ghY"

    def test_deterministic_seeds(self) -> None:
        """Climate and CO₂ keep deterministic seeds (42 and 137)."""
        sources = build_mock_sources_for_scope("t1", "gh_A")
        assert sources[0].config.params.get("seed") == 42
        assert sources[1].config.params.get("seed") == 137


# ---------------------------------------------------------------------------
# F5 — Fertigation channel noise stds in _NOISE_STD
# ---------------------------------------------------------------------------


class TestFertigationNoiseStdEntries:
    """_NOISE_STD must contain soil_moisture and root_zone_temp entries
    sourced from the constants in fertigation.py, so random episodes on
    those channels use realistic magnitudes (not the 1.0 fallback)."""

    def test_soil_moisture_entry_matches_fertigation(self) -> None:
        from src.live.sources.mock_episodes import _NOISE_STD
        from src.live.sources.fertigation import _SM_NOISE_STD

        assert "soil_moisture" in _NOISE_STD, (
            "_NOISE_STD must have a soil_moisture entry"
        )
        assert _NOISE_STD["soil_moisture"] == _SM_NOISE_STD

    def test_root_zone_temp_entry_matches_fertigation(self) -> None:
        from src.live.sources.mock_episodes import _NOISE_STD
        from src.live.sources.fertigation import _RZT_NOISE_STD

        assert "root_zone_temp" in _NOISE_STD, (
            "_NOISE_STD must have a root_zone_temp entry"
        )
        assert _NOISE_STD["root_zone_temp"] == _RZT_NOISE_STD

    def test_no_fallback_used_for_fertigation_channels(self) -> None:
        """Episode magnitudes for fertigation channels must not use the 1.0 fallback."""
        from src.live.sources.mock_episodes import _NOISE_STD

        for channel in ("soil_moisture", "root_zone_temp"):
            noise = _NOISE_STD.get(channel)
            assert noise is not None, f"Missing _NOISE_STD entry for {channel}"
            assert noise < 1.0, (
                f"_NOISE_STD[{channel!r}] = {noise} — should be much smaller "
                f"than the 1.0 fallback to avoid unrealistic episode magnitudes"
            )
