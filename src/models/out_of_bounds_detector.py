"""Out-of-bounds detector for hard physical / hardware sensor limits.

Flags readings that fall outside the absolute boundaries of the sensing
hardware (e.g., humidity cannot physically exceed 100%).  These are
sensor-type-specific hard limits, not operational setpoint ranges.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np

from src.config import OOB_LIMITS


class OutOfBoundsDetector:
    """Detect readings outside hard physical sensor limits."""

    def __init__(
        self,
        limits: Dict[str, Tuple[float, float]] | None = None,
    ) -> None:
        self._limits = dict(limits or OOB_LIMITS)

    def detect(self, value: float, sensor: str) -> bool:
        """Check a single reading against the sensor's physical limits.

        Args:
            value: Raw sensor reading.
            sensor: Sensor type key (e.g. ``"humidity"``).

        Returns:
            ``True`` if the value is outside the physical limits.
        """
        bounds = self._limits.get(sensor)
        if bounds is None:
            return False
        lower, upper = bounds
        return value < lower or value > upper

    def detect_series(self, series: np.ndarray, sensor: str) -> List[bool]:
        """Check every element of a 1-D series.

        Args:
            series: 1-D array of sensor readings.
            sensor: Sensor type key.

        Returns:
            List of booleans, one per element.
        """
        bounds = self._limits.get(sensor)
        if bounds is None:
            return [False] * len(series)
        series = np.asarray(series, dtype=float)
        lower, upper = bounds
        oob = (series < lower) | (series > upper)
        return oob.tolist()
