"""DHCP fingerprint configuration anomaly detector.

Compares a SHA-256 hash of the current client network configuration
(MAC, BSSID, gateway, DNS) against a baseline hash enrolled during
device provisioning.  Any mismatch indicates spoofed environments,
MAC clones, or unauthorised bridge hardware.
"""

from __future__ import annotations

import hashlib


class DHCPFingerprintDetector:
    """Detect DHCP configuration fingerprint violations."""

    @staticmethod
    def compute_hash(*params: str) -> str:
        """Compute SHA-256 of concatenated configuration parameters.

        Args:
            *params: Ordered configuration strings (MAC, gateway IP,
                DNS IP, BSSID, etc.).

        Returns:
            Hex-encoded SHA-256 digest.
        """
        payload = "|".join(params)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def detect(current_hash: str, baseline_hash: str) -> bool:
        """Check whether the current fingerprint matches the baseline.

        Args:
            current_hash: SHA-256 of the current configuration.
            baseline_hash: SHA-256 of the enrolled baseline.

        Returns:
            ``True`` if the hashes differ (anomaly).
        """
        return current_hash != baseline_hash
