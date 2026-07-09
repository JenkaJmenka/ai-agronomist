"""Bias / constant offset detector using redundant sensor cross-difference.

Compares two co-located sensors measuring the same physical variable.
Flags when the rolling average of their difference exceeds a calibration
tolerance, indicating that one sensor has developed a persistent offset.

This is the gold standard for bias detection in industrial metrology.
"""

from __future__ import annotations

from typing import List

import numpy as np

from src.config import BIAS_DUAL_THRESHOLD, BIAS_DUAL_WINDOW


class BiasDualSensorDetector:
    """Detect bias offset between redundant co-located sensors."""

    def __init__(
        self,
        window: int = BIAS_DUAL_WINDOW,
        threshold: float = BIAS_DUAL_THRESHOLD,
    ) -> None:
        if window < 1:
            raise ValueError(f"window must be >= 1, got {window}")
        if threshold <= 0:
            raise ValueError(f"threshold must be > 0, got {threshold}")
        self._window = window
        self._threshold = threshold

    def detect(
        self,
        sensor_a: np.ndarray,
        sensor_b: np.ndarray,
    ) -> List[bool]:
        """Per-timestep bias detection from two co-located sensors.

        Args:
            sensor_a: 1-D readings from sensor A.
            sensor_b: 1-D readings from sensor B (same length).

        Returns:
            List of booleans; ``True`` = bias offset detected.
            First ``w-1`` entries are ``False`` (insufficient window).
        """
        sensor_a = np.asarray(sensor_a, dtype=float)
        sensor_b = np.asarray(sensor_b, dtype=float)
        if len(sensor_a) != len(sensor_b):
            raise ValueError("sensor_a and sensor_b must have equal length")

        n = len(sensor_a)
        diff = sensor_a - sensor_b
        flags: List[bool] = [False] * n
        w = self._window

        if n < w:
            return flags

        for t in range(w - 1, n):
            avg_diff = float(np.mean(diff[t - w + 1 : t + 1]))
            flags[t] = abs(avg_diff) > self._threshold

        return flags
