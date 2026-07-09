"""Network round-trip time and jitter anomaly detector.

Flags excessive RTT (indicating congestion, ARP spoofing, or rogue
access points) and jitter spikes (sudden routing path changes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.config import JITTER_THRESHOLD_MS, RTT_BASELINE_MS, RTT_MULTIPLIER


@dataclass(frozen=True)
class RTTJitterResult:
    """Result of a single RTT/jitter check."""

    is_anomaly: bool
    rtt_violation: bool
    jitter_violation: bool
    rtt_ms: float
    jitter_ms: Optional[float]


class RTTJitterDetector:
    """Detect RTT latency and jitter anomalies on OT network links."""

    def __init__(
        self,
        baseline_ms: float = RTT_BASELINE_MS,
        multiplier: float = RTT_MULTIPLIER,
        jitter_threshold_ms: float = JITTER_THRESHOLD_MS,
    ) -> None:
        self._rtt_threshold = multiplier * baseline_ms
        self._jitter_threshold = jitter_threshold_ms
        self._last_rtt: Optional[float] = None

    def detect(
        self,
        rtt_ms: float,
        previous_rtt_ms: Optional[float] = None,
    ) -> RTTJitterResult:
        """Check a single RTT measurement.

        Args:
            rtt_ms: Current round-trip time in milliseconds.
            previous_rtt_ms: Previous RTT for jitter calculation.
                If ``None``, uses the internally tracked value.

        Returns:
            Result with per-check violation flags.
        """
        prev = previous_rtt_ms if previous_rtt_ms is not None else self._last_rtt
        jitter: Optional[float] = None

        rtt_violation = rtt_ms > self._rtt_threshold
        jitter_violation = False

        if prev is not None:
            jitter = abs(rtt_ms - prev)
            jitter_violation = jitter > self._jitter_threshold

        self._last_rtt = rtt_ms

        return RTTJitterResult(
            is_anomaly=rtt_violation or jitter_violation,
            rtt_violation=rtt_violation,
            jitter_violation=jitter_violation,
            rtt_ms=rtt_ms,
            jitter_ms=jitter,
        )
