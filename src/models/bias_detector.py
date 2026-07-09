"""
Bias-offset detector for greenhouse sensors.

Provides two detection strategies:

1. **Legacy (midpoint):** Computes per-sensor mean shift relative to the
   expected midpoint of the physical range.
2. **EMA baseline (preferred):** Compares readings against a rolling
   exponential moving average of the sensor's own recent history.  This
   is strictly more precise — it detects sudden persistent offsets without
   requiring the sensor to operate near the range midpoint.
"""

from __future__ import annotations

from typing import List

import numpy as np

from src.config import BIAS_EMA_SPAN, BIAS_EMA_THRESHOLD, SENSOR_NAMES

_SENSOR_NAMES: list[str] = list(SENSOR_NAMES)

_BIAS_FRACTION: float = 0.3


class BiasOffsetDetector:
    """Detect persistent mean-shift (bias) per sensor channel.

    Args:
        expected_ranges: Mapping of sensor name to ``(min, max)`` tuple
            in physical units.  Use ``SENSOR_RANGES`` from
            ``src/models/data_utils``.
    """

    def __init__(
        self,
        expected_ranges: dict[str, tuple[float, float]],
    ) -> None:
        self._expected_ranges = expected_ranges

    def detect(self, sequence: np.ndarray) -> dict[str, float]:
        """Compute per-sensor bias magnitude (legacy midpoint method).

        Args:
            sequence: Array of shape ``(seq_len, 4)`` with columns
                ordered as ``[temperature, humidity, co2, light]``.
                Values should be in physical (denormalized) units.

        Returns:
            Dict mapping sensor name to bias magnitude in ``[0, 1]``
            (normalized by range span).  ``0.0`` means no significant
            bias detected.
        """
        if sequence.ndim != 2:
            raise ValueError(
                f"Expected 2D array (seq_len, sensors), got {sequence.ndim}D"
            )

        means = np.mean(sequence, axis=0)
        num_sensors = min(sequence.shape[1], len(_SENSOR_NAMES))
        result: dict[str, float] = {}

        for idx in range(num_sensors):
            name = _SENSOR_NAMES[idx]
            sensor_range = self._expected_ranges.get(name)

            if sensor_range is None:
                result[name] = 0.0
                continue

            range_min, range_max = sensor_range
            expected_mid = (range_min + range_max) / 2.0
            range_span = range_max - range_min

            if range_span <= 0:
                result[name] = 0.0
                continue

            deviation = abs(float(means[idx]) - expected_mid)

            if deviation > _BIAS_FRACTION * range_span:
                result[name] = deviation / range_span
            else:
                result[name] = 0.0

        return result

    @staticmethod
    def detect_ema(
        series: np.ndarray,
        ema_span: int = BIAS_EMA_SPAN,
        threshold: float = BIAS_EMA_THRESHOLD,
    ) -> List[bool]:
        """Per-timestep bias detection using EMA baseline comparison.

        Computes an EMA of the series and flags timesteps where the
        instantaneous deviation from the EMA exceeds ``threshold``.

        Args:
            series: 1-D array of sensor readings.
            ema_span: EMA span in samples (default 288 = 24 h at 5-min).
            threshold: Absolute deviation limit.

        Returns:
            List of booleans; ``True`` = bias offset detected.
            First entry is always ``False`` (EMA initialises to first
            reading).
        """
        series = np.asarray(series, dtype=float)
        n = len(series)
        flags: List[bool] = [False] * n
        if n < 2:
            return flags

        alpha = 2.0 / (ema_span + 1)
        ema = series[0]

        for t in range(1, n):
            ema = alpha * series[t] + (1 - alpha) * ema
            if abs(series[t] - ema) > threshold:
                flags[t] = True

        return flags
