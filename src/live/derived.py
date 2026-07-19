"""Derived channel computations for the live monitoring pipeline.

Currently provides VPD (Vapour Pressure Deficit) from temperature and
humidity using the Magnus formula.  Pure functions, no state.
"""

from __future__ import annotations

import math


def compute_vpd(temperature_c: float, relative_humidity_pct: float) -> float:
    """Compute Vapour Pressure Deficit (kPa) via the Magnus formula.

    Parameters
    ----------
    temperature_c:
        Air temperature in degrees Celsius.
    relative_humidity_pct:
        Relative humidity as a percentage (0-100).

    Returns
    -------
    float
        VPD in kPa, clamped to >= 0.0.  Returns 0.0 if any input is
        non-finite to prevent propagation of NaN/Inf through the pipeline.

    References
    ----------
    Magnus formula:
        SVP = 0.6108 * exp(17.27 * T / (T + 237.3))
        VPD = SVP * (1 - RH/100)
    """
    if not math.isfinite(temperature_c) or not math.isfinite(relative_humidity_pct):
        return 0.0

    svp_kpa = 0.6108 * math.exp(17.27 * temperature_c / (temperature_c + 237.3))
    vpd = svp_kpa * (1.0 - relative_humidity_pct / 100.0)

    if not math.isfinite(vpd):
        return 0.0

    return max(0.0, vpd)
