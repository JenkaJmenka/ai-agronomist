"""Tests for ServiceResponseDetector using document Table 15 dataset."""

import numpy as np
import pytest

from src.models.network.service_response_detector import ServiceResponseDetector


class TestServiceResponseDetector:
    """Document Table 15: Service response duration (theta=120.0 ms)."""

    def test_full_table15_dataset(self):
        """Replay all 6 rows from Table 15."""
        detector = ServiceResponseDetector(baseline_threshold_ms=120.0)
        cases = [
            (45.0, False),    # 12:00
            (48.0, False),    # 12:01
            (44.0, False),    # 12:02
            (52.0, False),    # 12:03
            (47.0, False),    # 12:04
            (350.0, True),    # 12:05 — resource exhaustion
        ]
        for duration, expected in cases:
            assert detector.detect(duration) is expected

    def test_normal_response(self):
        detector = ServiceResponseDetector(baseline_threshold_ms=120.0)
        assert detector.detect(45.0) is False

    def test_slow_response(self):
        detector = ServiceResponseDetector(baseline_threshold_ms=120.0)
        assert detector.detect(350.0) is True

    def test_boundary_not_flagged(self):
        """120.0 exactly should NOT be flagged (strict >)."""
        detector = ServiceResponseDetector(baseline_threshold_ms=120.0)
        assert detector.detect(120.0) is False

    def test_override_threshold(self):
        detector = ServiceResponseDetector(baseline_threshold_ms=120.0)
        assert detector.detect(80.0, baseline_threshold_ms=50.0) is True
        assert detector.detect(80.0, baseline_threshold_ms=100.0) is False

    def test_detect_series(self):
        durations = np.array([45.0, 48.0, 44.0, 52.0, 47.0, 350.0])
        detector = ServiceResponseDetector(baseline_threshold_ms=120.0)
        flags = detector.detect_series(durations)
        assert flags == [False, False, False, False, False, True]
