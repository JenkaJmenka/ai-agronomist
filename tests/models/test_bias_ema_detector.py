"""Tests for BiasOffsetDetector.detect_ema (EMA-based single-sensor fallback)."""

import numpy as np
import pytest

from src.models.bias_detector import BiasOffsetDetector


class TestBiasEMADetector:
    """EMA-based single-sensor bias detection."""

    def test_sudden_offset_detected(self):
        """A +3C jump from steady 22C should flag."""
        series = np.array([22.0] * 50 + [25.0] * 10)
        flags = BiasOffsetDetector.detect_ema(series, ema_span=20, threshold=2.0)
        assert any(flags[50:])

    def test_stable_series_no_flag(self):
        series = np.array([22.0] * 100)
        flags = BiasOffsetDetector.detect_ema(series, ema_span=288, threshold=2.0)
        assert not any(flags)

    def test_slow_trend_no_flag(self):
        """Gradual setpoint change should not trigger (EMA tracks it)."""
        series = np.linspace(22.0, 24.0, 200)
        flags = BiasOffsetDetector.detect_ema(series, ema_span=50, threshold=2.0)
        assert not any(flags)

    def test_short_series(self):
        flags = BiasOffsetDetector.detect_ema(np.array([22.0]), ema_span=288, threshold=2.0)
        assert flags == [False]

    def test_first_element_always_false(self):
        series = np.array([22.0, 30.0])
        flags = BiasOffsetDetector.detect_ema(series, ema_span=10, threshold=2.0)
        assert flags[0] is False
        assert flags[1] is True

    def test_legacy_detect_still_works(self):
        """Original midpoint-based detect() API is preserved."""
        detector = BiasOffsetDetector(
            expected_ranges={"temperature": (20.0, 30.0), "humidity": (50.0, 80.0)}
        )
        seq = np.array([[22.0, 65.0, 400.0, 500.0]] * 10)
        result = detector.detect(seq)
        assert "temperature" in result
