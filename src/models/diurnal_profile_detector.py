"""Diurnal profile deviation detector.

Checks whether a sensor reading at a given hour matches the historical
statistical profile for that hour.  A Z-score exceeding the configured
threshold indicates an anomalous departure from the expected diurnal
pattern.

The detector applies a per-sensor standard-deviation floor so that hour
bins with artificially tight distributions (from short history or stable
climate control) do not produce false alarms on natural day-to-day
variation.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from src.config import DIURNAL_STD_FLOOR, DIURNAL_Z_THRESHOLD


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
        sensor_name: Optional[str] = None,
    ) -> bool:
        """Check a single reading against its hourly historical profile.

        Args:
            value: Current sensor reading.
            hour: Hour of day (0-23).
            hourly_stats: Mapping of hour to ``(mean, std)`` tuple from
                historical data.
            sensor_name: Optional sensor name used to look up the per-sensor
                std floor from ``DIURNAL_STD_FLOOR``.  When ``None`` no floor
                is applied beyond the existing zero guard.

        Returns:
            ``True`` if the absolute Z-score exceeds the threshold.
        """
        stats = hourly_stats.get(hour)
        if stats is None:
            return False

        mean, std = stats

        # Apply per-sensor std floor when the sensor name is provided.
        floor = DIURNAL_STD_FLOOR.get(sensor_name, 0.0) if sensor_name else 0.0
        effective_std = max(std, floor)

        if effective_std <= 0:
            return value != mean

        z = (value - mean) / effective_std
        return abs(z) > self._threshold
