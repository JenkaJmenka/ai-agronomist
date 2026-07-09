"""Service response duration anomaly detector.

Flags microservice or API latencies that exceed a dynamic threshold
derived from the 99th (or configurable) percentile of a sliding
baseline window.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from src.config import SERVICE_RESPONSE_BASELINE_MS


class ServiceResponseDetector:
    """Detect anomalous service response durations."""

    def __init__(
        self,
        baseline_threshold_ms: float = SERVICE_RESPONSE_BASELINE_MS,
    ) -> None:
        self._threshold = baseline_threshold_ms

    @property
    def threshold_ms(self) -> float:
        return self._threshold

    def detect(
        self,
        duration_ms: float,
        baseline_threshold_ms: Optional[float] = None,
    ) -> bool:
        """Check a single response duration.

        Args:
            duration_ms: Measured response time in milliseconds.
            baseline_threshold_ms: Override threshold for this check.

        Returns:
            ``True`` if duration exceeds the threshold.
        """
        threshold = baseline_threshold_ms if baseline_threshold_ms is not None else self._threshold
        return duration_ms > threshold

    def detect_series(
        self,
        durations: np.ndarray,
        baseline_threshold_ms: Optional[float] = None,
    ) -> List[bool]:
        """Check every element of a 1-D duration series.

        Args:
            durations: 1-D array of response times in ms.
            baseline_threshold_ms: Override threshold.

        Returns:
            List of booleans.
        """
        threshold = baseline_threshold_ms if baseline_threshold_ms is not None else self._threshold
        durations = np.asarray(durations, dtype=float)
        return (durations > threshold).tolist()
