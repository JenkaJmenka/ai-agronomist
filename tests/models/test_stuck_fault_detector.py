"""Tests for StuckFaultDetector — rolling-window variance analysis."""

from __future__ import annotations

import numpy as np
import pytest

from src.models.stuck_fault_detector import StuckFaultDetector


@pytest.fixture()
def detector() -> StuckFaultDetector:
    return StuckFaultDetector(variance_threshold=1e-4)


class TestDetection:
    def test_stuck_sensor_detected(self, detector: StuckFaultDetector) -> None:
        seq = np.ones((20, 4)) * np.array([25.0, 65.0, 400.0, 500.0])
        result = detector.detect(seq)
        assert result["temperature"] is True

    def test_normal_sensor_not_flagged(self, detector: StuckFaultDetector) -> None:
        rng = np.random.default_rng(0)
        seq = rng.normal(loc=[25.0, 65.0, 400.0, 500.0], scale=[1.0, 2.0, 20.0, 50.0], size=(20, 4))
        result = detector.detect(seq)
        assert result["temperature"] is False

    def test_returns_all_sensor_names(self, detector: StuckFaultDetector) -> None:
        seq = np.ones((20, 4)) * 25.0
        result = detector.detect(seq)
        assert set(result.keys()) == {"temperature", "humidity", "co2", "light"}

    def test_partial_stuck_detected(self, detector: StuckFaultDetector) -> None:
        """A sensor stuck for the first window_k steps is caught even if it recovers."""
        rng = np.random.default_rng(1)
        seq = np.ones((20, 4)) * np.array([25.0, 65.0, 400.0, 500.0])
        # Temperature recovers in last 5 steps — minimum-window logic should still catch it
        seq[15:, 0] = rng.normal(25.0, 1.0, 5)
        result = detector.detect(seq)
        assert result["temperature"] is True


class TestEdgeCases:
    def test_1d_array_raises(self, detector: StuckFaultDetector) -> None:
        with pytest.raises(ValueError, match="Expected 2D"):
            detector.detect(np.ones(20))

    def test_short_sequence_falls_back_to_full_variance(self, detector: StuckFaultDetector) -> None:
        """Sequences shorter than window_k use full-sequence variance — no crash."""
        seq = np.ones((5, 4)) * 25.0
        result = detector.detect(seq)
        assert result["temperature"] is True

    def test_custom_threshold(self) -> None:
        d = StuckFaultDetector(variance_threshold=1.0)
        rng = np.random.default_rng(2)
        seq = rng.normal(25.0, 0.5, (20, 4))  # std=0.5, var=0.25 < 1.0 → stuck
        result = d.detect(seq)
        assert result["temperature"] is True
