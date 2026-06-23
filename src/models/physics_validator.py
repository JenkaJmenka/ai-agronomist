"""
Physics-informed validation layer for greenhouse anomaly detection.

Applies domain-specific physical rules (psychrometric relationships,
time-of-day plausibility, gas concentration bounds, temperature limits)
to either suppress false positives or confirm real faults before the
statistical ensemble runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from src.config import HUMIDITY_RANGE, TEMP_RANGE


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of physics-based anomaly validation.

    Attributes:
        suppressed: True if the anomaly should be discarded
            (physically explained, not a real fault).
        confirmed: True if the anomaly is confirmed by physics rules
            (bypass ensemble, mark as anomaly directly).
        reason: Short machine-readable tag describing the rule that
            fired (empty string when no rule matched).
    """

    suppressed: bool
    confirmed: bool
    reason: str


# Temperature trend threshold (deg C per timestep) for psychrometric rule.
_TEMP_TREND_THRESHOLD: float = 0.5

# Humidity trend threshold (% per timestep) for psychrometric rule.
_HUM_TREND_THRESHOLD: float = 1.0

# Light intensity threshold (lux) for nocturnal-light rule.
_NOCTURNAL_LIGHT_THRESHOLD: float = 50.0

# Hours (UTC) considered "night" for the nocturnal-light rule.
_NIGHT_HOURS: set[int] = {22, 23, 0, 1, 2, 3, 4}

# CO2 plausibility bounds (ppm).
_CO2_LOW: float = 200.0
_CO2_HIGH: float = 2000.0

# Temperature plausibility bounds (deg C).
_TEMP_LOW: float = 5.0
_TEMP_HIGH: float = 45.0


class PhysicsValidator:
    """Stateless physics-rule engine for greenhouse sensor sequences.

    All rules operate on denormalized (physical-unit) sensor values.
    Column order: ``[temperature, humidity, co2, light]``.
    """

    @staticmethod
    def validate(
        sequence: np.ndarray,
        timestamp: datetime,
        utc_offset_hours: int = 0,
    ) -> ValidationResult:
        """Apply physics rules to a sensor sequence.

        Rules are evaluated in priority order; the first match wins.
        Suppression rules are checked before confirmation rules so
        that physically-normal gradual changes are not flagged.

        Args:
            sequence: Array of shape ``(seq_len, 4)`` in physical units.
            timestamp: Observation timestamp (used for time-of-day rules).
            utc_offset_hours: Offset from UTC for the greenhouse location.
                Defaults to 0 (UTC).  E.g. pass ``-5`` for US Eastern,
                ``+2`` for Eastern European time.

        Returns:
            A ``ValidationResult`` indicating whether the anomaly is
            suppressed, confirmed, or unclassified.
        """
        if sequence.ndim != 2 or sequence.shape[1] < 4:
            return ValidationResult(
                suppressed=False, confirmed=False, reason=""
            )

        # Rule 1: Psychrometric impossibility
        temp_diff = np.diff(sequence[:, 0])
        hum_diff = np.diff(sequence[:, 1])
        temp_trend = float(np.mean(temp_diff))
        hum_trend = float(np.mean(hum_diff))

        if temp_trend > _TEMP_TREND_THRESHOLD and hum_trend > _HUM_TREND_THRESHOLD:
            return ValidationResult(
                suppressed=False,
                confirmed=True,
                reason="psychrometric_impossibility",
            )

        # Rule 2: Nocturnal light (timezone-aware)
        local_hour = (timestamp.hour + utc_offset_hours) % 24
        max_light = float(np.max(sequence[:, 3]))

        if max_light > _NOCTURNAL_LIGHT_THRESHOLD and local_hour in _NIGHT_HOURS:
            return ValidationResult(
                suppressed=False,
                confirmed=True,
                reason="nocturnal_light",
            )

        # Rule 3: CO2 out of range
        co2_mean = float(np.mean(sequence[:, 2]))
        if co2_mean < _CO2_LOW or co2_mean > _CO2_HIGH:
            return ValidationResult(
                suppressed=False,
                confirmed=True,
                reason="co2_out_of_range",
            )

        # Rule 4: Temperature bounds
        if np.any(sequence[:, 0] < _TEMP_LOW) or np.any(
            sequence[:, 0] > _TEMP_HIGH
        ):
            return ValidationResult(
                suppressed=False,
                confirmed=True,
                reason="temperature_out_of_range",
            )

        # Rule 5 (suppression): Normal diurnal variation
        # Both temp and humidity are co-moving (changing in the same
        # direction, both non-zero) with gradual per-step changes
        # (below trend thresholds), and all values stay within normal
        # physical ranges.  Only fires after all confirmation rules
        # pass — a real fault always takes priority over suppression.
        is_temp_changing = temp_trend != 0.0
        is_hum_changing = hum_trend != 0.0
        is_co_moving = (
            is_temp_changing
            and is_hum_changing
            and (temp_trend > 0) == (hum_trend > 0)
        )
        is_temp_gradual = abs(temp_trend) < _TEMP_TREND_THRESHOLD
        is_hum_gradual = abs(hum_trend) < _HUM_TREND_THRESHOLD
        temp_in_range = bool(
            np.all(sequence[:, 0] >= TEMP_RANGE[0])
            and np.all(sequence[:, 0] <= TEMP_RANGE[1])
        )
        hum_in_range = bool(
            np.all(sequence[:, 1] >= HUMIDITY_RANGE[0])
            and np.all(sequence[:, 1] <= HUMIDITY_RANGE[1])
        )
        if (
            is_co_moving
            and is_temp_gradual
            and is_hum_gradual
            and temp_in_range
            and hum_in_range
        ):
            return ValidationResult(
                suppressed=True,
                confirmed=False,
                reason="normal_diurnal_variation",
            )

        # No rule matched
        return ValidationResult(
            suppressed=False, confirmed=False, reason=""
        )
