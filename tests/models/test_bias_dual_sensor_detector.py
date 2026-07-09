"""Tests for BiasDualSensorDetector using document Table 6 dataset."""

import numpy as np
import pytest

from src.models.bias_dual_sensor_detector import BiasDualSensorDetector


class TestBiasDualSensorDetector:
    """Document Table 6: Bias / constant offset verification (w=3, theta=1.5)."""

    def test_full_table6_dataset(self):
        """Replay all 10 rows from Table 6; flags must match exactly."""
        sensor_a = np.array([22.0, 22.1, 22.0, 21.9, 22.2, 22.0, 22.1, 21.9, 22.0, 21.8])
        sensor_b = np.array([22.1, 22.2, 22.1, 22.0, 22.3, 25.1, 25.2, 25.0, 25.1, 24.9])
        detector = BiasDualSensorDetector(window=3, threshold=1.5)
        flags = detector.detect(sensor_a, sensor_b)

        assert flags[0] is False, "Insufficient window"
        assert flags[1] is False, "Insufficient window"
        assert flags[2] is False, "|D_w| = 0.1 <= 1.5"
        assert flags[3] is False
        assert flags[4] is False
        assert flags[5] is False, "|D_w| ~ 1.1 <= 1.5"
        assert flags[6] is True, "|D_w| ~ 2.1 > 1.5"
        assert flags[7] is True, "Systematic offset confirmed"
        assert flags[8] is True
        assert flags[9] is True

    def test_no_bias(self):
        a = np.array([22.0, 22.1, 22.0, 21.9, 22.0])
        b = np.array([22.1, 22.2, 22.1, 22.0, 22.1])
        detector = BiasDualSensorDetector(window=3, threshold=1.5)
        flags = detector.detect(a, b)
        assert not any(flags)

    def test_bias_onset(self):
        a = np.array([22.0, 22.0, 22.0, 22.0, 22.0])
        b = np.array([22.0, 22.0, 22.0, 25.0, 25.0])
        detector = BiasDualSensorDetector(window=3, threshold=1.5)
        flags = detector.detect(a, b)
        assert flags[4] is True

    def test_symmetric_negative_bias(self):
        a = np.array([22.0, 22.0, 22.0, 22.0, 22.0])
        b = np.array([22.0, 22.0, 19.0, 19.0, 19.0])
        detector = BiasDualSensorDetector(window=3, threshold=1.5)
        flags = detector.detect(a, b)
        assert flags[4] is True

    def test_unequal_length_raises(self):
        with pytest.raises(ValueError):
            BiasDualSensorDetector().detect(np.array([1.0]), np.array([1.0, 2.0]))

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            BiasDualSensorDetector(window=0)

    def test_invalid_threshold_raises(self):
        with pytest.raises(ValueError):
            BiasDualSensorDetector(threshold=0.0)

    def test_production_params(self):
        """w=10, theta=0.5 — tighter threshold catches small offsets."""
        a = np.array([22.0] * 20)
        b = np.array([22.0] * 5 + [22.8] * 15)
        detector = BiasDualSensorDetector(window=10, threshold=0.5)
        flags = detector.detect(a, b)
        assert any(flags[14:])
