"""Psychrometric validation using the Tetens equation.

Pure functions only — no state. Validates the thermodynamic consistency of
temperature, relative humidity, and measured humidity deficit readings by
comparing measured HD against a value derived from the Tetens equation.

All thresholds are sourced from ``src.config``.
"""

import math
from typing import Literal

from src.config import PSYCHROMETRIC_HD_TOLERANCE

# Absolute-zero guard: temperatures at or below this are physically impossible.
_ABSOLUTE_ZERO_C: float = -273.15


def _validate_inputs(t_air: float, rh: float) -> None:
    """Guard against unphysical temperature or humidity values.

    Raises:
        ValueError: If *t_air* is at or below absolute zero, or *rh* is
            outside [0, 100].
    """
    if t_air <= _ABSOLUTE_ZERO_C:
        raise ValueError(
            f"Temperature {t_air}°C is at or below absolute zero "
            f"({_ABSOLUTE_ZERO_C}°C)"
        )
    if rh < 0.0 or rh > 100.0:
        raise ValueError(
            f"Relative humidity {rh}% is outside the valid range [0, 100]"
        )


def calculate_saturation_vapor_pressure(t_air: float) -> float:
    """Tetens equation: saturation vapour pressure *e_s* in kPa.

    Args:
        t_air: Air temperature in °C.

    Returns:
        Saturation vapour pressure in kPa.

    Raises:
        ValueError: If *t_air* is at or below absolute zero.
    """
    if t_air <= _ABSOLUTE_ZERO_C:
        raise ValueError(
            f"Temperature {t_air}°C is at or below absolute zero "
            f"({_ABSOLUTE_ZERO_C}°C)"
        )
    return 0.61078 * math.exp((17.27 * t_air) / (t_air + 237.3))


def calculate_humidity_deficit(t_air: float, rh: float) -> float:
    """Compute the humidity deficit (HD) in g/m³.

    Uses the Tetens equation to derive the saturation vapour pressure, then
    calculates the absolute humidity deficit from the difference between
    saturated and actual vapour pressures.

    Args:
        t_air: Air temperature in °C.
        rh: Relative humidity in % (0–100).

    Returns:
        Humidity deficit in g/m³.

    Raises:
        ValueError: If inputs are unphysical (T ≤ -273.15 °C or RH ∉ [0, 100]).
    """
    _validate_inputs(t_air, rh)

    e_s = calculate_saturation_vapor_pressure(t_air)
    e_a = e_s * (rh / 100.0)
    t_kelvin = t_air + 273.15

    return (216.7 * (e_s - e_a)) / t_kelvin


def validate_row(
    t_air: float,
    rh: float,
    hd_measured: float,
) -> Literal["OK", "FAILED_PSYCHROMETRIC_LIMIT"]:
    """Flag a row if its measured HD deviates from the Tetens-derived value.

    Compares ``|HD_measured - HD_calc|`` against the tolerance defined in
    ``PSYCHROMETRIC_HD_TOLERANCE``.

    Args:
        t_air: Air temperature in °C.
        rh: Relative humidity in % (0–100).
        hd_measured: Measured humidity deficit in g/m³.

    Returns:
        ``"OK"`` when within tolerance, ``"FAILED_PSYCHROMETRIC_LIMIT"``
        otherwise.

    Raises:
        ValueError: If inputs are unphysical.
    """
    _validate_inputs(t_air, rh)

    hd_calc = calculate_humidity_deficit(t_air, rh)

    if abs(hd_measured - hd_calc) > PSYCHROMETRIC_HD_TOLERANCE:
        return "FAILED_PSYCHROMETRIC_LIMIT"

    return "OK"
