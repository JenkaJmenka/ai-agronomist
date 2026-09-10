"""Fault-type classification, sensor attribution, and reasoning-text generation.

Pure functions that map sensor values, incident data, and anomaly_type labels
to fault-type keys, affected-sensor lists, and human-readable reasoning text.
No I/O, no database access, no pandas.
"""

from __future__ import annotations

from typing import Any

from src.config import (
    CO2_RANGE,
    HUMIDITY_RANGE,
    LIGHT_RANGE,
    TEMP_RANGE,
)
from src.models.band_breach_detector import BAND_TYPE_TO_VOTE
from src.models.setpoint_scada_detector import (
    DEHUMIDIFICATION_FAILURE,
    HUMIDITY_SATURATION_RISK,
    OVER_HEATING,
    SENSOR_FLATLINE,
    THERMAL_DIVERGENCE,
    UNDER_HEATING,
)

_UNKNOWN_KEY: str = "unknown"

# Sensor columns that may appear in a sample's 'values' dict.
_SENSOR_VALUE_KEYS: tuple[str, ...] = (
    "temperature",
    "humidity",
    "co2",
    "light",
    "vpd",
    "soil_moisture",
    "root_zone_temp",
)

_SENSOR_BOUNDS: dict[str, tuple[float, float]] = {
    "temperature": TEMP_RANGE,
    "humidity": HUMIDITY_RANGE,
    "co2": CO2_RANGE,
    "light": LIGHT_RANGE,
}

# Maps SensorReading.anomaly_type to the Incident.fault_type taxonomy.
#
# Three sources write anomaly_type values to SensorReading rows:
#   1. synthetic_anomalies.py  — lowercase keys from _TYPE_PROFILES
#   2. setpoint_scada_detector — UPPER_CASE constants (e.g. UNDER_HEATING)
#   3. band_breach_detector    — UPPER_CASE constants (e.g. TEMP_BAND_HIGH)
#
# Fault keys are taken verbatim from comprehensive_pipeline._RULE_VOTE_PROFILES
# so the mapping is always consistent with what the pipeline writes to Incident.
#
# Dependency direction: src/api/services → src/models is already established
# by comprehensive_pipeline.py (lines 52, 83–92).  Importing BAND_TYPE_TO_VOTE
# and the SCADA constants here follows that same pattern.
#
# BAND_TYPE_TO_VOTE (band_breach_detector.py:29) already maps every band
# anomaly_type to the correct lowercase fault_key; we reuse it via **-unpack
# rather than restating 8 pairs (two maps that must stay in sync violates the
# no-duplication guardrail).
_ANOMALY_TYPE_TO_FAULT_TYPE: dict[str, str | None] = {
    # ── synthetic-injection labels (synthetic_anomalies.py _TYPE_PROFILES) ──
    "temperature_spike": "spike_fault",
    "temperature_drop":  "spike_fault",
    "humidity_spike":    "spike_fault",
    "humidity_drop":     "spike_fault",
    "co2_surge":         "spike_fault",
    "light_failure":     "spike_fault",
    "gradual_drift":     "calibration_drift",
    # 'random' perturbs all four sensors simultaneously — it is not a spike on
    # any single channel.  'multi_sensor' is explicitly multivariate — it is
    # not an accuracy decline.  Neither maps honestly to a single fault key;
    # return None and let value-based inference decide.
    "random":            None,
    "multi_sensor":      None,
    # Sentinel: marks normal background readings injected around synthetic
    # faults.  Not a fault — must not produce any fault classification.
    "synthetic_context": None,
    # ── setpoint-SCADA detector (setpoint_scada_detector.py:38–43) ──────────
    UNDER_HEATING:            "under_heating",
    OVER_HEATING:             "over_heating",
    DEHUMIDIFICATION_FAILURE: "dehumidification_failure",
    HUMIDITY_SATURATION_RISK: "humidity_saturation_risk",
    THERMAL_DIVERGENCE:       "thermal_divergence",
    # SENSOR_FLATLINE: a sensor frozen at constant value (zero variance) is
    # the canonical "stuck" failure in db/models.py taxonomy (stuck_fault).
    # comprehensive_pipeline._RULE_VOTE_PROFILES["sensor_flatline"] uses the
    # same assignment (fault_type="stuck_fault"), so this is consistent.
    SENSOR_FLATLINE:          "stuck_fault",
    # ── band-breach detector (band_breach_detector.py:29 BAND_TYPE_TO_VOTE) ──
    # Reuse the existing map — its values are the lowercase fault_keys used by
    # comprehensive_pipeline (temp_band_high, vpd_band_low, etc.).
    **BAND_TYPE_TO_VOTE,
}


# ─── fault-type classification ───────────────────────────────────────────────


def _fault_type_from_incident(incident: dict[str, Any] | None) -> str | None:
    """Extract fault_type from a matched incident, or None when no incident."""
    if incident is None:
        return None
    return incident.get("fault_type") or None


def _fault_type_from_anomaly_type(anomaly_type: str | None) -> str | None:
    """Map SensorReading.anomaly_type to the Incident.fault_type taxonomy.

    Returns None when anomaly_type is None, not recognised, or is the sentinel
    "synthetic_context" (which marks normal background readings that are not
    faults).  Callers should fall back to _infer_fault_type_from_values when
    this returns None.
    """
    if anomaly_type is None:
        return None
    return _ANOMALY_TYPE_TO_FAULT_TYPE.get(anomaly_type)


def _infer_fault_type_from_values(
    temperature: float | None,
    humidity: float | None,
    co2: float | None,
    light: float | None,
) -> str:
    """Infer fault_type from sensor values (fallback when no incident matched).

    Above upper bound -> spike_fault; below lower bound -> spike_fault;
    in-range -> unknown (z-score path is unreachable; see inline comment).
    """
    vals: dict[str, float | None] = {
        "temperature": temperature, "humidity": humidity,
        "co2": co2, "light": light,
    }
    above: list[str] = []
    below: list[str] = []
    zscores: dict[str, float] = {}
    for sensor, (lo, hi) in _SENSOR_BOUNDS.items():
        v = vals.get(sensor)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > hi:
            above.append(sensor)
        elif f < lo:
            below.append(sensor)
        else:
            mid = (lo + hi) / 2.0
            span = max(hi - lo, 1e-6)
            zscores[sensor] = abs(f - mid) / (span / 2.0)

    if above:
        return "spike_fault"
    if below:
        return "spike_fault"
    # In-range: z = |f - mid| / (span/2) <= 1.0 for all f in [lo, hi], so the
    # old "accuracy_decline" branch was unreachable dead code.  Without an
    # incident or range violation, the honest answer is "unknown".
    return _UNKNOWN_KEY


# ─── sensor attribution ─────────────────────────────────────────────────────


def _affected_sensors_from_incident(incident: dict[str, Any] | None) -> list[str]:
    """Extract affected_sensors list from a matched incident."""
    if incident is None:
        return []
    details = incident.get("root_cause_details") or {}
    sensors = details.get("affected_sensors") or []
    return list(sensors)


def _infer_sensors_from_values(
    temperature: float | None,
    humidity: float | None,
    co2: float | None,
    light: float | None,
) -> list[str]:
    """Infer affected sensors from sensor values via z-score deviation.

    Simplified port of infer_affected_sensors_from_row that works with plain
    Python values instead of a pd.Series.  Used as a fallback when no incident
    is matched (mirrors the z-score path in enrich_row/_zscore_flags).
    """
    vals: dict[str, float | None] = {
        "temperature": temperature,
        "humidity": humidity,
        "co2": co2,
        "light": light,
    }
    # Range violations are strong signals -> return those first.
    violated: list[str] = []
    zscores: dict[str, float] = {}
    for sensor, (lo, hi) in _SENSOR_BOUNDS.items():
        v = vals.get(sensor)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f < lo or f > hi:
            violated.append(sensor)
            zscores[sensor] = 99.0
        else:
            mid = (lo + hi) / 2.0
            span = max(hi - lo, 1e-6)
            zscores[sensor] = abs(f - mid) / (span / 2.0)

    if violated:
        return violated[:3]
    if zscores:
        # At least one sensor always flagged (mirrors _zscore_flags fallback).
        return [max(zscores, key=lambda k: zscores[k])]
    return [_UNKNOWN_KEY]


# ─── reasoning-text generation ───────────────────────────────────────────────

_SIMPLE_REASONING_UNITS: dict[str, str] = {
    "temperature": "°C",
    "humidity": "%",
    "co2": " ppm",
    "light": " W/m²",  # canonical unit — see LIGHT_CANONICAL_UNIT in src/config.py
}


def _simple_reasoning_from_values(channel: str, value: float | None) -> str:
    """Generate a compact reasoning string for the single channel that fired.

    Describes only the triggering channel — never emits clauses about channels
    that did not fire.  When the channel is a raw detector column name (e.g.
    ``measured_pipe_1_temp``) and has no entry in ``_SENSOR_BOUNDS``, the
    reasoning quotes the value without a spike/drop qualifier.

    Parameters
    ----------
    channel:
        The raw column name that triggered the alert (e.g. ``humidity``,
        ``measured_pipe_1_temp``).
    value:
        The sensor value at the time of the alert; None if unavailable.
    """
    if value is None:
        return (
            f"Anomaly detected on {channel!r} — value not recorded"
        )
    unit = _SIMPLE_REASONING_UNITS.get(channel, "")
    bounds = _SENSOR_BOUNDS.get(channel)
    if bounds is not None:
        lo, hi = bounds
        if value > hi:
            return (
                f"Unexpected {channel} spike: {value:.1f}{unit}"
                f" ({hi:.0f}{unit} upper bound)"
            )
        if value < lo:
            return (
                f"Unexpected {channel} drop: {value:.1f}{unit}"
                f" ({lo:.0f}{unit} lower bound)"
            )
    return f"Unusual {channel} reading: {value:.1f}{unit}"
