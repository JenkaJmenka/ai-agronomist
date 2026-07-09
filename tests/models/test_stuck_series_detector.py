"""Tests for StuckFaultDetector.detect_series using document Table 3."""

import numpy as np
import pytest

from src.models.stuck_fault_detector import StuckFaultDetector


class TestStuckSeriesDetector:
    """Document Table 3: Stuck-at anomaly verification (w=5, eps=0.001)."""

    def test_full_table3_dataset(self):
        """Replay all 10 rows from Table 3; flags must match exactly."""
        series = np.array([22.1, 21.9, 22.2, 22.0, 22.0, 22.0, 22.0, 22.0, 22.0, 22.0])
        flags = StuckFaultDetector.detect_series(series, w=5, epsilon=0.001)

        assert flags[0:4] == [False, False, False, False], "Insufficient window"
        assert flags[4] is False, "Variance 0.013 >= 0.001"
        assert flags[5] is False, "Variance 0.012 >= 0.001"
        assert flags[6] is False, "Variance 0.008 >= 0.001"
        assert flags[7] is True, "All 22.0 — variance 0.0 < 0.001"
        assert flags[8] is True, "Still stuck"
        assert flags[9] is True, "Still stuck"

    def test_normal_variance(self):
        series = np.array([22.1, 21.9, 22.2, 22.0, 22.0])
        flags = StuckFaultDetector.detect_series(series, w=5, epsilon=0.001)
        assert not any(flags)

    def test_stuck_detected(self):
        series = np.array([22.0, 22.0, 22.0, 22.0, 22.0])
        flags = StuckFaultDetector.detect_series(series, w=5, epsilon=0.001)
        assert flags[4] is True

    def test_transition_to_stuck(self):
        series = np.array([22.1, 21.9, 22.2, 22.0, 22.0, 22.0, 22.0, 22.0])
        flags = StuckFaultDetector.detect_series(series, w=5, epsilon=0.001)
        first_stuck = next(i for i, f in enumerate(flags) if f)
        assert first_stuck == 7

    def test_epsilon_boundary(self):
        """Variance exactly at epsilon should NOT be flagged (strict <)."""
        series = np.array([22.0, 22.0, 22.0, 22.0, 22.0])
        var = float(np.var(series, ddof=1))
        flags = StuckFaultDetector.detect_series(series, w=5, epsilon=var)
        assert not any(flags)

    def test_production_params(self):
        """w=20, epsilon=1e-4 — tighter epsilon, longer window."""
        series = np.array([22.0] * 25)
        flags = StuckFaultDetector.detect_series(series, w=20, epsilon=1e-4)
        assert flags[19] is True
        assert not any(flags[:19])

    def test_short_series(self):
        series = np.array([22.0, 22.0])
        flags = StuckFaultDetector.detect_series(series, w=5, epsilon=0.001)
        assert not any(flags)
