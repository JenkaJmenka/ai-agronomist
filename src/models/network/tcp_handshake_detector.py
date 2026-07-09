"""TCP handshake timing anomaly detector.

Flags SYN-to-SYN-ACK delays that exceed a configurable multiple of the
baseline, indicating session hijacking, connection forwarding, or MITM
attacks.
"""

from __future__ import annotations

from src.config import TCP_BASELINE_MS, TCP_DELAY_FACTOR


class TCPHandshakeDetector:
    """Detect anomalous TCP handshake latencies."""

    def __init__(
        self,
        baseline_ms: float = TCP_BASELINE_MS,
        delay_factor: float = TCP_DELAY_FACTOR,
    ) -> None:
        self._threshold = delay_factor * baseline_ms

    @property
    def threshold_ms(self) -> float:
        """The computed latency threshold in milliseconds."""
        return self._threshold

    def detect(self, handshake_ms: float) -> bool:
        """Check a single handshake latency.

        Args:
            handshake_ms: Measured SYN-to-SYN-ACK duration in ms.

        Returns:
            ``True`` if the latency exceeds the threshold.
        """
        return handshake_ms > self._threshold
