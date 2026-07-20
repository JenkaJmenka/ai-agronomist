"""Mock SCADA data source for development and integration testing.

Produces deterministic diurnal sine signals (keyed to UTC wall-clock hour)
with Gaussian noise for the four canonical greenhouse channels, plus a
``mock_fertigation`` source with soil_moisture (sawtooth irrigation
pattern) and root_zone_temp (damped diurnal).

Anomaly-episode logic (spike/drift/stuck/dropout and compound demo
episodes) lives in :mod:`src.live.sources.mock_episodes`; scope factory
helpers live in :mod:`src.live.sources.mock_factory`.
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, ClassVar, Dict, Optional, Union

from src.config import (
    AMPLITUDE_DIVISOR,
    CO2_NOISE_STD,
    CO2_RANGE,
    HUMIDITY_COUPLING_FACTOR,
    HUMIDITY_NOISE_STD,
    HUMIDITY_RANGE,
    LIGHT_AMPLITUDE,
    LIGHT_NOISE_STD,
    TEMP_NOISE_STD,
    TEMP_RANGE,
)
from src.live.events import SourceReading
from src.live.sources.base import LiveSourceConnector, SourceConfig
from src.live.sources.fertigation import FertigationSignalState
from src.live.sources.mock_episodes import (
    EpisodeEngine,
    _AnomalyEpisode,
    _CompoundEpisode,
)
from src.live.sources.mock_factory import (  # noqa: F401  (re-export: stable import path)
    _clamp_and_sanitize,
    build_mock_sources_for_scope,
)

_TWO_PI: float = 2.0 * math.pi


def _hour_fraction(ts: datetime) -> float:
    """Return the fractional hour of day in UTC (0.0 to 24.0)."""
    return ts.hour + ts.minute / 60.0 + ts.second / 3600.0


def _diurnal_sine(hour_frac: float, phase_offset: float = 6.0) -> float:
    """Sine with a 24-hour period.  Default offset peaks at noon."""
    return math.sin(_TWO_PI * (hour_frac - phase_offset) / 24.0)


class MockScadaSource(LiveSourceConnector):
    """Mock SCADA source producing synthetic diurnal greenhouse signals.

    Deterministic when ``SourceConfig.params["seed"]`` is set (uses a
    per-instance ``random.Random``, shared with the episode engine so the
    draw order is stable).
    """

    vendor: ClassVar[str] = "mock"

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        seed = config.params.get("seed")
        self._rng = random.Random(seed)
        self._is_connected = False
        self._start_time: Optional[datetime] = self._parse_start_time(
            config.params.get("start_time"),
        )
        self._tick_count: int = 0

        # Fertigation signal state (only active when source carries those channels).
        has_fert = {"soil_moisture", "root_zone_temp"} & set(config.channels)
        self._fertigation: Optional[FertigationSignalState] = (
            FertigationSignalState(self._rng) if has_fert else None
        )

        # Episode engine owns all anomaly/demo-episode state.  Constructed
        # after the fertigation state to preserve the pre-split RNG draw
        # order at init (demo scheduling draws once when demo_mode is on).
        self._episodes = EpisodeEngine(self._rng, config)

    @staticmethod
    def _parse_start_time(
        value: Optional[Union[str, datetime]],
    ) -> Optional[datetime]:
        """Parse an optional start_time param (ISO string or datetime).

        When set, ``stream()`` advances timestamps logically per tick
        (start_time + n * interval) instead of reading the wall clock,
        making seeded runs fully deterministic.
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)

    # ------------------------------------------------------------------
    # Episode-state passthroughs
    #
    # Episode state is owned by the EpisodeEngine; these private properties
    # preserve the attribute surface that existing tests rely on so they
    # survive the refactor unchanged.  New code should use
    # ``self._episodes`` directly.
    # ------------------------------------------------------------------

    @property
    def _episode(self) -> Optional[_AnomalyEpisode]:
        return self._episodes.episode

    @_episode.setter
    def _episode(self, value: Optional[_AnomalyEpisode]) -> None:
        self._episodes.episode = value

    @property
    def _compound_episode(self) -> Optional[_CompoundEpisode]:
        return self._episodes.compound_episode

    @_compound_episode.setter
    def _compound_episode(self, value: Optional[_CompoundEpisode]) -> None:
        self._episodes.compound_episode = value

    @property
    def _demo_mode(self) -> bool:
        return self._episodes.demo_mode

    @property
    def _demo_next_tick(self) -> Optional[int]:
        return self._episodes.demo_next_tick

    @_demo_next_tick.setter
    def _demo_next_tick(self, value: Optional[int]) -> None:
        self._episodes.demo_next_tick = value

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Mock connection -- always succeeds."""
        self._is_connected = True

    async def close(self) -> None:
        """Mark the source as disconnected."""
        self._is_connected = False

    async def stream(self) -> AsyncIterator[SourceReading]:  # type: ignore[override]
        """Yield ``SourceReading`` objects at the configured interval.

        When ``start_time`` is set in params, timestamps advance logically
        (start_time + tick * interval) instead of reading the wall clock.
        This makes seeded runs bit-identical regardless of execution timing.
        """
        interval = self._config.poll_interval_seconds
        while self._is_connected:
            if self._start_time is not None:
                now = self._start_time + timedelta(
                    seconds=self._tick_count * interval,
                )
            else:
                now = datetime.now(timezone.utc)
            # Tick advances in BOTH clock modes: demo-episode scheduling is
            # tick-based, and wall-clock mode previously froze at tick 0,
            # silently disabling guaranteed demo anomalies in production.
            self._tick_count += 1
            channels = self._generate_channels(now)
            if channels:  # dropout episode may empty the dict
                yield SourceReading(
                    source_id=self._config.source_id,
                    vendor=self._config.vendor,
                    tenant_id=self._config.tenant_id,
                    greenhouse_id=self._config.greenhouse_id,
                    timestamp=now,
                    channels=channels,
                )
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def _generate_channels(self, ts: datetime) -> Dict[str, float]:
        """Build channel values for the given timestamp."""
        hour_frac = _hour_fraction(ts)
        raw: Dict[str, float] = {}

        # Standard diurnal signals.
        for ch in self._config.channels:
            if ch in ("soil_moisture", "root_zone_temp"):
                continue  # handled by fertigation state below
            raw[ch] = self._base_signal(ch, hour_frac, raw)

        # Fertigation channels (stateful sawtooth + damped diurnal).
        if self._fertigation is not None:
            fert = self._fertigation.generate(hour_frac)
            for ch in self._config.channels:
                if ch in fert:
                    raw[ch] = fert[ch]

        # Episode injection (demo compound + random single-channel).
        shaped = self._episodes.process(raw, self._tick_count)
        return _clamp_and_sanitize(shaped)

    def _base_signal(
        self,
        channel: str,
        hour_frac: float,
        prior: Dict[str, float],
    ) -> float:
        """Compute the diurnal + noise base signal for one channel."""
        if channel == "temperature":
            mid = (TEMP_RANGE[0] + TEMP_RANGE[1]) / 2.0
            amp = (TEMP_RANGE[1] - TEMP_RANGE[0]) / AMPLITUDE_DIVISOR
            return mid + amp * _diurnal_sine(hour_frac) + self._rng.gauss(0, TEMP_NOISE_STD)

        if channel == "humidity":
            mid = (HUMIDITY_RANGE[0] + HUMIDITY_RANGE[1]) / 2.0
            amp = (HUMIDITY_RANGE[1] - HUMIDITY_RANGE[0]) / AMPLITUDE_DIVISOR
            base = mid - amp * _diurnal_sine(hour_frac)
            temp_val = prior.get("temperature")
            if temp_val is not None:
                temp_mid = (TEMP_RANGE[0] + TEMP_RANGE[1]) / 2.0
                base -= HUMIDITY_COUPLING_FACTOR * (temp_val - temp_mid)
            return base + self._rng.gauss(0, HUMIDITY_NOISE_STD)

        if channel == "co2":
            mid = (CO2_RANGE[0] + CO2_RANGE[1]) / 2.0
            amp = (CO2_RANGE[1] - CO2_RANGE[0]) / AMPLITUDE_DIVISOR
            dip = amp * max(0.0, _diurnal_sine(hour_frac))
            return mid - dip + self._rng.gauss(0, CO2_NOISE_STD)

        if channel == "light":
            base = LIGHT_AMPLITUDE * max(0.0, _diurnal_sine(hour_frac))
            return base + self._rng.gauss(0, LIGHT_NOISE_STD)

        return self._rng.gauss(0, 1.0)
