"""
Stuck-fault detector for greenhouse sensors.

Flags sensors whose minimum rolling-window variance falls below a
configurable threshold, indicating a frozen / stuck reading. Using the
minimum over rolling windows means a sensor that was stuck for the first
K steps is still caught even if it recovers later.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import SENSOR_NAMES

try:
    from src.config import STUCK_WINDOW_K, STUCK_VARIANCE_THRESHOLD
except ImportError:  # pragma: no cover
    STUCK_WINDOW_K = 10
    STUCK_VARIANCE_THRESHOLD = 1e-4

_SENSOR_NAMES: list[str] = list(SENSOR_NAMES)


class StuckFaultDetector:
    """Detect sensors stuck at a constant value via rolling variance analysis.

    Args:
        window: Expected number of timesteps in the sequence.
            Used only for documentation; detection works on any length.
        variance_threshold: Per-sensor variance below this value
            flags the sensor as stuck.
        window_k: Rolling window size for minimum-variance computation.
            Defaults to ``STUCK_WINDOW_K`` from config.
    """

    def __init__(
        self,
        variance_threshold: float = STUCK_VARIANCE_THRESHOLD,
        window_k: int = STUCK_WINDOW_K,
    ) -> None:
        self._variance_threshold = variance_threshold
        self._window_k = window_k

    def detect(self, sequence: np.ndarray) -> dict[str, bool]:
        """Check each sensor channel for stuck-fault condition.

        Uses the minimum variance over rolling windows of size
        ``_window_k`` so a sensor stuck for only part of the sequence
        is still caught.

        Args:
            sequence: Array of shape ``(seq_len, n_sensors)`` with columns
                ordered as ``[temperature, humidity, co2, light]``.

        Returns:
            Dict mapping each sensor name to a boolean (``True`` = stuck).
        """
        if sequence.ndim != 2:
            raise ValueError(
                f"Expected 2D array (seq_len, sensors), got {sequence.ndim}D"
            )

        num_sensors = min(sequence.shape[1], len(_SENSOR_NAMES))

        # Use minimum variance over rolling window of STUCK_WINDOW_K steps
        # so a sensor stuck for first 15/20 steps is still caught even if
        # it recovers in the last few steps.
        variances = np.array([
            pd.Series(sequence[:, idx])
            .rolling(window=self._window_k, min_periods=self._window_k)
            .var()
            .min()
            if sequence.shape[0] >= self._window_k
            else np.var(sequence[:, idx])
            for idx in range(num_sensors)
        ])
        variances = np.nan_to_num(variances, nan=1.0)  # NaN = not enough data → not stuck

        return {
            _SENSOR_NAMES[idx]: bool(variances[idx] < self._variance_threshold)
            for idx in range(num_sensors)
        }
