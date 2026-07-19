"""Mock SCADA data source for development and integration testing.

Produces deterministic diurnal sine signals (keyed to UTC wall-clock hour)
with Gaussian noise and periodic anomaly episodes (spike, drift, stuck,
dropout) for the four canonical greenhouse channels.

Also provides a ``mock_fertigation`` source with soil_moisture (sawtooth
irrigation pattern) and root_zone_temp (damped diurnal).
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, ClassVar, Dict, List, Optional, Set, Tuple, Union

from src.config import (
    AMPLITUDE_DIVISOR,
    CO2_NOISE_STD,
    CO2_RANGE,
    HUMIDITY_COUPLING_FACTOR,
    HUMIDITY_NOISE_STD,
    HUMIDITY_RANGE,
    LIGHT_AMPLITUDE,
    LIGHT_NOISE_STD,
    MOCK_SCADA_ANOMALY_RATE,
    MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS,
    MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS,
    MOCK_SCADA_DEMO_MODE,
    MOCK_SCADA_INTERVAL_SECONDS,
    SENSOR_NAMES,
    TEMP_NOISE_STD,
    TEMP_RANGE,
)
from src.live.events import CHANNEL_BOUNDS, SourceReading
from src.live.sources.base import LiveSourceConnector, SourceConfig
from src.live.sources.fertigation import FertigationSignalState

_TWO_PI: float = 2.0 * math.pi

# The four channels consumed by the ML scorer (LSTM-VAE).
ML_CHANNELS: Set[str] = set(SENSOR_NAMES)

# Anomaly episode tick durations
_SPIKE_TICKS: Tuple[int, int] = (3, 6)
_DRIFT_TICKS: int = 60
_STUCK_TICKS: int = 30

# Spike magnitude range (multiples of the channel's noise std)
_SPIKE_SIGMA_MIN: float = 5.0
_SPIKE_SIGMA_MAX: float = 8.0

# Episode types that produce a score-visible signal (excludes dropout).
_SCORE_VISIBLE_EPISODES: Tuple[str, ...] = ("spike", "drift", "stuck")

# Per-channel noise standard deviations (reused from config)
_NOISE_STD: Dict[str, float] = {
    "temperature": TEMP_NOISE_STD,
    "humidity": HUMIDITY_NOISE_STD,
    "co2": CO2_NOISE_STD,
    "light": LIGHT_NOISE_STD,
}


def _hour_fraction(ts: datetime) -> float:
    """Return the fractional hour of day in UTC (0.0 to 24.0)."""
    return ts.hour + ts.minute / 60.0 + ts.second / 3600.0


def _diurnal_sine(hour_frac: float, phase_offset: float = 6.0) -> float:
    """Sine with a 24-hour period.  Default offset peaks at noon."""
    return math.sin(_TWO_PI * (hour_frac - phase_offset) / 24.0)


class _AnomalyEpisode:
    """Tracks an active anomaly episode on a single channel."""

    __slots__ = (
        "episode_type", "channel", "remaining_ticks",
        "magnitude", "accumulated",
    )

    def __init__(
        self,
        episode_type: str,
        channel: str,
        remaining_ticks: int,
        magnitude: float,
        accumulated: float,
    ) -> None:
        self.episode_type = episode_type
        self.channel = channel
        self.remaining_ticks = remaining_ticks
        self.magnitude = magnitude
        self.accumulated = accumulated  # frozen value (stuck) / drift sum


class MockScadaSource(LiveSourceConnector):
    """Mock SCADA source producing synthetic diurnal greenhouse signals.

    Deterministic when ``SourceConfig.params["seed"]`` is set (uses a
    per-instance ``random.Random``).
    """

    vendor: ClassVar[str] = "mock"

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        seed = config.params.get("seed")
        self._rng = random.Random(seed)
        self._is_connected = False
        self._episode: Optional[_AnomalyEpisode] = None
        self._start_time: Optional[datetime] = self._parse_start_time(
            config.params.get("start_time"),
        )
        self._tick_count: int = 0

        # Fertigation signal state (only active when source carries those channels).
        has_fert = {"soil_moisture", "root_zone_temp"} & set(config.channels)
        self._fertigation: Optional[FertigationSignalState] = (
            FertigationSignalState(self._rng) if has_fert else None
        )

        # Demo-mode guaranteed episode scheduling.
        self._demo_mode: bool = bool(config.params.get("demo_mode", False))
        self._demo_next_tick: Optional[int] = None
        if self._demo_mode:
            self._schedule_demo_episode()

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
                self._tick_count += 1
            else:
                now = datetime.now(timezone.utc)
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

        # Demo-mode guaranteed episode injection.
        self._maybe_start_demo_episode(raw)

        self._maybe_start_episode(raw)
        result = self._apply_episode(raw)
        return _clamp_and_sanitize(result)

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

    # ------------------------------------------------------------------
    # Anomaly episodes
    # ------------------------------------------------------------------

    def _maybe_start_episode(self, raw: Dict[str, float]) -> None:
        """With probability ``MOCK_SCADA_ANOMALY_RATE``, start an episode."""
        if self._episode is not None:
            return
        if self._rng.random() >= MOCK_SCADA_ANOMALY_RATE:
            return
        if not raw:
            return

        kind = self._rng.choice(["spike", "drift", "stuck", "dropout"])
        channel = self._rng.choice(list(raw.keys()))
        noise = _NOISE_STD.get(channel, 1.0)

        if kind == "spike":
            ticks = self._rng.randint(*_SPIKE_TICKS)
            sigma = self._rng.uniform(_SPIKE_SIGMA_MIN, _SPIKE_SIGMA_MAX)
            mag = sigma * noise * self._rng.choice([-1, 1])
            self._episode = _AnomalyEpisode(kind, channel, ticks, mag, 0.0)
        elif kind == "drift":
            total = self._rng.uniform(3, 6) * noise * self._rng.choice([-1, 1])
            per_tick = total / _DRIFT_TICKS
            self._episode = _AnomalyEpisode(kind, channel, _DRIFT_TICKS, per_tick, 0.0)
        elif kind == "stuck":
            self._episode = _AnomalyEpisode(kind, channel, _STUCK_TICKS, 0.0, raw[channel])
        elif kind == "dropout":
            ticks = self._rng.randint(5, 15)
            self._episode = _AnomalyEpisode(kind, channel, ticks, 0.0, 0.0)

    def _apply_episode(self, raw: Dict[str, float]) -> Dict[str, float]:
        """Apply the active episode to raw channels and tick down."""
        if self._episode is None:
            return raw

        ep = self._episode
        ep.remaining_ticks -= 1
        result = dict(raw)

        if ep.episode_type == "spike" and ep.channel in result:
            result[ep.channel] += ep.magnitude
        elif ep.episode_type == "drift" and ep.channel in result:
            ep.accumulated += ep.magnitude
            result[ep.channel] += ep.accumulated
        elif ep.episode_type == "stuck" and ep.channel in result:
            result[ep.channel] = ep.accumulated
        elif ep.episode_type == "dropout":
            result.pop(ep.channel, None)

        if ep.remaining_ticks <= 0:
            self._episode = None

        return result

    # ------------------------------------------------------------------
    # Demo-mode guaranteed episodes
    # ------------------------------------------------------------------

    def _schedule_demo_episode(self) -> None:
        """Schedule the next guaranteed episode tick using config interval."""
        interval = self._config.poll_interval_seconds
        min_ticks = max(1, int(MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS / interval))
        max_ticks = max(min_ticks + 1, int(MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS / interval))
        self._demo_next_tick = self._tick_count + self._rng.randint(min_ticks, max_ticks)

    def _maybe_start_demo_episode(self, raw: Dict[str, float]) -> None:
        """If demo mode is due, force a score-visible episode on an ML channel."""
        if not self._demo_mode:
            return
        if self._demo_next_tick is None:
            return
        if self._tick_count < self._demo_next_tick:
            return
        if self._episode is not None:
            # An episode is already active; reschedule slightly ahead.
            self._demo_next_tick = self._tick_count + 1
            return

        # Pick a random canonical ML channel present in this source.
        ml_channels = [ch for ch in raw if ch in ML_CHANNELS]
        if not ml_channels:
            # This source has no ML channels; skip.
            self._schedule_demo_episode()
            return

        channel = self._rng.choice(ml_channels)
        noise = _NOISE_STD.get(channel, 1.0)
        kind = self._rng.choice(list(_SCORE_VISIBLE_EPISODES))

        if kind == "spike":
            ticks = self._rng.randint(*_SPIKE_TICKS)
            sigma = self._rng.uniform(_SPIKE_SIGMA_MIN, _SPIKE_SIGMA_MAX)
            mag = sigma * noise * self._rng.choice([-1, 1])
            self._episode = _AnomalyEpisode(kind, channel, ticks, mag, 0.0)
        elif kind == "drift":
            total = self._rng.uniform(3, 6) * noise * self._rng.choice([-1, 1])
            per_tick = total / _DRIFT_TICKS
            self._episode = _AnomalyEpisode(kind, channel, _DRIFT_TICKS, per_tick, 0.0)
        elif kind == "stuck":
            self._episode = _AnomalyEpisode(kind, channel, _STUCK_TICKS, 0.0, raw[channel])

        self._schedule_demo_episode()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _clamp_and_sanitize(channels: Dict[str, float]) -> Dict[str, float]:
    """Clamp to ``CHANNEL_BOUNDS`` and drop non-finite values."""
    out: Dict[str, float] = {}
    for key, val in channels.items():
        if not math.isfinite(val):
            continue
        lo, hi = CHANNEL_BOUNDS.get(key, (val, val))
        out[key] = max(lo, min(hi, val))
    return out


def build_mock_sources_for_scope(
    tenant_id: str,
    greenhouse_id: str,
) -> List[MockScadaSource]:
    """Build three complementary mock sources for a tenant/greenhouse scope.

    Returns
    -------
    list[MockScadaSource]
        - ``mock_climate``: temperature, humidity, light (priority 10).
          Has demo-mode enabled when ``MOCK_SCADA_DEMO_MODE`` is True.
        - ``mock_co2``: co2 only (priority 20, 1.5x interval offset).
        - ``mock_fertigation``: soil_moisture, root_zone_temp (priority 30,
          2x interval offset).

    The three sources prove multi-source merge with different priorities
    and cadences.
    """
    climate = SourceConfig(
        source_id=f"{tenant_id}_{greenhouse_id}_mock_climate",
        vendor="mock",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        channels=["temperature", "humidity", "light"],
        priority=10,
        poll_interval_seconds=MOCK_SCADA_INTERVAL_SECONDS,
        params={"seed": 42, "demo_mode": MOCK_SCADA_DEMO_MODE},
    )
    co2 = SourceConfig(
        source_id=f"{tenant_id}_{greenhouse_id}_mock_co2",
        vendor="mock",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        channels=["co2"],
        priority=20,
        poll_interval_seconds=MOCK_SCADA_INTERVAL_SECONDS * 1.5,
        params={"seed": 137},
    )
    fertigation = SourceConfig(
        source_id=f"{tenant_id}_{greenhouse_id}_mock_fertigation",
        vendor="mock",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        channels=["soil_moisture", "root_zone_temp"],
        priority=30,
        poll_interval_seconds=MOCK_SCADA_INTERVAL_SECONDS * 2,
        params={"seed": 256},
    )
    return [MockScadaSource(climate), MockScadaSource(co2), MockScadaSource(fertigation)]
