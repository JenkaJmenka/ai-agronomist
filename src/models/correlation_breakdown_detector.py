"""Spatial correlation breakdown detector for redundant sensor pairs.

Monitors the rolling Pearson correlation between two co-located sensors
that should track each other closely.  A drop below the configured
threshold indicates that one sensor has failed or is reporting
erroneous data.
"""

from __future__ import annotations

from typing import List

import numpy as np

from src.config import CORR_BREAKDOWN_THRESHOLD, CORR_BREAKDOWN_WINDOW


class CorrelationBreakdownDetector:
    """Detect spatial correlation breakdown between sensor pairs."""

    def __init__(
        self,
        window: int = CORR_BREAKDOWN_WINDOW,
        threshold: float = CORR_BREAKDOWN_THRESHOLD,
    ) -> None:
        if window < 3:
            raise ValueError(f"window must be >= 3, got {window}")
        self._window = window
        self._threshold = threshold

    def detect(
        self,
        sensor_a: np.ndarray,
        sensor_b: np.ndarray,
    ) -> List[bool]:
        """Per-timestep correlation breakdown detection.

        Args:
            sensor_a: 1-D readings from sensor A.
            sensor_b: 1-D readings from sensor B (same length).

        Returns:
            List of booleans; ``True`` = correlation below threshold.
            First ``w-1`` entries are ``False``.
        """
        sensor_a = np.asarray(sensor_a, dtype=float)
        sensor_b = np.asarray(sensor_b, dtype=float)
        if len(sensor_a) != len(sensor_b):
            raise ValueError("sensor_a and sensor_b must have equal length")

        n = len(sensor_a)
        w = self._window
        flags: List[bool] = [False] * n

        if n < w:
            return flags

        for t in range(w - 1, n):
            wa = sensor_a[t - w + 1 : t + 1]
            wb = sensor_b[t - w + 1 : t + 1]
            std_a = float(np.std(wa, ddof=0))
            std_b = float(np.std(wb, ddof=0))

            if std_a < 1e-12 or std_b < 1e-12:
                flags[t] = True
                continue

            corr_matrix = np.corrcoef(wa, wb)
            rho = float(corr_matrix[0, 1])

            if np.isnan(rho) or rho < self._threshold:
                flags[t] = True

        return flags
