"""Tests for DHCPFingerprintDetector using document Table 14 dataset."""

import pytest

from src.models.network.dhcp_fingerprint_detector import DHCPFingerprintDetector


class TestDHCPFingerprintDetector:
    """Document Table 14: DHCP fingerprint configuration."""

    LEGITIMATE_MAC = "00:1A:2B:3C:4D:5E"
    LEGITIMATE_GW = "192.168.1.1"
    LEGITIMATE_DNS = "8.8.8.8"
    ROGUE_DNS = "10.0.0.9"

    def test_full_table14_dataset(self):
        """Replay normal and anomalous rows from Table 14."""
        baseline = DHCPFingerprintDetector.compute_hash(
            self.LEGITIMATE_MAC, self.LEGITIMATE_GW, self.LEGITIMATE_DNS,
        )
        for _ in range(5):
            current = DHCPFingerprintDetector.compute_hash(
                self.LEGITIMATE_MAC, self.LEGITIMATE_GW, self.LEGITIMATE_DNS,
            )
            assert DHCPFingerprintDetector.detect(current, baseline) is False

        rogue = DHCPFingerprintDetector.compute_hash(
            self.LEGITIMATE_MAC, self.LEGITIMATE_GW, self.ROGUE_DNS,
        )
        assert DHCPFingerprintDetector.detect(rogue, baseline) is True

    def test_matching_hash(self):
        h = DHCPFingerprintDetector.compute_hash("AA:BB:CC:DD:EE:FF", "10.0.0.1", "8.8.4.4")
        assert DHCPFingerprintDetector.detect(h, h) is False

    def test_dns_change_detected(self):
        baseline = DHCPFingerprintDetector.compute_hash("AA:BB", "10.0.0.1", "8.8.8.8")
        current = DHCPFingerprintDetector.compute_hash("AA:BB", "10.0.0.1", "1.1.1.1")
        assert DHCPFingerprintDetector.detect(current, baseline) is True

    def test_mac_spoof_different_bssid(self):
        baseline = DHCPFingerprintDetector.compute_hash("AA:BB", "10.0.0.1", "8.8.8.8")
        current = DHCPFingerprintDetector.compute_hash("AA:BB", "192.168.0.1", "8.8.8.8")
        assert DHCPFingerprintDetector.detect(current, baseline) is True

    def test_hash_deterministic(self):
        h1 = DHCPFingerprintDetector.compute_hash("a", "b", "c")
        h2 = DHCPFingerprintDetector.compute_hash("a", "b", "c")
        assert h1 == h2

    def test_hash_length(self):
        h = DHCPFingerprintDetector.compute_hash("test")
        assert len(h) == 64
