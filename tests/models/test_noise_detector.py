"""Tests for NoiseDetector (SCADA spec section 1.4 — precision degradation)."""

from __future__ import annotations

import numpy as np
import pytest

from src.models.noise_detector import NoiseDetector


@pytest.fixture()
def detector() -> NoiseDetector:
    return NoiseDetector()


@pytest.fixture()
def rng() -> np.random.Generator:
    return np.random.default_rng(42)


class TestDetection:
    """Core detection behaviour."""

    def test_high_noise_triggers_alarm(
        self, detector: NoiseDetector, rng: np.random.Generator
    ) -> None:
        """A signal with large high-frequency noise exceeds the target."""
        # Temperature target = 0.3, alpha = 3.0 → threshold = 0.9
        # Add heavy white noise (std=5.0) to a smooth signal
        clean = np.linspace(20.0, 25.0, 100)
        noisy = clean + rng.normal(0, 5.0, size=100)
        assert detector.detect(noisy, "temperature") is True

    def test_clean_signal_no_alarm(
        self, detector: NoiseDetector, rng: np.random.Generator
    ) -> None:
        """A smooth signal with minimal noise does not trigger."""
        clean = np.linspace(20.0, 25.0, 100)
        clean += rng.normal(0, 0.01, size=100)  # tiny noise
        assert detector.detect(clean, "temperature") is False

    def test_unknown_sensor_returns_false(
        self, detector: NoiseDetector, rng: np.random.Generator
    ) -> None:
        """A sensor not in SENSOR_NOISE_TARGETS returns False."""
        series = rng.normal(0, 10.0, size=100)
        assert detector.detect(series, "unknown_sensor") is False

    def test_too_short_sequence_returns_false(
        self, detector: NoiseDetector
    ) -> None:
        """Series shorter than 10 samples returns False (filtfilt padlen)."""
        assert detector.detect(np.array([1.0, 2.0, 3.0]), "temperature") is False
        assert detector.detect(np.array([1.0] * 9), "temperature") is False

    def test_minimum_viable_length(
        self, detector: NoiseDetector
    ) -> None:
        """Exactly 10 samples should not crash."""
        series = np.array([20.0, 25.0, 20.0, 25.0, 20.0, 25.0, 20.0, 25.0, 20.0, 25.0])
        result = detector.detect(series, "temperature")
        assert isinstance(result, bool)

    def test_threshold_boundary(
        self, detector: NoiseDetector, rng: np.random.Generator
    ) -> None:
        """Signal noise near boundary correctly triggers/ignores.

        The hybrid detector uses variance-ratio against baseline variance.
        For temperature, baseline_variance=0.01 and ratio_threshold=10.0,
        so alarm fires when filtered variance > 0.1 (i.e. filtered std > ~0.316).
        """
        clean = np.linspace(20.0, 25.0, 100)

        barely_fail_noise = rng.normal(0, 0.1, size=100)
        assert detector.detect(clean + barely_fail_noise, "temperature") is False

        barely_pass_noise = rng.normal(0, 1.0, size=100)
        assert detector.detect(clean + barely_pass_noise, "temperature") is True


class TestSensorTypes:
    """Verify detection works for different sensor types."""

    def test_humidity_high_noise(
        self, detector: NoiseDetector, rng: np.random.Generator
    ) -> None:
        """Humidity with very high noise triggers."""
        noisy = 65.0 + rng.normal(0, 20.0, size=100)
        assert detector.detect(noisy, "humidity") is True

    def test_co2_clean(
        self, detector: NoiseDetector, rng: np.random.Generator
    ) -> None:
        """Clean CO2 signal does not trigger."""
        clean = np.linspace(400.0, 410.0, 100) + rng.normal(0, 0.1, size=100)
        assert detector.detect(clean, "co2") is False
