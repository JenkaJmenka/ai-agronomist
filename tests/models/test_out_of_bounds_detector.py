"""Tests for OutOfBoundsDetector using document Table 7 verification dataset."""

import numpy as np
import pytest

from src.models.out_of_bounds_detector import OutOfBoundsDetector


class TestOutOfBoundsDetector:
    """Document Table 7: Out-of-bounds humidity anomaly verification."""

    def test_full_table7_dataset(self):
        """Replay all 10 rows from Table 7; flags must match exactly."""
        series = np.array([85.0, 88.0, 92.0, 95.0, 98.0, 108.0, 95.0, 90.0, 85.0, 80.0])
        detector = OutOfBoundsDetector()
        flags = detector.detect_series(series, "humidity")

        assert flags == [
            False, False, False, False, False,
            True,   # 108% exceeds 100%
            False, False, False, False,
        ]

    def test_normal_humidity(self):
        detector = OutOfBoundsDetector()
        for v in [80.0, 85.0, 90.0, 95.0, 98.0]:
            assert detector.detect(v, "humidity") is False

    def test_above_upper_bound(self):
        detector = OutOfBoundsDetector()
        assert detector.detect(108.0, "humidity") is True

    def test_below_lower_bound(self):
        detector = OutOfBoundsDetector()
        assert detector.detect(-5.0, "humidity") is True

    def test_boundary_values_inclusive(self):
        """0.0% and 100.0% are within [0, 100] — not flagged."""
        detector = OutOfBoundsDetector()
        assert detector.detect(0.0, "humidity") is False
        assert detector.detect(100.0, "humidity") is False

    def test_all_sensor_types(self):
        detector = OutOfBoundsDetector()
        assert detector.detect(-50.0, "temperature") is True
        assert detector.detect(25.0, "temperature") is False
        assert detector.detect(85.0, "temperature") is True
        assert detector.detect(-1.0, "co2") is True
        assert detector.detect(400.0, "co2") is False
        assert detector.detect(6000.0, "co2") is True
        assert detector.detect(-1.0, "light") is True
        assert detector.detect(500.0, "light") is False
        assert detector.detect(250000.0, "light") is True

    def test_unknown_sensor_returns_false(self):
        detector = OutOfBoundsDetector()
        assert detector.detect(999.0, "unknown_sensor") is False

    def test_custom_limits(self):
        detector = OutOfBoundsDetector(limits={"pressure": (900.0, 1100.0)})
        assert detector.detect(850.0, "pressure") is True
        assert detector.detect(1000.0, "pressure") is False
        assert detector.detect(1200.0, "pressure") is True
