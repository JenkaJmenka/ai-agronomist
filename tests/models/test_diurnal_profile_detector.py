"""Tests for DiurnalProfileDetector using document Table 10 dataset."""

import pytest

from src.models.diurnal_profile_detector import DiurnalProfileDetector


class TestDiurnalProfileDetector:
    """Document Table 10: Diurnal profile deviation (theta=3.0)."""

    HOURLY_STATS = {
        10: (24.5, 0.5),
        11: (24.8, 0.5),
        12: (25.0, 0.5),
        13: (24.9, 0.5),
        14: (24.5, 0.5),
        15: (23.8, 0.5),
    }

    def test_full_table10_dataset(self):
        """Replay all 6 rows from Table 10; flags must match exactly."""
        detector = DiurnalProfileDetector(threshold=3.0)
        cases = [
            (24.1, 10, False),   # Z = -0.8
            (24.6, 11, False),   # Z = -0.4
            (24.8, 12, False),   # Z = -0.4
            (18.2, 13, True),    # Z = -13.4
            (24.7, 14, False),   # Z = 0.4
            (24.0, 15, False),   # Z = 0.4
        ]
        for value, hour, expected in cases:
            assert detector.detect(value, hour, self.HOURLY_STATS) is expected, \
                f"Hour {hour}, value {value}"

    def test_normal_reading(self):
        detector = DiurnalProfileDetector(threshold=3.0)
        assert detector.detect(24.1, 10, self.HOURLY_STATS) is False

    def test_extreme_deviation(self):
        detector = DiurnalProfileDetector(threshold=3.0)
        assert detector.detect(18.2, 13, self.HOURLY_STATS) is True

    def test_boundary_not_flagged(self):
        """Z exactly 3.0 should NOT be flagged (strict >)."""
        detector = DiurnalProfileDetector(threshold=3.0)
        mu, sigma = 24.9, 0.5
        value = mu + 3.0 * sigma
        assert detector.detect(value, 13, self.HOURLY_STATS) is False

    def test_positive_deviation(self):
        detector = DiurnalProfileDetector(threshold=3.0)
        assert detector.detect(26.5, 13, self.HOURLY_STATS) is True

    def test_production_threshold(self):
        """theta=2.5 catches readings at 2.5 sigma."""
        detector = DiurnalProfileDetector(threshold=2.5)
        mu, sigma = 24.9, 0.5
        value = mu + 2.6 * sigma
        assert detector.detect(value, 13, self.HOURLY_STATS) is True

    def test_unknown_hour_returns_false(self):
        detector = DiurnalProfileDetector(threshold=3.0)
        assert detector.detect(50.0, 23, self.HOURLY_STATS) is False

    def test_zero_std_exact_match(self):
        """With std=0, only exact match is normal."""
        stats = {10: (24.5, 0.0)}
        detector = DiurnalProfileDetector(threshold=3.0)
        assert detector.detect(24.5, 10, stats) is False
        assert detector.detect(24.6, 10, stats) is True

    def test_invalid_threshold_raises(self):
        with pytest.raises(ValueError):
            DiurnalProfileDetector(threshold=0.0)
