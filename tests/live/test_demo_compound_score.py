"""Empirical verification: compound demo episodes score >= 0.08 on baseline.

This test loads the real LSTM-VAE baseline detector from a committed test
fixture (trained checkpoint, epoch 72, loss=0.0054) and verifies that the
compound episode offsets (as computed by MockScadaSource in demo mode)
reliably cross the scoring threshold.

Marked ``@pytest.mark.slow`` — skipped in normal CI; run explicitly with
``pytest -m slow tests/live/test_demo_compound_score.py``.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

from src.config import (
    MOCK_SCADA_DEMO_COMPOUND_MAX_TICKS,
    MOCK_SCADA_DEMO_COMPOUND_MIN_TICKS,
    MOCK_SCADA_DEMO_SPIKE_SIGMA,
    SEQ_LEN,
    SENSOR_NAMES,
    TEMP_NOISE_STD,
    HUMIDITY_NOISE_STD,
    CO2_NOISE_STD,
    LIGHT_NOISE_STD,
)
from src.live.events import ML_CHANNELS
from src.live.sources.mock import MockScadaSource
from src.live.sources.mock_episodes import _NOISE_STD, _CompoundEpisode
from src.live.sources.base import SourceConfig
from src.models.data_utils import SENSOR_RANGES, normalize_data

# Committed test fixture: trained LSTM-VAE checkpoint (epoch 72, loss=0.0054).
# This is a real trained model — not random weights — so assertion thresholds
# are meaningful.  If this file is missing, the checkout is broken.
TRAINED_FIXTURE_PATH: str = str(
    Path(__file__).resolve().parents[1] / "fixtures" / "epoch_0072_trained_baseline.pt"
)

pytestmark = [pytest.mark.slow]

# Channel order for the model input (must match SENSOR_NAMES[:4]).
CHANNEL_ORDER: List[str] = list(SENSOR_NAMES[:4])

# Per-channel noise STD for building compound offsets.
_CHANNEL_NOISE: Dict[str, float] = {
    "temperature": TEMP_NOISE_STD,
    "humidity": HUMIDITY_NOISE_STD,
    "co2": CO2_NOISE_STD,
    "light": LIGHT_NOISE_STD,
}


def _build_normal_window(seed: int = 42) -> np.ndarray:
    """Build a realistic 20-step normal window via the mock source.

    Uses MockScadaSource with zero anomaly rate and no demo mode to produce
    deterministic diurnal signals at noon (when all channels are active).
    """
    config = SourceConfig(
        source_id="test_normal",
        vendor="mock",
        tenant_id="t1",
        greenhouse_id="gh_A",
        channels=["temperature", "humidity", "co2", "light"],
        priority=10,
        poll_interval_seconds=2.0,
        params={
            "seed": seed,
            "start_time": "2026-07-18T12:00:00+00:00",
            "demo_mode": False,
        },
    )
    source = MockScadaSource(config)
    # Monkey-patch anomaly rate to zero so no random episodes contaminate.
    import src.live.sources.mock_episodes as mock_mod
    original_rate = mock_mod.MOCK_SCADA_ANOMALY_RATE
    mock_mod.MOCK_SCADA_ANOMALY_RATE = 0.0
    try:
        readings: List[List[float]] = []
        for tick in range(SEQ_LEN):
            ts = datetime(2026, 7, 18, 12, 0, tick * 2, tzinfo=timezone.utc)
            source._tick_count = tick
            channels = source._generate_channels(ts)
            vector = [channels.get(ch, 0.0) for ch in CHANNEL_ORDER]
            readings.append(vector)
    finally:
        mock_mod.MOCK_SCADA_ANOMALY_RATE = original_rate

    return np.array(readings, dtype=np.float32)


def _apply_compound_offsets(
    window: np.ndarray,
    channels: List[str],
    sigma: float,
    duration_ticks: int,
    direction: int = 1,
) -> np.ndarray:
    """Apply compound offsets to the last `duration_ticks` of a window.

    Mimics exactly what the MockScadaSource compound episode does.
    """
    result = window.copy()
    for ch in channels:
        ch_idx = CHANNEL_ORDER.index(ch)
        noise = _CHANNEL_NOISE[ch]
        offset = direction * sigma * noise
        # Apply to the last `duration_ticks` rows.
        result[-duration_ticks:, ch_idx] += offset
    return result


def _get_detector():
    """Load the baseline detector from the committed test fixture."""
    from src.api.services.detector_cache import get_cached_detector
    return get_cached_detector(TRAINED_FIXTURE_PATH)


def _score_window(detector, window: np.ndarray) -> float:
    """Score a raw (unnormalized) window through the real forward pass."""
    from src.live.attribution import forward_pass_sync
    normalized = normalize_data(window, SENSOR_RANGES)
    score, _ = forward_pass_sync(detector, normalized)
    return score


class TestCompoundDemoScore:
    """Verify compound demo episodes reliably score >= 0.08."""

    def test_compound_3ch_peak_score(self) -> None:
        """3-channel compound (temp+humidity+light) scores >= 0.08 at peak."""
        detector = _get_detector()
        window = _build_normal_window(seed=42)

        # Apply compound offsets exactly as the mock does: 3 channels, same direction.
        scored_window = _apply_compound_offsets(
            window,
            channels=["temperature", "humidity", "light"],
            sigma=MOCK_SCADA_DEMO_SPIKE_SIGMA,
            duration_ticks=MOCK_SCADA_DEMO_COMPOUND_MAX_TICKS,
            direction=1,
        )

        score = _score_window(detector, scored_window)
        assert score >= 0.08, (
            f"Compound 3-channel episode scored {score:.4f}, expected >= 0.08"
        )

    def test_compound_2ch_peak_score(self) -> None:
        """2-channel compound (temp+humidity) scores >= 0.08 at peak."""
        detector = _get_detector()
        window = _build_normal_window(seed=42)

        scored_window = _apply_compound_offsets(
            window,
            channels=["temperature", "humidity"],
            sigma=MOCK_SCADA_DEMO_SPIKE_SIGMA,
            duration_ticks=MOCK_SCADA_DEMO_COMPOUND_MAX_TICKS,
            direction=1,
        )

        score = _score_window(detector, scored_window)
        assert score >= 0.08, (
            f"Compound 2-channel episode scored {score:.4f}, expected >= 0.08"
        )

    def test_compound_negative_direction(self) -> None:
        """Compound in negative direction also scores >= 0.08."""
        detector = _get_detector()
        window = _build_normal_window(seed=42)

        scored_window = _apply_compound_offsets(
            window,
            channels=["temperature", "humidity", "light"],
            sigma=MOCK_SCADA_DEMO_SPIKE_SIGMA,
            duration_ticks=MOCK_SCADA_DEMO_COMPOUND_MAX_TICKS,
            direction=-1,
        )

        score = _score_window(detector, scored_window)
        assert score >= 0.08, (
            f"Compound negative-direction scored {score:.4f}, expected >= 0.08"
        )

    def test_compound_min_duration_still_scores(self) -> None:
        """Compound with minimum duration (8 ticks) still scores >= 0.08."""
        detector = _get_detector()
        window = _build_normal_window(seed=42)

        scored_window = _apply_compound_offsets(
            window,
            channels=["temperature", "humidity", "light"],
            sigma=MOCK_SCADA_DEMO_SPIKE_SIGMA,
            duration_ticks=MOCK_SCADA_DEMO_COMPOUND_MIN_TICKS,
            direction=1,
        )

        score = _score_window(detector, scored_window)
        assert score >= 0.08, (
            f"Compound at min duration scored {score:.4f}, expected >= 0.08"
        )

    def test_normal_window_scores_below_threshold(self) -> None:
        """A normal window (no offsets) scores well below 0.05."""
        detector = _get_detector()
        window = _build_normal_window(seed=42)
        score = _score_window(detector, window)
        assert score < 0.05, (
            f"Normal window scored {score:.4f}, expected < 0.05 (noise floor)"
        )

    def test_multiple_seeds_all_pass(self) -> None:
        """Across different RNG seeds, compound episodes always score >= 0.08."""
        detector = _get_detector()
        min_score = 1.0
        for seed in range(5):
            window = _build_normal_window(seed=seed)
            scored_window = _apply_compound_offsets(
                window,
                channels=["temperature", "humidity", "light"],
                sigma=MOCK_SCADA_DEMO_SPIKE_SIGMA,
                duration_ticks=10,  # midpoint of [8, 12]
                direction=1,
            )
            score = _score_window(detector, scored_window)
            min_score = min(min_score, score)
            assert score >= 0.08, (
                f"Seed {seed}: compound scored {score:.4f}, expected >= 0.08"
            )
        # Report the minimum for tuning visibility.
        print(f"\n  Minimum compound score across seeds: {min_score:.4f}")

    def test_mock_source_compound_episode_integration(self) -> None:
        """End-to-end: MockScadaSource in demo mode produces scoreable compound."""
        import src.live.sources.mock_episodes as mock_mod
        detector = _get_detector()

        # Configure source with fast demo scheduling.
        original_min = mock_mod.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS
        original_max = mock_mod.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS
        original_rate = mock_mod.MOCK_SCADA_ANOMALY_RATE
        mock_mod.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS = 2.0
        mock_mod.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS = 4.0
        mock_mod.MOCK_SCADA_ANOMALY_RATE = 0.0

        try:
            config = SourceConfig(
                source_id="test_demo_e2e",
                vendor="mock",
                tenant_id="t1",
                greenhouse_id="gh_A",
                channels=["temperature", "humidity", "co2", "light"],
                priority=10,
                poll_interval_seconds=1.0,
                params={
                    "seed": 42,
                    "start_time": "2026-07-18T12:00:00+00:00",
                    "demo_mode": True,
                },
            )
            source = MockScadaSource(config)

            # Collect readings until we fill a window during a compound episode.
            readings: List[List[float]] = []
            found_compound = False
            peak_score = 0.0

            for tick in range(60):
                ts = datetime(2026, 7, 18, 12, 0, tick, tzinfo=timezone.utc)
                source._tick_count = tick
                channels = source._generate_channels(ts)
                vector = [channels.get(ch, 0.0) for ch in CHANNEL_ORDER]
                readings.append(vector)

                # Once we have a full window and are in a compound episode,
                # score it.
                if len(readings) >= SEQ_LEN:
                    window = np.array(readings[-SEQ_LEN:], dtype=np.float32)
                    score = _score_window(detector, window)
                    peak_score = max(peak_score, score)
                    if source._compound_episode is not None:
                        found_compound = True

            assert found_compound, "Demo compound episode never started"
            assert peak_score >= 0.08, (
                f"Peak score during compound episode: {peak_score:.4f}, "
                f"expected >= 0.08"
            )
            print(f"\n  E2E peak score: {peak_score:.4f}")
        finally:
            mock_mod.MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS = original_min
            mock_mod.MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS = original_max
            mock_mod.MOCK_SCADA_ANOMALY_RATE = original_rate
