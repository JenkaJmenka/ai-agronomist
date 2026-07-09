"""Diurnal profile deviation detector.

Checks whether a sensor reading at a given hour matches the historical
statistical profile for that hour.  A Z-score exceeding the configured
threshold indicates an anomalous departure from the expected diurnal
pattern.
"""

from __future__ import annotations

from typing import Dict, Tuple

from src.config import DIURNAL_Z_THRESHOLD


class DiurnalProfileDetector:
    """Detect deviations from historical hourly sensor profiles."""

    def __init__(
        self,
        threshold: float = DIURNAL_Z_THRESHOLD,
    ) -> None:
        if threshold <= 0:
            raise ValueError(f"threshold must be > 0, got {threshold}")
        self._threshold = threshold

    def detect(
        self,
        value: float,
        hour: int,
        hourly_stats: Dict[int, Tuple[float, float]],
    ) -> bool:
        """Check a single reading against its hourly historical profile.

        Args:
            value: Current sensor reading.
            hour: Hour of day (0-23).
            hourly_stats: Mapping of hour to ``(mean, std)`` tuple from
                historical data.

        Returns:
            ``True`` if the absolute Z-score exceeds the threshold.
        """
        stats = hourly_stats.get(hour)
        if stats is None:
            return False

        mean, std = stats
        if std <= 0:
            return value != mean

        z = (value - mean) / std
        return abs(z) > self._threshold
