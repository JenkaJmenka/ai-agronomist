"""
Bias-offset detector for greenhouse sensors.

Computes per-sensor mean shift relative to expected midpoint of the
physical range and flags channels whose bias exceeds a configurable
fraction of the range span.
"""

from __future__ import annotations

import numpy as np

from src.config import SENSOR_NAMES

_SENSOR_NAMES: list[str] = list(SENSOR_NAMES)

# Fraction of range span that constitutes a significant bias offset.
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
        """Compute per-sensor bias magnitude.

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
