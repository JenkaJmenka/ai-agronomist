"""Tests for CorrelationBreakdownDetector using document Table 8."""

import numpy as np
import pytest

from src.models.correlation_breakdown_detector import CorrelationBreakdownDetector


class TestCorrelationBreakdownDetector:
    """Document Table 8: Spatial correlation breakdown (w=5, theta=0.70)."""

    def test_full_table8_dataset(self):
        """Replay all 10 rows from Table 8; flags must match exactly."""
        sensor_a = np.array([20.1, 20.5, 21.0, 21.2, 21.8, 22.0, 22.5, 22.4, 22.8, 23.0])
        sensor_b_anomalous = np.array([
            21.24, 21.60, 21.90, 22.26, 22.95,
            19.50, 19.80, 19.20, 19.60, 19.40,
        ])
        detector = CorrelationBreakdownDetector(window=5, threshold=0.70)
        flags = detector.detect(sensor_a, sensor_b_anomalous)

        assert flags[0:4] == [False, False, False, False], "Insufficient window"
        assert flags[4] is False, "Highly correlated (rho ~ 0.997)"
        assert flags[5] is True, "Correlation broken"
        assert flags[6] is True
        assert flags[7] is True
        assert flags[8] is True
        assert flags[9] is True

    def test_highly_correlated(self):
        a = np.array([20.0, 21.0, 22.0, 23.0, 24.0])
        b = a + 1.0
        detector = CorrelationBreakdownDetector(window=5, threshold=0.70)
        flags = detector.detect(a, b)
        assert not any(flags)

    def test_correlation_breakdown(self):
        a = np.array([20.0, 21.0, 22.0, 23.0, 24.0])
        b = np.array([20.0, 21.0, 22.0, 23.0, 18.0])
        detector = CorrelationBreakdownDetector(window=5, threshold=0.70)
        flags = detector.detect(a, b)
        assert flags[4] is True

    def test_negative_correlation_flagged(self):
        a = np.array([20.0, 21.0, 22.0, 23.0, 24.0])
        b = np.array([30.0, 29.0, 28.0, 27.0, 26.0])
        detector = CorrelationBreakdownDetector(window=5, threshold=0.70)
        flags = detector.detect(a, b)
        assert flags[4] is True

    def test_production_params(self):
        """w=12, theta=0.80 — stricter correlation requirement."""
        a = np.linspace(20, 25, 15)
        b = a + np.random.RandomState(42).normal(0, 0.01, 15)
        detector = CorrelationBreakdownDetector(window=12, threshold=0.80)
        flags = detector.detect(a, b)
        assert not any(flags[11:])

    def test_unequal_length_raises(self):
        with pytest.raises(ValueError):
            CorrelationBreakdownDetector().detect(np.array([1.0, 2.0]), np.array([1.0]))

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            CorrelationBreakdownDetector(window=2)

    def test_constant_sensor_flagged(self):
        """A constant sensor has zero std => flagged as breakdown."""
        a = np.array([20.0, 21.0, 22.0, 23.0, 24.0])
        b = np.array([22.0, 22.0, 22.0, 22.0, 22.0])
        detector = CorrelationBreakdownDetector(window=5, threshold=0.70)
        flags = detector.detect(a, b)
        assert flags[4] is True
