"""Tests for TCPHandshakeDetector using document Table 13 dataset."""

import pytest

from src.models.network.tcp_handshake_detector import TCPHandshakeDetector


class TestTCPHandshakeDetector:
    """Document Table 13: TCP handshake timing (baseline=5.0, c=2.5)."""

    def test_full_table13_dataset(self):
        """Replay all 6 rows from Table 13; threshold = 2.5 * 5.0 = 12.5 ms."""
        detector = TCPHandshakeDetector(baseline_ms=5.0, delay_factor=2.5)
        cases = [
            (5.0, False),    # 12:00
            (5.0, False),    # 12:01
            (4.0, False),    # 12:02
            (6.0, False),    # 12:03
            (5.0, False),    # 12:04
            (25.0, True),    # 12:05 — attack
        ]
        for latency, expected in cases:
            assert detector.detect(latency) is expected

    def test_normal_latency(self):
        detector = TCPHandshakeDetector(baseline_ms=5.0, delay_factor=2.5)
        assert detector.detect(5.0) is False

    def test_attack_latency(self):
        detector = TCPHandshakeDetector(baseline_ms=5.0, delay_factor=2.5)
        assert detector.detect(25.0) is True

    def test_boundary_not_flagged(self):
        """Exactly at threshold (12.5 ms) should NOT be flagged (strict >)."""
        detector = TCPHandshakeDetector(baseline_ms=5.0, delay_factor=2.5)
        assert detector.detect(12.5) is False

    def test_production_params(self):
        """baseline auto-cal, c=2.0 — threshold = 10.0 ms."""
        detector = TCPHandshakeDetector(baseline_ms=5.0, delay_factor=2.0)
        assert detector.threshold_ms == 10.0
        assert detector.detect(11.0) is True
        assert detector.detect(9.0) is False
