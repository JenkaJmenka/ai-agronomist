"""Derived channel computations for the live monitoring pipeline.

Currently provides VPD (Vapour Pressure Deficit) from temperature and
humidity using the Magnus formula.  Pure functions, no state.
"""

from __future__ import annotations

import math

from src.live.events import LiveReadingEvent
from src.models.psychrometric_validator import calculate_saturation_vapor_pressure


def attach_derived_channels(event: LiveReadingEvent) -> None:
    """Attach derived channels (currently VPD) to a validated reading event.

    Adds the ``vpd`` channel when both ``temperature`` and ``humidity`` are
    present.  Mutates ``event.channels``/``event.sources`` in place — this is
    safe because :func:`compute_vpd` guarantees a finite return value (it
    returns ``0.0`` for any non-finite input), so the mutation can never
    introduce NaN/Inf into an already-validated event.  The scorer only
    consumes the canonical ML channels, so VPD never affects scoring.
    """
    temperature = event.channels.get("temperature")
    humidity = event.channels.get("humidity")
    if temperature is None or humidity is None:
        return
    event.channels["vpd"] = compute_vpd(temperature, humidity)
    event.sources["vpd"] = "derived"


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
    Magnus/Tetens formula (single source in
    :func:`~src.models.psychrometric_validator.calculate_saturation_vapor_pressure`):
        SVP = 0.61078 * exp(17.27 * T / (T + 237.3))
        VPD = SVP * (1 - RH/100)
    """
    if not math.isfinite(temperature_c) or not math.isfinite(relative_humidity_pct):
        return 0.0

    # Single source for the Magnus/Tetens saturation vapour pressure —
    # uses 0.61078 (the canonical Tetens constant shared with the
    # psychrometric validator).  Previous local constant was 0.6108;
    # live VPD values shift only in the 4th decimal place.
    svp_kpa = calculate_saturation_vapor_pressure(temperature_c)
    vpd = svp_kpa * (1.0 - relative_humidity_pct / 100.0)

    if not math.isfinite(vpd):
        return 0.0

    return max(0.0, vpd)
