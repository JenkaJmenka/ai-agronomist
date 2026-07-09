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

from src.config import SENSOR_NAMES, STUCK_FLAT_RANGE_THRESHOLD

try:
    from src.config import STUCK_EPSILON, STUCK_WINDOW_K, STUCK_VARIANCE_THRESHOLD
except ImportError:  # pragma: no cover
    STUCK_WINDOW_K = 10
    STUCK_VARIANCE_THRESHOLD = 1e-4
    STUCK_EPSILON = 1e-4

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

        seq_active = sequence[:, :num_sensors]
        if sequence.shape[0] >= self._window_k:
            windows = np.lib.stride_tricks.sliding_window_view(seq_active, self._window_k, axis=0)
            variances = np.var(windows, axis=-1, ddof=1)
            min_variances = np.min(variances, axis=0)
        else:
            min_variances = np.var(seq_active, axis=0)

        # Standardize NaNs to 1.0
        min_variances = np.where(np.isnan(min_variances), 1.0, min_variances)

        value_ranges = np.max(seq_active, axis=0) - np.min(seq_active, axis=0)
        is_flats = value_ranges < self._flat_range_threshold

        for idx in range(num_sensors):
            name = _SENSOR_NAMES[idx]
            if name in skip:
                results[name] = False
                continue
            is_flat = bool(is_flats[idx])
            variance = float(min_variances[idx])
            results[name] = bool(
                is_flat and variance < self._variance_threshold
            )
        return results

    @staticmethod
    def detect_series(
        series: np.ndarray,
        w: int = STUCK_WINDOW_K,
        epsilon: float = STUCK_EPSILON,
    ) -> list[bool]:
        """Per-timestep stuck-at detection on a 1-D time series.

        Uses rolling variance with Bessel's correction (``ddof=1``).

        Args:
            series: 1-D array of sensor readings.
            w: Rolling window size.
            epsilon: Minimum variance threshold.

        Returns:
            List of booleans; ``True`` when the rolling variance
            falls below *epsilon*.  The first ``w-1`` entries are
            always ``False`` (insufficient window).
        """
        series = np.asarray(series, dtype=float)
        n = len(series)
        flags: list[bool] = [False] * n

        if n < w or w < 2:
            return flags

        for t in range(w - 1, n):
            window = series[t - w + 1 : t + 1]
            var = float(np.var(window, ddof=1))
            flags[t] = var < epsilon

        return flags
