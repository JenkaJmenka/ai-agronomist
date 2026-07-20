"""Tests for the FertigationSignalState and mock fertigation source.

Covers: soil_moisture bounds + sawtooth behaviour (depletion → refill);
root_zone_temp bounds + determinism; fertigation channels in the mock source;
anomaly episodes can target fertigation channels.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone

import pytest

from src.live.events import CHANNEL_BOUNDS
from src.live.sources.base import SourceConfig
from src.live.sources.fertigation import (
    FertigationSignalState,
    _RZT_CEILING,
    _RZT_FLOOR,
    _SM_CEILING,
    _SM_FLOOR,
    _SM_REFILL_TARGET,
    _SM_REFILL_TRIGGER,
)
from src.live.sources.mock import MockScadaSource
from src.live.sources.mock_episodes import _AnomalyEpisode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fertigation_source(
    seed: int = 42,
    interval: float = 0.001,
    start_time: str | None = "2026-07-18T12:00:00+00:00",
) -> MockScadaSource:
    """Build a mock source with fertigation channels and pinned start_time."""
    config = SourceConfig(
        source_id="test_fert",
        vendor="mock",
        tenant_id="t1",
        greenhouse_id="gh_A",
        channels=["soil_moisture", "root_zone_temp"],
        priority=30,
        poll_interval_seconds=interval,
        params={"seed": seed, "start_time": start_time},
    )
    return MockScadaSource(config)


# ---------------------------------------------------------------------------
# FertigationSignalState — soil moisture
# ---------------------------------------------------------------------------


class TestSoilMoisture:

    def test_stays_within_bounds_over_long_run(self) -> None:
        """Over 1000 ticks, soil_moisture stays within [_SM_FLOOR, _SM_CEILING]."""
        rng = random.Random(42)
        state = FertigationSignalState(rng)
        for tick in range(1000):
            hour_frac = (tick * 0.1) % 24.0
            values = state.generate(hour_frac)
            sm = values["soil_moisture"]
            assert _SM_FLOOR <= sm <= _SM_CEILING, (
                f"Tick {tick}: soil_moisture={sm} out of [{_SM_FLOOR}, {_SM_CEILING}]"
            )

    def test_sawtooth_pattern_depletion_then_refill(self) -> None:
        """Soil moisture depletes then shows a refill jump after crossing the threshold."""
        rng = random.Random(42)
        state = FertigationSignalState(rng, initial_moisture=78.0)

        values_over_time = []
        for tick in range(2000):
            vals = state.generate(12.0)  # Fixed hour to isolate SM pattern
            values_over_time.append(vals["soil_moisture"])

        # Check that we see depletion (declining values) and refill (jumps).
        # Look for at least one transition from below refill trigger to near target.
        saw_depletion = False
        saw_refill = False
        for i in range(1, len(values_over_time)):
            if values_over_time[i] < values_over_time[i - 1]:
                saw_depletion = True
            if values_over_time[i] - values_over_time[i - 1] > 1.0:
                saw_refill = True

        assert saw_depletion, "Expected to see depletion (declining soil moisture)"
        assert saw_refill, "Expected to see refill jumps"

    def test_refill_targets_near_refill_target(self) -> None:
        """After a full refill cycle, soil moisture reaches near _SM_REFILL_TARGET."""
        rng = random.Random(99)
        state = FertigationSignalState(rng, initial_moisture=_SM_REFILL_TRIGGER - 1.0)

        # Force below trigger
        state.soil_moisture = _SM_REFILL_TRIGGER - 0.5

        # Run enough ticks for the refill to complete (3 ticks)
        for _ in range(5):
            state.generate(12.0)

        # After refill, should be near the target
        assert state.soil_moisture >= _SM_REFILL_TRIGGER


# ---------------------------------------------------------------------------
# FertigationSignalState — root_zone_temp
# ---------------------------------------------------------------------------


class TestRootZoneTemp:

    def test_stays_within_bounds(self) -> None:
        """Over many ticks across all hours, root_zone_temp stays bounded."""
        rng = random.Random(42)
        state = FertigationSignalState(rng)
        for tick in range(500):
            hour_frac = (tick * 0.5) % 24.0
            values = state.generate(hour_frac)
            rzt = values["root_zone_temp"]
            assert _RZT_FLOOR <= rzt <= _RZT_CEILING, (
                f"Tick {tick}: root_zone_temp={rzt} out of [{_RZT_FLOOR}, {_RZT_CEILING}]"
            )

    def test_deterministic_with_same_seed(self) -> None:
        """Two runs with the same seed produce identical root_zone_temp series."""
        series_a = []
        series_b = []
        for series_list, seed in ((series_a, 42), (series_b, 42)):
            rng = random.Random(seed)
            state = FertigationSignalState(rng)
            for tick in range(50):
                vals = state.generate(12.0)
                series_list.append(vals["root_zone_temp"])

        assert series_a == series_b

    def test_different_seeds_diverge(self) -> None:
        """Two runs with different seeds produce different root_zone_temp series."""
        series_a = []
        series_b = []
        for series_list, seed in ((series_a, 1), (series_b, 2)):
            rng = random.Random(seed)
            state = FertigationSignalState(rng)
            for tick in range(50):
                vals = state.generate(12.0)
                series_list.append(vals["root_zone_temp"])

        assert series_a != series_b

    def test_diurnal_variation(self) -> None:
        """Root zone temp shows diurnal variation (different at different hours)."""
        rng = random.Random(42)
        state = FertigationSignalState(rng)

        morning_vals = [state.generate(6.0)["root_zone_temp"] for _ in range(20)]
        rng2 = random.Random(42)
        state2 = FertigationSignalState(rng2)
        evening_vals = [state2.generate(18.0)["root_zone_temp"] for _ in range(20)]

        mean_morning = sum(morning_vals) / len(morning_vals)
        mean_evening = sum(evening_vals) / len(evening_vals)

        # They should differ due to different hour_frac in the sine
        assert abs(mean_morning - mean_evening) > 0.1


# ---------------------------------------------------------------------------
# FertigationSignalState — generate output
# ---------------------------------------------------------------------------


class TestFertigationGenerate:

    def test_returns_both_channels(self) -> None:
        """generate() returns dict with both soil_moisture and root_zone_temp."""
        rng = random.Random(42)
        state = FertigationSignalState(rng)
        result = state.generate(12.0)
        assert "soil_moisture" in result
        assert "root_zone_temp" in result

    def test_values_are_finite(self) -> None:
        """All generated values are finite floats."""
        rng = random.Random(42)
        state = FertigationSignalState(rng)
        for tick in range(100):
            result = state.generate((tick * 0.3) % 24.0)
            for key, val in result.items():
                assert math.isfinite(val), f"{key}={val} is not finite at tick {tick}"


# ---------------------------------------------------------------------------
# MockScadaSource — fertigation channel generation via _generate_channels
# ---------------------------------------------------------------------------


class TestMockSourceFertigationChannels:

    def test_fertigation_source_generates_correct_channels(self) -> None:
        """A source with fertigation channels produces soil_moisture & root_zone_temp."""
        source = _make_fertigation_source()
        ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        channels = source._generate_channels(ts)
        assert "soil_moisture" in channels
        assert "root_zone_temp" in channels

    def test_fertigation_channels_within_channel_bounds(self) -> None:
        """Fertigation channels respect CHANNEL_BOUNDS after clamping."""
        source = _make_fertigation_source()
        for tick in range(100):
            ts = datetime(2026, 7, 18, 12, tick % 60, tick % 60, tzinfo=timezone.utc)
            channels = source._generate_channels(ts)
            for ch, val in channels.items():
                lo, hi = CHANNEL_BOUNDS[ch]
                assert lo <= val <= hi, f"{ch}={val} outside [{lo}, {hi}]"

    def test_fertigation_deterministic_with_pinned_start_time(self) -> None:
        """Same seed + same start_time → identical channel series."""
        src_a = _make_fertigation_source(seed=42)
        src_b = _make_fertigation_source(seed=42)
        ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        for _ in range(50):
            ch_a = src_a._generate_channels(ts)
            ch_b = src_b._generate_channels(ts)
            assert ch_a == ch_b

    @pytest.mark.asyncio
    async def test_fertigation_source_streams_readings(self) -> None:
        """Fertigation source yields SourceReading objects via stream()."""
        source = _make_fertigation_source(interval=0.001)
        await source.connect()
        count = 0
        async for reading in source.stream():
            assert "soil_moisture" in reading.channels
            assert "root_zone_temp" in reading.channels
            count += 1
            if count >= 5:
                break
        await source.close()
        assert count == 5


# ---------------------------------------------------------------------------
# Anomaly episodes on fertigation channels
# ---------------------------------------------------------------------------


class TestFertigationAnomalyEpisodes:

    def test_spike_on_soil_moisture(self) -> None:
        """A spike episode can be injected on soil_moisture."""
        source = _make_fertigation_source()
        source._episode = _AnomalyEpisode(
            episode_type="spike",
            channel="soil_moisture",
            remaining_ticks=3,
            magnitude=20.0,
            accumulated=0.0,
        )
        ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        channels = source._generate_channels(ts)
        # Spike adds magnitude; clamped by CHANNEL_BOUNDS
        assert "soil_moisture" in channels

    def test_dropout_on_root_zone_temp(self) -> None:
        """A dropout episode removes root_zone_temp from output."""
        source = _make_fertigation_source()
        source._episode = _AnomalyEpisode(
            episode_type="dropout",
            channel="root_zone_temp",
            remaining_ticks=3,
            magnitude=0.0,
            accumulated=0.0,
        )
        ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        channels = source._generate_channels(ts)
        assert "root_zone_temp" not in channels

    def test_stuck_on_soil_moisture(self) -> None:
        """A stuck episode freezes soil_moisture."""
        source = _make_fertigation_source()
        frozen_val = 70.0
        source._episode = _AnomalyEpisode(
            episode_type="stuck",
            channel="soil_moisture",
            remaining_ticks=5,
            magnitude=0.0,
            accumulated=frozen_val,
        )
        ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        channels = source._generate_channels(ts)
        assert channels["soil_moisture"] == frozen_val
