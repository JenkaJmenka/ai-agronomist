"""Fertigation signal generators for the mock SCADA source.

Provides soil_moisture (irrigation sawtooth) and root_zone_temp (damped
diurnal) signals used by the ``mock_fertigation`` source.
"""

from __future__ import annotations

import math
import random
from typing import Dict

_TWO_PI: float = 2.0 * math.pi

# Soil moisture depletion rate per tick (% per tick, before noise).
_SM_DEPLETION_RATE: float = 0.05

# Soil moisture refill threshold and target.
_SM_REFILL_TRIGGER: float = 62.0
_SM_REFILL_TARGET: float = 85.0
_SM_REFILL_TICKS: int = 3

# Soil moisture operational bounds (hard clamp).
_SM_FLOOR: float = 55.0
_SM_CEILING: float = 90.0

# Root zone temperature parameters.
_RZT_BASE: float = 21.0
_RZT_AMPLITUDE: float = 2.0
# Phase lag from air temp: air peaks at hour 12 (phase_offset=6 in diurnal),
# root zone lags ~2h -> phase_offset = 8.
_RZT_PHASE_OFFSET: float = 8.0
_RZT_NOISE_STD: float = 0.15

# Root zone temperature clamps.
_RZT_FLOOR: float = 16.0
_RZT_CEILING: float = 28.0


class FertigationSignalState:
    """Mutable state for the fertigation signal generator.

    Tracks soil moisture level and refill state across ticks.
    """

    __slots__ = ("soil_moisture", "_refill_remaining", "_rng")

    def __init__(self, rng: random.Random, initial_moisture: float = 78.0) -> None:
        self.soil_moisture: float = initial_moisture
        self._refill_remaining: int = 0
        self._rng = rng

    def generate(self, hour_frac: float) -> Dict[str, float]:
        """Generate soil_moisture and root_zone_temp for the current tick.

        Parameters
        ----------
        hour_frac:
            Fractional hour of day in UTC (0.0 to 24.0).

        Returns
        -------
        Dict with ``soil_moisture`` and ``root_zone_temp`` values.
        """
        sm = self._next_soil_moisture()
        rzt = self._next_root_zone_temp(hour_frac)
        return {"soil_moisture": sm, "root_zone_temp": rzt}

    def _next_soil_moisture(self) -> float:
        """Advance soil moisture one tick (sawtooth with refill events)."""
        if self._refill_remaining > 0:
            # Refilling: linear ramp toward target over remaining ticks.
            step = (_SM_REFILL_TARGET - self.soil_moisture) / self._refill_remaining
            self.soil_moisture += step
            self._refill_remaining -= 1
        else:
            # Depleting with noise.
            noise = self._rng.gauss(0, 0.02)
            self.soil_moisture -= _SM_DEPLETION_RATE + noise

            # Check if irrigation trigger fires.
            if self.soil_moisture <= _SM_REFILL_TRIGGER:
                self._refill_remaining = _SM_REFILL_TICKS

        # Hard clamp to operational bounds.
        self.soil_moisture = max(_SM_FLOOR, min(_SM_CEILING, self.soil_moisture))
        return self.soil_moisture

    def _next_root_zone_temp(self, hour_frac: float) -> float:
        """Damped diurnal around _RZT_BASE with small noise."""
        sine = math.sin(
            _TWO_PI * (hour_frac - _RZT_PHASE_OFFSET) / 24.0
        )
        val = _RZT_BASE + _RZT_AMPLITUDE * sine + self._rng.gauss(0, _RZT_NOISE_STD)
        return max(_RZT_FLOOR, min(_RZT_CEILING, val))
