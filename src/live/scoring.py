"""Live anomaly scoring — one ``LiveScorer`` per (tenant_id, greenhouse_id).

Maintains a sliding window of sensor readings, normalises with the same
min-max scheme the batch inference path uses, runs the tenant's active
LSTM-VAE detector, and publishes anomaly / enrichment events to the hub.

Detector resolution and enrichment are delegated to :class:`DetectorResolver`
(:mod:`src.live.detector_resolver`) and :class:`EnrichmentRunner`
(:mod:`src.live.enrichment`) — the DI seam that lets both be unit-tested
without a real database or checkpoint file.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Set

import numpy as np

from src.config import (
    LIVE_ANOMALY_REEMIT_SECONDS,
    LIVE_ANOMALY_THRESHOLD,
    LIVE_FORWARD_FILL_MAX_TICKS,
    MOCK_SCADA_INTERVAL_SECONDS,
    SEQ_LEN,
)
from src.live.attribution import (
    CHANNEL_ORDER,
    compute_channel_attribution,
    forward_pass_sync,
)
from src.live.detector_resolver import DetectorResolver
from src.live.enrichment import EnrichmentRunner
from src.live.events import LiveAnomalyEvent, LiveReadingEvent
from src.live.hub import LiveHub
from src.models.data_utils import SENSOR_RANGES, normalize_data
from src.models.noise_detector import NoiseDetector

logger = logging.getLogger(__name__)

# Recent physical windows the scorer did not flag, kept so enrichment can
# calibrate the noise detector. No existing config constant counts clean
# windows: ``SEQ_LEN`` is the window length and ``LIVE_REPLAY_BUFFER_SIZE``
# is Redis stream entries. The cap stays in this module; moving it to
# ``src/config.py`` does not change which windows are admitted.
# Stored windows are one full slide apart (every ``SEQ_LEN`` scored steps)
# so a noise fault under the LSTM threshold cannot fill the ring with
# overlapping copies. A fixed-baseline noise hit on temperature, humidity,
# or CO2 is refused. Light is ignored. The anomalous window is never appended.
CLEAN_WINDOW_HISTORY: int = 32
_CLIMATE_NOISE_CHANNELS: tuple[str, ...] = ("temperature", "humidity", "co2")


def _fixed_baseline_noise_hit(window_array: np.ndarray) -> bool:
    """True when the cold-start noise detector flags a climate channel.

    The detector is local: no custom ``baseline_variance``, default ratio.
    Light is not a precision fault and is not checked.
    """
    if window_array.ndim != 2:
        return False
    detector = NoiseDetector()
    width = window_array.shape[1]
    for name in _CLIMATE_NOISE_CHANNELS:
        index = CHANNEL_ORDER.index(name)
        if index >= width:
            continue
        series = np.asarray(window_array[:, index], dtype=float)
        if detector.detect(series, name):
            return True
    return False


class LiveScorer:
    """Real-time anomaly scorer for one (tenant_id, greenhouse_id) scope.

    Public API (the runtime wires by this contract):
        ``__init__(tenant_id, greenhouse_id, hub, threshold)``
        ``async score(event) -> LiveReadingEvent``
        ``async close() -> None``

    ``resolver``/``enrichment`` are optional DI seams for tests — production
    callers get real ``DetectorResolver``/``EnrichmentRunner`` instances
    built automatically.
    """

    def __init__(
        self,
        tenant_id: str,
        greenhouse_id: str,
        hub: LiveHub,
        threshold: float = LIVE_ANOMALY_THRESHOLD,
        emit_interval: float = MOCK_SCADA_INTERVAL_SECONDS,
        resolver: Optional[DetectorResolver] = None,
        enrichment: Optional[EnrichmentRunner] = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._greenhouse_id = greenhouse_id
        self._hub = hub
        self._threshold = threshold
        self._emit_interval = emit_interval

        self._resolver = resolver or DetectorResolver(tenant_id)
        self._enrichment = enrichment or EnrichmentRunner(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            hub=hub,
            resolver=self._resolver,
        )

        # Sliding window of raw [temp, humidity, co2, light] vectors.
        self._window: Deque[List[float]] = deque(maxlen=SEQ_LEN)
        self._window_timestamps: Deque[datetime] = deque(maxlen=SEQ_LEN)
        # Physical windows with score < threshold. Not the window in flight.
        self._clean_windows: Deque[np.ndarray] = deque(maxlen=CLEAN_WINDOW_HISTORY)
        # Non-anomalous scored windows since construction. Admission keeps
        # one window per full slide so consecutive stores do not overlap.
        self._clean_scored_count: int = 0

        # Forward-fill: last known value per channel.
        self._last_known: Dict[str, float] = {}
        self._channels_seen: Set[str] = set()
        # Per-channel monotonic timestamp of last update (for staleness cutoff).
        self._channel_last_update: Dict[str, float] = {}

        # Anomaly episode state.
        self._in_episode: bool = False
        self._episode_id: str = ""
        self._last_anomaly_emit: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def detector_state(self) -> str:
        """The scope's detector state, surfaced on ``/api/live/status``.

        Delegates to the resolver so there is one source of truth; see
        ``src/live/detector_resolver.py`` for the vocabulary.
        """
        return self._resolver.state

    async def score(self, event: LiveReadingEvent) -> LiveReadingEvent:
        """Score *event*; returns a copy with ``anomaly_score`` set once warm."""
        vector = self._extract_vector(event)
        if vector is None:
            return event  # channel(s) never seen — not warm

        self._window.append(vector)
        self._window_timestamps.append(event.timestamp)

        if len(self._window) < SEQ_LEN:
            return event  # window not full

        detector = await self._resolver.resolve()
        if detector is None:
            return event

        window_array = np.array(list(self._window), dtype=np.float32)
        normalized = normalize_data(window_array, SENSOR_RANGES)

        try:
            scaled_score, per_channel_errors = await asyncio.to_thread(
                forward_pass_sync, detector, normalized,
            )
        except Exception:
            logger.warning(
                "Forward pass failed for %s/%s",
                self._tenant_id, self._greenhouse_id, exc_info=True,
            )
            return event

        if not math.isfinite(scaled_score):
            logger.warning(
                "Non-finite score for %s/%s: %r",
                self._tenant_id, self._greenhouse_id, scaled_score,
            )
            return event

        scored = event.model_copy(update={"anomaly_score": scaled_score})
        await self._handle_episode(
            scaled_score, per_channel_errors, scored, window_array,
        )
        return scored

    async def close(self) -> None:
        """Cancel and await all in-flight enrichment tasks."""
        await self._enrichment.close()

    # ------------------------------------------------------------------
    # Vector extraction
    # ------------------------------------------------------------------

    def _extract_vector(self, event: LiveReadingEvent) -> Optional[List[float]]:
        """Build ``[temp, humidity, co2, light]``, forward-filling gaps.

        Returns ``None`` if any channel has NEVER been seen (not warm), or
        if any channel has exceeded the forward-fill staleness cutoff
        (``LIVE_FORWARD_FILL_MAX_TICKS * emit_interval``).
        """
        now_mono = time.monotonic()
        for ch_name, ch_val in event.channels.items():
            if ch_name in CHANNEL_ORDER:
                self._last_known[ch_name] = ch_val
                self._channels_seen.add(ch_name)
                self._channel_last_update[ch_name] = now_mono

        if not all(ch in self._channels_seen for ch in CHANNEL_ORDER):
            return None

        # Staleness cutoff: if a channel hasn't been updated for too long,
        # treat the window as not-warm rather than fabricating input.
        max_stale = LIVE_FORWARD_FILL_MAX_TICKS * self._emit_interval
        for ch in CHANNEL_ORDER:
            last_update = self._channel_last_update.get(ch)
            if last_update is not None and (now_mono - last_update) > max_stale:
                return None

        return [self._last_known[ch] for ch in CHANNEL_ORDER]

    # ------------------------------------------------------------------
    # Episode management
    # ------------------------------------------------------------------

    async def _handle_episode(
        self,
        score: float,
        per_channel_errors: np.ndarray,
        event: LiveReadingEvent,
        window_array: np.ndarray,
    ) -> None:
        """Start / debounced re-emit / end anomaly episodes."""
        now = time.monotonic()
        is_anomalous = score >= self._threshold

        if is_anomalous and not self._in_episode:
            # New episode — generate a unique episode ID for correlation.
            # Snapshot before any await: this window is anomalous and is
            # not in the ring. An empty ring stays None so enrichment keeps
            # the fixed noise map.
            clean_history = tuple(self._clean_windows) or None
            self._in_episode = True
            self._episode_id = uuid.uuid4().hex
            self._last_anomaly_emit = now
            await self._publish_anomaly(event, score, per_channel_errors)
            self._enrichment.spawn(
                window_array,
                self._episode_id,
                noise_clean_windows=clean_history,
            )

        elif is_anomalous and self._in_episode:
            if (now - self._last_anomaly_emit) >= LIVE_ANOMALY_REEMIT_SECONDS:
                self._last_anomaly_emit = now
                await self._publish_anomaly(event, score, per_channel_errors)

        elif not is_anomalous and self._in_episode:
            self._in_episode = False

        if not is_anomalous:
            self._remember_clean_window(window_array)

    def _remember_clean_window(self, window_array: np.ndarray) -> None:
        """Keep a non-overlapping physical window the scorer did not flag.

        The copy is owned by the ring. Enrichment snapshots it later and
        must not see a window whose score was at or above the threshold.
        Stride-1 windows overlap by ``SEQ_LEN - 1`` samples; only the first
        eligible window and one window per full slide are stored. A
        fixed-baseline noise hit on a climate channel is refused so that
        fault cannot become the calibration median.
        """
        scored_index = self._clean_scored_count
        self._clean_scored_count += 1
        if scored_index % SEQ_LEN != 0:
            return
        if _fixed_baseline_noise_hit(window_array):
            return
        self._clean_windows.append(
            np.array(window_array, dtype=np.float32, copy=True),
        )

    async def _publish_anomaly(
        self,
        event: LiveReadingEvent,
        score: float,
        per_channel_errors: np.ndarray,
    ) -> None:
        """Publish a ``LiveAnomalyEvent`` to the hub."""
        affected, reasoning = compute_channel_attribution(per_channel_errors)
        window_start = self._window_timestamps[0]
        window_end = self._window_timestamps[-1]

        try:
            await self._hub.publish(
                self._tenant_id,
                self._greenhouse_id,
                "anomaly",
                LiveAnomalyEvent(
                    seq=self._episode_id,
                    timestamp=event.timestamp,
                    tenant_id=self._tenant_id,
                    greenhouse_id=self._greenhouse_id,
                    score=score,
                    threshold=self._threshold,
                    affected_channels=affected,
                    reasoning=reasoning,
                    window_start=window_start,
                    window_end=window_end,
                ),
            )
        except Exception:
            logger.warning(
                "Anomaly event publish failed for %s/%s",
                self._tenant_id, self._greenhouse_id, exc_info=True,
            )
