"""
Enrich anomaly table rows with diagnosis from incident memory or z-score fallback.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.config import CO2_RANGE, HUMIDITY_RANGE, LIGHT_RANGE, SENSOR_UNITS, TEMP_RANGE

SENSOR_COLUMNS = ["temperature", "humidity", "co2", "light"]
_SENSOR_BOUNDS = {
    "temperature": TEMP_RANGE,
    "humidity": HUMIDITY_RANGE,
    "co2": CO2_RANGE,
    "light": LIGHT_RANGE,
}
STRONG_ATTRIBUTION = 0.25
HINT_MAX_LEN = 120


def _zscore_flags(row: pd.Series) -> Dict[str, bool]:
    """Statistical fallback when no incident match (range + z-score).

    If no sensor crosses the z-score or range threshold, the most deviant
    sensor is flagged anyway so the operator always has a highlighted cell
    to inspect for multivariate / sequence anomalies.
    """
    zscores: Dict[str, float] = {}
    flags: Dict[str, bool] = {}
    for col in SENSOR_COLUMNS:
        if col not in row or col not in _SENSOR_BOUNDS:
            continue
        lo, hi = _SENSOR_BOUNDS[col]
        val = float(row[col])
        if val < lo or val > hi:
            flags[col] = True
            zscores[col] = 99.0  # out-of-range always wins
            continue
        mid = (lo + hi) / 2.0
        span = max(hi - lo, 1e-6)
        z = abs(val - mid) / (span / 2.0)
        zscores[col] = z
        flags[col] = z > 1.2

    if zscores and not any(flags.values()):
        most_deviant = max(zscores, key=lambda c: zscores[c])
        flags[most_deviant] = True

    return flags


def _hint_for_sensor(
    sensor: str,
    flagged: bool,
    attribution: float,
    pattern_type: Optional[str],
    value: Optional[float] = None,
    affected_sensors: Optional[List[str]] = None,
) -> str:
    """Generate a user-friendly description for a flagged sensor reading."""
    if not flagged:
        return ""

    bounds = _SENSOR_BOUNDS.get(sensor)
    unit = SENSOR_UNITS.get(sensor, "")
    lo, hi = bounds if bounds else (None, None)

    text = ""
    if value is not None and lo is not None and hi is not None:
        if value > hi:
            delta = value - hi
            text = (
                f"Unexpected {sensor} spike: {value:.1f}{unit} detected, "
                f"{delta:.1f}{unit} above normal ({lo:.0f}–{hi:.0f}{unit})"
            )
        elif value < lo:
            delta = lo - value
            text = (
                f"Unexpected {sensor} drop: {value:.1f}{unit} detected, "
                f"{delta:.1f}{unit} below normal ({lo:.0f}–{hi:.0f}{unit})"
            )

    if not text:
        val_str = f"{value:.1f}{unit}" if value is not None else "unknown"
        if attribution >= STRONG_ATTRIBUTION:
            text = f"Unusual {sensor} reading ({val_str}) — strong contributor to anomaly"
        elif attribution > 0:
            text = f"Unusual {sensor} reading ({val_str}) — contributing to anomaly"
        elif lo is not None and hi is not None and value is not None and lo <= value <= hi:
            # Value is within range but model still flagged the sequence as anomalous —
            # multivariate / temporal pattern the model detected across the full sequence.
            text = (
                f"{sensor.capitalize()} reading {val_str} is within normal range "
                f"({lo:.0f}–{hi:.0f}{unit}) but the model detected an unusual "
                f"sequence pattern — run inference to get a full diagnosis"
            )
        elif lo is not None and hi is not None:
            text = f"{sensor.capitalize()} outside normal range ({lo:.0f}–{hi:.0f}{unit})"
        else:
            text = f"{sensor.capitalize()} flagged as anomalous"

    if pattern_type:
        sensors_str = ", ".join(affected_sensors) if affected_sensors else sensor
        text = f"{text} — part of {pattern_type} pattern affecting {sensors_str}"

    return text


def enrich_row(
    row: pd.Series,
    incident: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return enrichment dict for one anomaly row."""
    details = (incident or {}).get("root_cause_details") or {}
    attribution = details.get("attribution") or {}
    affected = details.get("affected_sensors") or []
    pattern_type = details.get("pattern_type") or (incident or {}).get("pattern_type")

    flags: Dict[str, bool] = {}
    hints: Dict[str, str] = {}

    if attribution:
        for col in SENSOR_COLUMNS:
            attr_val = float(attribution.get(col, 0.0))
            flags[col] = col in affected or attr_val >= 0.1
            sensor_val = float(row[col]) if col in row.index else None
            hints[col] = _hint_for_sensor(
                col,
                flags[col],
                attr_val,
                pattern_type,
                value=sensor_val,
                affected_sensors=affected,
            )
    else:
        flags = _zscore_flags(row)
        fallback_affected = [c for c, f in flags.items() if f]

        # Classify flags into range violations vs. in-range deviations.
        range_violations = [
            c for c in SENSOR_COLUMNS
            if flags.get(c) and c in row.index
            and (float(row[c]) > _SENSOR_BOUNDS[c][1] or float(row[c]) < _SENSOR_BOUNDS[c][0])
        ]
        in_range_flags = [c for c in SENSOR_COLUMNS if flags.get(c) and c not in range_violations]

        # Derive a basic pattern label from range violations when no incident is matched.
        if pattern_type is None and any(flags.values()):
            above = [
                c for c in range_violations
                if c in row.index and float(row[c]) > _SENSOR_BOUNDS[c][1]
            ]
            below = [
                c for c in range_violations
                if c in row.index and float(row[c]) < _SENSOR_BOUNDS[c][0]
            ]
            if above:
                pattern_type = "spike"
            elif below:
                pattern_type = "drop"
            elif in_range_flags:
                pattern_type = "multivariate"
            else:
                pattern_type = "deviation"

        for col in SENSOR_COLUMNS:
            sensor_val = float(row[col]) if col in row.index else None
            hints[col] = _hint_for_sensor(
                col,
                flags.get(col, False),
                0.0,
                pattern_type,
                value=sensor_val,
                affected_sensors=fallback_affected,
            )

    hint_parts = [hints[c] for c in SENSOR_COLUMNS if hints.get(c)]
    hint_summary = "; ".join(hint_parts)[:HINT_MAX_LEN]

    return {
        "flags": flags,
        "hints": hints,
        "attribution": attribution,
        "pattern_type": pattern_type or "",
        "affected_sensors": affected if affected else [c for c, f in flags.items() if f],
        "hint_summary": hint_summary,
        "incident_id": (incident or {}).get("id"),
        "fault_type": (incident or {}).get("fault_type"),
    }


def enrich_dataframe(
    anomalies_df: pd.DataFrame,
    incidents: Optional[List[Dict[str, Any]]] = None,
) -> pd.DataFrame:
    """Add diagnosis columns to anomaly rows."""
    if anomalies_df.empty:
        return anomalies_df

    work = anomalies_df.copy()

    enrichments = []
    for _, row in work.iterrows():
        matched = None
        if incidents:
            matched = _nearest_incident(row["timestamp"], incidents)
        enrichments.append(enrich_row(row, matched))

    work["pattern_type"] = [e["pattern_type"] for e in enrichments]
    work["affected_sensors"] = [
        ", ".join(e["affected_sensors"]) for e in enrichments
    ]
    work["hint_summary"] = [e["hint_summary"] for e in enrichments]
    work["_enrichment"] = enrichments
    return work


def _nearest_incident(
    timestamp: pd.Timestamp,
    incidents: List[Dict[str, Any]],
    window_minutes: int = 15,
) -> Optional[Dict[str, Any]]:
    """Pick incident closest in time within ±window_minutes."""
    if not incidents:
        return None
    ts = pd.to_datetime(timestamp)
    best: Optional[Dict[str, Any]] = None
    best_delta = pd.Timedelta(minutes=window_minutes + 1)
    for inc in incidents:
        detected = inc.get("detected_at")
        if detected is None:
            continue
        inc_ts = pd.to_datetime(detected)
        delta = abs(ts - inc_ts)
        if delta <= pd.Timedelta(minutes=window_minutes) and delta < best_delta:
            best = inc
            best_delta = delta
    return best
