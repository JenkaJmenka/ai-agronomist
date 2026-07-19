"""Tests for the demo-mode guaranteed anomaly injection mechanism.

Covers: guaranteed compound episode scheduling within [MIN, MAX] window even
when ANOMALY_RATE=0; compound episodes target only canonical ML channels;
fertigation-only source never gets demo episodes; after an episode ends,
next one is scheduled.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.live.sources.base import SourceConfig
from src.live.sources.mock import (
    ML_CHANNELS,
    MockScadaSource,
    _CompoundEpisode,
    _SCORE_VISIBLE_EPISODES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_demo_source(
    channels: list[str],
    seed: int = 42,
    interval: float = 1.0,
    demo_mode: bool = True,
    start_time: str = "2026-07-18T12:00:00+00:00",
) -> MockScadaSource:
    """Build a mock source with demo mode and a pinned start_time."""
    config = SourceConfig(
        source_id="test_demo",
        vendor="mock",
        tenant_id="t1",
        greenhouse_id="gh_A",
        channels=channels,
        priority=10,
        poll_interval_seconds=interval,
        params={
            "seed": seed,
            "demo_mode": demo_mode,
            "start_time": start_time,
        },
    )
    return MockScadaSource(config)


# ---------------------------------------------------------------------------
# Scheduling within [MIN, MAX] window
# ---------------------------------------------------------------------------


class TestDemoScheduling:

    def test_episode_scheduled_on_init(self) -> None:
        """When demo_mode is True, _demo_next_tick is set during __init__."""
        source = _make_demo_source(channels=["temperature", "humidity", "light"])
        assert source._demo_mode is True
        assert source._demo_next_tick is not None
        assert source._demo_next_tick > 0

    def test_no_schedule_when_demo_disabled(self) -> None:
        """When demo_mode is False, _demo_next_tick is None."""
        source = _make_demo_source(
            channels=["temperature", "humidity", "light"],
            demo_mode=False,
        )
        assert source._demo_mode is False
        assert source._demo_next_tick is None

    def test_guaranteed_episode_within_window(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With ANOMALY_RATE=0 and tiny demo interval, compound episode occurs within window."""
        # Monkeypatch config to make the window small (2-4 seconds with 1s interval
        # → 2-4 ticks).
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 2.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 4.0)

        source = _make_demo_source(
            channels=["temperature", "humidity", "co2", "light"],
            interval=1.0,
        )

        # With interval=1.0, min_ticks=2, max_ticks=4
        assert source._demo_next_tick is not None
        # _demo_next_tick should be >= 2 and <= 4 (from tick_count=0)
        assert 2 <= source._demo_next_tick <= 4

        # Run ticks until we see a compound episode.
        episode_started = False
        for tick in range(10):
            ts = datetime(2026, 7, 18, 12, 0, tick, tzinfo=timezone.utc)
            source._tick_count = tick
            source._generate_channels(ts)
            if source._compound_episode is not None:
                episode_started = True
                break

        assert episode_started, (
            "Demo compound episode should have started within the [MIN, MAX] window"
        )


# ---------------------------------------------------------------------------
# Episodes target only ML channels
# ---------------------------------------------------------------------------


class TestDemoEpisodeTargeting:

    def test_episode_targets_ml_channel_only(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Demo compound episodes only target canonical ML channels (temp, humidity, co2, light)."""
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 1.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 2.0)

        # Source has ML channels + fertigation channels mixed
        all_channels = ["temperature", "humidity", "co2", "light", "soil_moisture", "root_zone_temp"]
        channels_seen: list[str] = []

        # Run many trials to collect compound episode channels
        for trial_seed in range(20):
            source = _make_demo_source(
                channels=all_channels,
                seed=trial_seed,
                interval=1.0,
            )
            for tick in range(5):
                ts = datetime(2026, 7, 18, 12, 0, tick, tzinfo=timezone.utc)
                source._tick_count = tick
                source._generate_channels(ts)
                if source._compound_episode is not None:
                    channels_seen.extend(source._compound_episode.channel_magnitudes.keys())
                    break

        assert len(channels_seen) > 0, "Expected at least one demo compound episode"
        for ch in channels_seen:
            assert ch in ML_CHANNELS, (
                f"Demo compound episode targeted non-ML channel '{ch}'"
            )

    def test_compound_episode_is_multi_channel(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Demo compound episodes offset multiple channels (2-3) simultaneously."""
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 1.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 2.0)

        channel_counts_seen: list[int] = []
        for trial_seed in range(50):
            source = _make_demo_source(
                channels=["temperature", "humidity", "co2", "light"],
                seed=trial_seed,
                interval=1.0,
            )
            for tick in range(5):
                ts = datetime(2026, 7, 18, 12, 0, tick, tzinfo=timezone.utc)
                source._tick_count = tick
                source._generate_channels(ts)
                if source._compound_episode is not None:
                    channel_counts_seen.append(
                        len(source._compound_episode.channel_magnitudes)
                    )
                    break

        assert len(channel_counts_seen) > 0, "Expected compound episodes"
        for count in channel_counts_seen:
            assert 2 <= count <= 3, (
                f"Compound episode should offset 2-3 channels, got {count}"
            )


# ---------------------------------------------------------------------------
# Fertigation-only source never gets demo episodes
# ---------------------------------------------------------------------------


class TestDemoModeFertigationOnly:

    def test_fertigation_only_source_no_demo_episode(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A source with only fertigation channels (no ML) never gets demo episodes."""
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 1.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 2.0)

        source = _make_demo_source(
            channels=["soil_moisture", "root_zone_temp"],
            seed=42,
            interval=1.0,
        )

        # Run many ticks — should never get a compound demo episode
        for tick in range(50):
            ts = datetime(2026, 7, 18, 12, 0, tick, tzinfo=timezone.utc)
            source._tick_count = tick
            source._generate_channels(ts)
            assert source._compound_episode is None, (
                f"Fertigation-only source got a compound demo episode at tick {tick}"
            )

    def test_fertigation_only_reschedules_without_episode(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When demo fires on a fertigation-only source, it reschedules without creating an episode."""
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 1.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 2.0)

        source = _make_demo_source(
            channels=["soil_moisture", "root_zone_temp"],
            seed=42,
            interval=1.0,
        )

        original_next_tick = source._demo_next_tick
        assert original_next_tick is not None

        # Advance past the scheduled tick
        for tick in range(original_next_tick + 5):
            ts = datetime(2026, 7, 18, 12, 0, tick, tzinfo=timezone.utc)
            source._tick_count = tick
            source._generate_channels(ts)

        # After the fire and miss (no ML channels), it should have rescheduled
        assert source._demo_next_tick is not None
        assert source._demo_next_tick > original_next_tick


# ---------------------------------------------------------------------------
# Episode end triggers rescheduling
# ---------------------------------------------------------------------------


class TestDemoRescheduling:

    def test_next_episode_scheduled_after_first(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After a demo compound episode starts, the next one is already scheduled."""
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 1.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 2.0)

        source = _make_demo_source(
            channels=["temperature", "humidity", "co2", "light"],
            seed=42,
            interval=1.0,
        )

        episode_started_at = None

        for tick in range(10):
            ts = datetime(2026, 7, 18, 12, 0, tick, tzinfo=timezone.utc)
            source._tick_count = tick
            source._generate_channels(ts)
            if source._compound_episode is not None and episode_started_at is None:
                episode_started_at = tick
                # Check that the NEXT episode is already scheduled
                assert source._demo_next_tick is not None
                assert source._demo_next_tick > tick
                break

        assert episode_started_at is not None, "Demo compound episode should have started"

    def test_second_episode_fires_after_first_ends(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After the first demo compound episode ends, a second one fires eventually."""
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 1.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 3.0)

        source = _make_demo_source(
            channels=["temperature", "humidity", "co2", "light"],
            seed=42,
            interval=1.0,
        )

        episode_count = 0
        was_in_compound = False

        for tick in range(100):
            ts = datetime(2026, 7, 18, 12, tick // 60, tick % 60, tzinfo=timezone.utc)
            source._tick_count = tick
            source._generate_channels(ts)

            # Detect compound episode transitions.
            if source._compound_episode is not None:
                was_in_compound = True
            elif was_in_compound:
                episode_count += 1  # Compound episode just ended
                was_in_compound = False

        assert episode_count >= 2, (
            f"Expected at least 2 demo compound episodes in 100 ticks, saw {episode_count}"
        )


# ---------------------------------------------------------------------------
# Demo episode deferred when one is already active
# ---------------------------------------------------------------------------


class TestDemoActiveEpisodeDefer:

    def test_demo_defers_when_single_episode_active(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a single-channel episode is active when demo fires, it reschedules +1 tick."""
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 1.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 2.0)

        source = _make_demo_source(
            channels=["temperature", "humidity", "co2", "light"],
            seed=42,
            interval=1.0,
        )

        # Force an active single-channel episode
        from src.live.sources.mock import _AnomalyEpisode
        source._episode = _AnomalyEpisode(
            episode_type="spike",
            channel="temperature",
            remaining_ticks=99,
            magnitude=5.0,
            accumulated=0.0,
        )

        # Set the demo tick to fire now
        fire_tick = 5
        source._tick_count = fire_tick
        source._demo_next_tick = fire_tick

        ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        source._generate_channels(ts)

        # Should have deferred to tick_count + 1
        assert source._demo_next_tick == fire_tick + 1

    def test_demo_defers_when_compound_episode_active(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a compound episode is active when demo fires, it reschedules +1 tick."""
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_ANOMALY_RATE", 0.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", 1.0)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", 2.0)

        source = _make_demo_source(
            channels=["temperature", "humidity", "co2", "light"],
            seed=42,
            interval=1.0,
        )

        # Force an active compound episode
        source._compound_episode = _CompoundEpisode(
            channel_magnitudes={"temperature": 12.5, "humidity": 25.0},
            remaining_ticks=99,
        )

        # Set the demo tick to fire now
        fire_tick = 5
        source._tick_count = fire_tick
        source._demo_next_tick = fire_tick

        ts = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        source._generate_channels(ts)

        # Should have deferred to tick_count + 1
        assert source._demo_next_tick == fire_tick + 1
