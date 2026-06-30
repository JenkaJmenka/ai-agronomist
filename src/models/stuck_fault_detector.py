"""
Stuck-fault detector for greenhouse sensors.

Flags sensors whose minimum rolling-window variance falls below a
configurable threshold, indicating a frozen / stuck reading. Using the
minimum over rolling windows means a sensor that was stuck for the first
K steps is still caught even if it recovers later.
"""

from __future__ import annotations

from typing import Optional, Set

import numpy as np
import pandas as pd

from src.config import SENSOR_NAMES, STUCK_FLAT_RANGE_THRESHOLD

try:
    from src.config import STUCK_WINDOW_K, STUCK_VARIANCE_THRESHOLD
except ImportError:  # pragma: no cover
    STUCK_WINDOW_K = 10
    STUCK_VARIANCE_THRESHOLD = 1e-4

_SENSOR_NAMES: list[str] = list(SENSOR_NAMES)


class StuckFaultDetector:
    """Detect sensors stuck at a constant value via rolling variance analysis."""

    def __init__(
        self,
        variance_threshold: float = STUCK_VARIANCE_THRESHOLD,
        window_k: int = STUCK_WINDOW_K,
        flat_range_threshold: float = STUCK_FLAT_RANGE_THRESHOLD,
    ) -> None:
        self._variance_threshold = variance_threshold
        self._window_k = window_k
        self._flat_range_threshold = flat_range_threshold

    def detect(
        self,
        sequence: np.ndarray,
        skip_channels: Optional[Set[str]] = None,
    ) -> dict[str, bool]:
        """Check each sensor channel for stuck-fault condition."""
        if sequence.ndim != 2:
            raise ValueError(
                f"Expected 2D array (seq_len, sensors), got {sequence.ndim}D"
            )

        skip = skip_channels or set()
        num_sensors = min(sequence.shape[1], len(_SENSOR_NAMES))
        results: dict[str, bool] = {}

        for idx in range(num_sensors):
            name = _SENSOR_NAMES[idx]
            if name in skip:
                results[name] = False
                continue
            channel = sequence[:, idx]
            value_range = float(np.max(channel) - np.min(channel))
            if sequence.shape[0] >= self._window_k:
                variance = float(
                    pd.Series(channel)
                    .rolling(window=self._window_k, min_periods=self._window_k)
                    .var()
                    .min()
                )
            else:
                variance = float(np.var(channel))
            if np.isnan(variance):
                variance = 1.0
            is_flat = value_range < self._flat_range_threshold
            results[name] = bool(
                is_flat and variance < self._variance_threshold
            )
        return results
