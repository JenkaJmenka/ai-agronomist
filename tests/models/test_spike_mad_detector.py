"""Tests for SpikeMADDetector using document Table 2 verification dataset."""

import numpy as np
import pytest

from src.models.spike_mad_detector import SpikeMADDetector


class TestSpikeMADDetector:
    """Document Table 2: Spike anomaly verification (k=5, c=2.5)."""

    def test_full_table2_dataset(self):
        """Replay all 10 rows from Table 2; flags must match exactly."""
        series = np.array([21.8, 22.1, 22.0, 21.9, 22.2, 35.0, 22.1, 21.9, 22.0, 21.8])
        detector = SpikeMADDetector(window_k=5, tuning_c=2.5)
        flags = detector.detect(series)

        assert flags[0:4] == [False, False, False, False], "Insufficient window"
        assert flags[4] is False, "Normal (delta 0.2 <= 2.5 * 0.1)"
        assert flags[5] is True, "Anomalous spike at 35.0"
        assert flags[6] is False, "Returned to baseline"
        assert flags[7] is False
        assert flags[8] is False
        assert flags[9] is False

    def test_normal_baseline(self):
        series = np.array([21.8, 22.1, 22.0, 21.9, 22.2])
        detector = SpikeMADDetector(window_k=5, tuning_c=2.5)
        flags = detector.detect(series)
        assert not any(flags)

    def test_spike_detection(self):
        series = np.array([22.0, 22.1, 21.9, 22.0, 22.2, 35.0])
        detector = SpikeMADDetector(window_k=5, tuning_c=2.5)
        flags = detector.detect(series)
        assert flags[5] is True

    def test_recovery_after_spike(self):
        series = np.array([22.0, 22.1, 21.9, 22.0, 22.2, 35.0, 22.1, 21.9, 22.0, 21.8])
        detector = SpikeMADDetector(window_k=5, tuning_c=2.5)
        flags = detector.detect(series)
        assert not any(flags[6:])

    def test_insufficient_window(self):
        series = np.array([22.0, 22.1, 21.9, 22.0])
        detector = SpikeMADDetector(window_k=5, tuning_c=2.5)
        flags = detector.detect(series)
        assert flags == [False, False, False, False]

    def test_production_parameters(self):
        """k=10, c=3.0 — higher tolerance requires larger spike."""
        baseline = [22.0 + 0.1 * (i % 3 - 1) for i in range(9)]
        series = np.array(baseline + [35.0])
        detector = SpikeMADDetector(window_k=10, tuning_c=3.0)
        flags = detector.detect(series)
        assert flags[9] is True

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            SpikeMADDetector(window_k=1)

    def test_invalid_tuning_raises(self):
        with pytest.raises(ValueError):
            SpikeMADDetector(tuning_c=0.0)

    def test_empty_series(self):
        detector = SpikeMADDetector(window_k=5, tuning_c=2.5)
        flags = detector.detect(np.array([]))
        assert flags == []

    def test_constant_series_no_flag(self):
        """All identical values => MAD=0, so no spike can be flagged."""
        series = np.array([22.0] * 10)
        detector = SpikeMADDetector(window_k=5, tuning_c=2.5)
        flags = detector.detect(series)
        assert not any(flags)
