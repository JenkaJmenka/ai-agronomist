"""Anomaly-episode engine for the mock SCADA source.

Owns all episode state for one :class:`~src.live.sources.mock.MockScadaSource`:
random single-channel episodes (spike, drift, stuck, dropout), compound
multi-channel demo episodes, and demo-mode scheduling.  Extracted from
``mock.py`` so episode logic can be unit-tested directly instead of by
poking private source state.

Determinism contract: the engine shares the source's seeded
``random.Random`` and performs RNG draws in exactly the same order as the
pre-split implementation, so seeded runs remain byte-identical.
"""

from __future__ import annotations

import random
from typing import Dict, Optional, Tuple

from src.config import (
    CO2_NOISE_STD,
    HUMIDITY_NOISE_STD,
    LIGHT_NOISE_STD,
    MOCK_SCADA_ANOMALY_RATE,
    MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS,
    MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS,
    MOCK_SCADA_DEMO_COMPOUND_MAX_TICKS,
    MOCK_SCADA_DEMO_COMPOUND_MIN_TICKS,
    MOCK_SCADA_DEMO_SPIKE_SIGMA,
    TEMP_NOISE_STD,
)
from src.live.events import ML_CHANNELS
from src.live.sources.base import SourceConfig
from src.live.sources.fertigation import _RZT_NOISE_STD, _SM_NOISE_STD

# Anomaly episode tick durations
_SPIKE_TICKS: Tuple[int, int] = (3, 6)
_DRIFT_TICKS: int = 60
_STUCK_TICKS: int = 30

# Spike magnitude range (multiples of the channel's noise std)
_SPIKE_SIGMA_MIN: float = 5.0
_SPIKE_SIGMA_MAX: float = 8.0

# Drift total-magnitude range (multiples of the channel's noise std).
_DRIFT_SIGMA_RANGE: Tuple[float, float] = (3.0, 6.0)

# Dropout episode duration range (ticks).
_DROPOUT_TICKS: Tuple[int, int] = (5, 15)

# Episode types that produce a score-visible signal (excludes dropout).
_SCORE_VISIBLE_EPISODES: Tuple[str, ...] = ("spike", "drift", "stuck")

# Per-channel noise standard deviations (reused from config and fertigation
# constants).  Every channel that can receive random anomaly episodes MUST
# have an entry here — the fallback of 1.0 uses absolute-unit magnitudes
# which produce unrealistic episode sizes for narrow-noise channels like
# root_zone_temp (noise std 0.15).
_NOISE_STD: Dict[str, float] = {
    "temperature": TEMP_NOISE_STD,
    "humidity": HUMIDITY_NOISE_STD,
    "co2": CO2_NOISE_STD,
    "light": LIGHT_NOISE_STD,
    "soil_moisture": _SM_NOISE_STD,
    "root_zone_temp": _RZT_NOISE_STD,
}


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


class _CompoundEpisode:
    """Multi-channel anomaly episode for demo mode.

    Simultaneously offsets 2-3 channels in the same direction to break
    learned inverse couplings (e.g. temp-humidity) and reliably cross the
    scoring threshold.
    """

    __slots__ = ("channel_magnitudes", "remaining_ticks")

    def __init__(
        self,
        channel_magnitudes: Dict[str, float],
        remaining_ticks: int,
    ) -> None:
        self.channel_magnitudes = channel_magnitudes
        self.remaining_ticks = remaining_ticks


class EpisodeEngine:
    """Episode state machine collaborating with ``MockScadaSource``.

    Parameters
    ----------
    rng:
        The source's seeded ``random.Random`` (shared, not copied — draw
        order across signal generation and episodes must be preserved).
    config:
        The source's ``SourceConfig`` (poll interval and demo-mode flag).
    """

    def __init__(self, rng: random.Random, config: SourceConfig) -> None:
        self._rng = rng
        self._config = config
        self.episode: Optional[_AnomalyEpisode] = None
        self.compound_episode: Optional[_CompoundEpisode] = None
        self.demo_mode: bool = bool(config.params.get("demo_mode", False))
        self.demo_next_tick: Optional[int] = None
        if self.demo_mode:
            self.schedule_demo_episode(tick=0)

    def process(self, raw: Dict[str, float], tick: int) -> Dict[str, float]:
        """Run one tick of episode logic over *raw* channel values.

        Order matters (demo scheduling before random episodes, single
        episode applied before compound) — it defines the RNG draw order
        that seeded determinism tests pin down.
        """
        self.maybe_start_demo_episode(raw, tick)
        self.maybe_start_episode(raw)
        applied = self.apply_episode(raw)
        return self.apply_compound_episode(applied)

    # ------------------------------------------------------------------
    # Random anomaly episodes
    # ------------------------------------------------------------------

    def maybe_start_episode(self, raw: Dict[str, float]) -> None:
        """With probability ``MOCK_SCADA_ANOMALY_RATE``, start an episode."""
        if self.episode is not None:
            return
        if self.compound_episode is not None:
            return  # compound episode active, skip random episode
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
            self.episode = _AnomalyEpisode(kind, channel, ticks, mag, 0.0)
        elif kind == "drift":
            total = self._rng.uniform(*_DRIFT_SIGMA_RANGE) * noise * self._rng.choice([-1, 1])
            per_tick = total / _DRIFT_TICKS
            self.episode = _AnomalyEpisode(kind, channel, _DRIFT_TICKS, per_tick, 0.0)
        elif kind == "stuck":
            self.episode = _AnomalyEpisode(kind, channel, _STUCK_TICKS, 0.0, raw[channel])
        elif kind == "dropout":
            ticks = self._rng.randint(*_DROPOUT_TICKS)
            self.episode = _AnomalyEpisode(kind, channel, ticks, 0.0, 0.0)

    def apply_episode(self, raw: Dict[str, float]) -> Dict[str, float]:
        """Apply the active episode to raw channels and tick down."""
        if self.episode is None:
            return raw

        ep = self.episode
        ep.remaining_ticks -= 1
        applied = dict(raw)

        if ep.episode_type == "spike" and ep.channel in applied:
            applied[ep.channel] += ep.magnitude
        elif ep.episode_type == "drift" and ep.channel in applied:
            ep.accumulated += ep.magnitude
            applied[ep.channel] += ep.accumulated
        elif ep.episode_type == "stuck" and ep.channel in applied:
            applied[ep.channel] = ep.accumulated
        elif ep.episode_type == "dropout":
            applied.pop(ep.channel, None)

        if ep.remaining_ticks <= 0:
            self.episode = None

        return applied

    def apply_compound_episode(self, raw: Dict[str, float]) -> Dict[str, float]:
        """Apply the active compound episode offsets and tick down."""
        if self.compound_episode is None:
            return raw

        ep = self.compound_episode
        ep.remaining_ticks -= 1
        applied = dict(raw)

        for ch, magnitude in ep.channel_magnitudes.items():
            if ch in applied:
                applied[ch] += magnitude

        if ep.remaining_ticks <= 0:
            self.compound_episode = None

        return applied

    # ------------------------------------------------------------------
    # Demo-mode guaranteed episodes
    # ------------------------------------------------------------------

    def schedule_demo_episode(self, tick: int) -> None:
        """Schedule the next guaranteed episode tick using config interval."""
        interval = self._config.poll_interval_seconds
        min_ticks = max(1, int(MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS / interval))
        max_ticks = max(min_ticks + 1, int(MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS / interval))
        self.demo_next_tick = tick + self._rng.randint(min_ticks, max_ticks)

    def maybe_start_demo_episode(self, raw: Dict[str, float], tick: int) -> None:
        """If demo mode is due, force a compound multi-channel episode.

        Creates a compound episode that simultaneously offsets 2-3 ML channels
        in the same direction (breaking learned inverse couplings) with
        ``MOCK_SCADA_DEMO_SPIKE_SIGMA``-sized magnitudes sustained for
        ``MOCK_SCADA_DEMO_COMPOUND_MIN/MAX_TICKS`` — empirically tuned so the
        baseline LSTM-VAE detector reliably crosses ``LIVE_ANOMALY_THRESHOLD``.
        """
        if not self.demo_mode:
            return
        if self.demo_next_tick is None:
            return
        if tick < self.demo_next_tick:
            return
        if self.episode is not None or self.compound_episode is not None:
            # An episode is already active; reschedule slightly ahead.
            self.demo_next_tick = tick + 1
            return

        # Pick 2-3 canonical ML channels present in this source.
        ml_channels = [ch for ch in raw if ch in ML_CHANNELS]
        if not ml_channels:
            # This source has no ML channels; skip.
            self.schedule_demo_episode(tick)
            return

        # Select 2-3 channels (or all available if fewer).
        n_channels = min(len(ml_channels), self._rng.randint(2, 3))
        chosen = self._rng.sample(ml_channels, n_channels)

        # Direction: all channels offset in the SAME direction to break
        # the inverse coupling the model learned (e.g. temp + humidity both up).
        direction = self._rng.choice([-1, 1])

        # Build per-channel magnitudes using MOCK_SCADA_DEMO_SPIKE_SIGMA.
        channel_magnitudes: Dict[str, float] = {}
        for ch in chosen:
            noise = _NOISE_STD.get(ch, 1.0)
            channel_magnitudes[ch] = direction * MOCK_SCADA_DEMO_SPIKE_SIGMA * noise

        # Duration in ticks (env-configurable).
        ticks = self._rng.randint(
            MOCK_SCADA_DEMO_COMPOUND_MIN_TICKS,
            MOCK_SCADA_DEMO_COMPOUND_MAX_TICKS,
        )

        self.compound_episode = _CompoundEpisode(channel_magnitudes, ticks)
        self.schedule_demo_episode(tick)
