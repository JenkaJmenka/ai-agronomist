"""Tests for RTTJitterDetector using document Table 11 dataset."""

import pytest

from src.models.network.rtt_jitter_detector import RTTJitterDetector


class TestRTTJitterDetector:
    """Document Table 11: RTT/jitter anomaly verification."""

    def test_full_table11_dataset(self):
        """Replay all 7 rows from Table 11 (baseline=2.0, mult=3.0, jitter=50.0)."""
        detector = RTTJitterDetector(
            baseline_ms=2.0, multiplier=3.0, jitter_threshold_ms=50.0,
        )
        rtts = [2.0, 2.0, 2.0, 2.0, 2.0, 157.0, 118.0]
        expected = [
            (False, False, False),   # 12:00 — no previous
            (False, False, False),   # 12:01
            (False, False, False),   # 12:02
            (False, False, False),   # 12:03
            (False, False, False),   # 12:04
            (True, True, True),      # 12:05 — RTT>6, jitter=155>50
            (True, True, False),     # 12:06 — RTT>6, jitter=39<50
        ]
        for i, rtt in enumerate(rtts):
            result = detector.detect(rtt)
            assert result.is_anomaly == expected[i][0], f"Row {i}: is_anomaly"
            assert result.rtt_violation == expected[i][1], f"Row {i}: rtt_violation"
            assert result.jitter_violation == expected[i][2], f"Row {i}: jitter_violation"

    def test_normal_rtt(self):
        detector = RTTJitterDetector(baseline_ms=2.0, multiplier=3.0, jitter_threshold_ms=50.0)
        result = detector.detect(2.0, previous_rtt_ms=2.0)
        assert result.is_anomaly is False

    def test_rtt_violation(self):
        detector = RTTJitterDetector(baseline_ms=2.0, multiplier=3.0, jitter_threshold_ms=50.0)
        result = detector.detect(157.0, previous_rtt_ms=2.0)
        assert result.rtt_violation is True

    def test_jitter_violation(self):
        detector = RTTJitterDetector(baseline_ms=2.0, multiplier=3.0, jitter_threshold_ms=50.0)
        result = detector.detect(157.0, previous_rtt_ms=2.0)
        assert result.jitter_violation is True
        assert result.jitter_ms == 155.0

    def test_rtt_only_violation(self):
        """RTT flagged but jitter below threshold."""
        detector = RTTJitterDetector(baseline_ms=2.0, multiplier=3.0, jitter_threshold_ms=50.0)
        result = detector.detect(118.0, previous_rtt_ms=157.0)
        assert result.rtt_violation is True
        assert result.jitter_violation is False

    def test_no_previous_rtt(self):
        """First measurement has no jitter."""
        detector = RTTJitterDetector(baseline_ms=2.0, multiplier=3.0, jitter_threshold_ms=50.0)
        result = detector.detect(2.0)
        assert result.jitter_ms is None
        assert result.is_anomaly is False

    def test_production_params(self):
        """Production jitter_threshold=20.0 is stricter."""
        detector = RTTJitterDetector(baseline_ms=2.0, multiplier=3.0, jitter_threshold_ms=20.0)
        result = detector.detect(5.0, previous_rtt_ms=2.0)
        assert result.jitter_ms == 3.0
        assert result.jitter_violation is False
        result2 = detector.detect(30.0, previous_rtt_ms=5.0)
        assert result2.jitter_violation is True
