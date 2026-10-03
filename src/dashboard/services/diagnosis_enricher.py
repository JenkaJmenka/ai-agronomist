"""
Enrich anomaly table rows with diagnosis from incident memory or z-score fallback.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from src.config import CO2_RANGE, HUMIDITY_RANGE, LIGHT_RANGE, SENSOR_UNITS, TEMP_RANGE
from src.models.fault_classifier import FaultClassifier

SENSOR_COLUMNS = ["temperature", "humidity", "co2", "light"]

# Map canonical display slots to common CSV / raw-reading column names.
_CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "temperature": (
        "temperature",
        "measured_greenhouse_temp",
        "greenhouse_temp",
        "measured_plant_temp",
        "plant_temp",
        "outdoor_temp",
    ),
    "humidity": (
        "humidity",
        "measured_relative_humidity",
        "relative_humidity",
        "measured_humidity",
    ),
    "co2": ("co2", "gas_co2", "measured_co2"),
    "light": (
        "light",
        "light_intensity",
        "illuminance",
        "measured_light",
        "par",
        "ppfd",
    ),
}


def resolve_canonical_sensor_values(row: pd.Series) -> dict[str, float | None]:
    """Read temp/humidity/co2/light from canonical or vendor column names."""
    resolved: dict[str, float | None] = {}
    for canon, aliases in _CANONICAL_ALIASES.items():
        val = None
        for name in aliases:
            if name in row.index and pd.notna(row[name]):
                try:
                    val = float(row[name])
                    break
                except (TypeError, ValueError):
                    continue
        resolved[canon] = val
    return resolved


def row_with_canonical_sensors(row: pd.Series) -> pd.Series:
    """Return a copy of *row* with canonical sensor columns filled from aliases."""
    out = row.copy()
    for canon, val in resolve_canonical_sensor_values(row).items():
        if val is not None:
            out[canon] = val
    return out


def canonical_slot_for_column(column: str) -> str | None:
    """Map a raw or canonical column name to a shared sensor slot."""
    norm = str(column).strip().lower()
    for canon, aliases in _CANONICAL_ALIASES.items():
        if norm == canon or norm in aliases:
            return canon
    return None


def columns_refer_to_same_sensor(column_a: str, column_b: str) -> bool:
    """True when two column names refer to the same sensor channel."""
    if column_a == column_b:
        return True
    slot_a = canonical_slot_for_column(column_a)
    slot_b = canonical_slot_for_column(column_b)
    return slot_a is not None and slot_a == slot_b


_METADATA_COLS = frozenset({
    "timestamp",
    "source",
    "anomaly_score",
    "anomaly_columns",
    "anomaly_detectors",
    "_enrichment",
})


def _climate_deviation_score(column: str, value: float) -> float:
    """Normalized deviation from expected range for a climate column name."""
    norm = column.lower()
    if "temp" in norm:
        lo, hi = TEMP_RANGE
    elif "humid" in norm and "deficit" not in norm:
        lo, hi = HUMIDITY_RANGE
    elif "co2" in norm:
        lo, hi = CO2_RANGE
    elif any(token in norm for token in ("light", "lux", "illuminance", "par", "ppfd")):
        lo, hi = LIGHT_RANGE
    else:
        return 0.0
    mid = (lo + hi) / 2.0
    span = max(hi - lo, 1e-6)
    return abs(value - mid) / (span / 2.0)


def infer_affected_sensors_from_row(row: pd.Series) -> list[str]:
    """Infer contributing sensors from upload metadata or row values."""
    persisted = row.get("anomaly_columns")
    if isinstance(persisted, list) and persisted:
        return list(persisted)
    if isinstance(persisted, str) and persisted.strip():
        return [persisted.strip()]

    from src.dashboard.components.sensor_columns import is_climate_column

    scored: list[tuple[str, float]] = []
    for col in row.index:
        if col in _METADATA_COLS or not is_climate_column(str(col)):
            continue
        try:
            val = float(row[col])
        except (TypeError, ValueError):
            continue
        if not np.isfinite(val):
            continue
        deviation = _climate_deviation_score(str(col), val)
        if deviation >= 1.0:
            scored.append((str(col), deviation))

    scored.sort(key=lambda item: item[1], reverse=True)
    if scored:
        return [col for col, _ in scored[:3]]

    # Fall back to canonical z-score flags mapped back to real column names.
    flags = _zscore_flags(row_with_canonical_sensors(row))
    affected: list[str] = []
    for canon, flagged in flags.items():
        if not flagged:
            continue
        for alias in _CANONICAL_ALIASES.get(canon, (canon,)):
            if alias in row.index:
                affected.append(alias)
                break
        else:
            affected.append(canon)
    return affected


def format_sensor_label(column: str) -> str:
    """Human-readable sensor label for reasoning text."""
    return str(column).replace("_", " ").replace("measured ", "").title()


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
        # z = |val - mid| / (span/2) ≤ 1.0 for any val ∈ [lo, hi], so
        # z > 1.2 is always False — no in-range value can exceed this bound.
        # The dead expression `flags[col] = z > 1.2` was removed; the fallback
        # below (if not any(flags.values())) ensures the most-deviant in-range
        # sensor is still flagged for multivariate anomalies.
        # anomaly_group_service._infer_fault_type_from_values has the same
        # bound; the old "accuracy_decline" branch there was dead for the
        # same reason and has been removed (see that module's inline comment).

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


def _infer_fault_type(
    pattern_type: Optional[str],
    *,
    incident_fault_type: Optional[str] = None,
    details_fault_type: Optional[str] = None,
    detectors: Optional[List[str]] = None,
) -> Optional[str]:
    """Resolve fault type from incident data or pattern-based fallback."""
    if incident_fault_type:
        return incident_fault_type
    if details_fault_type:
        return details_fault_type

    if detectors:
        if "statistical" in detectors and "lstm" not in detectors:
            return "spike_fault"
        if "lstm" in detectors and "statistical" not in detectors:
            return "accuracy_decline"
        if detectors:
            return "accuracy_decline"

    pattern = (pattern_type or "").strip().lower()
    if not pattern:
        return None

    if pattern in {"statistical", "rolling_z_score", "z-score", "z_score"}:
        return "spike_fault"
    if pattern == "lstm":
        return "accuracy_decline"

    classified = FaultClassifier.classify(pattern, {})
    if classified != "unknown":
        return classified

    if pattern in {"drop", "spike"}:
        return "spike_fault"
    if pattern == "multivariate":
        return "accuracy_decline"
    if pattern in {"deviation", "drift"}:
        return "calibration_drift"

    return "unknown"


def resolve_stuck_duration_hours(row: Any) -> Optional[float]:
    """Hours frozen at detection for a stuck_fault row, else ``None``.

    Shared by every dashboard display site that needs a stuck hit's
    duration: this function, ``monitoring_jump.py``, and ``1_Monitoring.py``
    all made the same decision independently before being consolidated
    here. Prefers ``frozen_hours_at_detection``; falls back to the legacy
    ``z_score`` overload ONLY when ``detector == "stuck_fault"`` (never
    based on whether ``z_score`` is merely present, since a legacy
    STATISTICAL row also has a numeric ``z_score``, and that is exactly the
    misread this fallback must not cause). Returns ``None`` for non-stuck
    rows, or when neither field carries a value. Mirrors
    ``episode_grouper.py::_stuck_fault_duration``'s fallback, and ``row``
    may be a ``dict`` or a ``pd.Series`` -- both support ``.get()``.
    """
    if row.get("detector") != "stuck_fault":
        return None
    frozen_hours = row.get("frozen_hours_at_detection")
    if frozen_hours is None:
        frozen_hours = row.get("z_score")
    return frozen_hours


def _build_reasoning(
    *,
    incident: Optional[Dict[str, Any]],
    details: Dict[str, Any],
    hints: Dict[str, str],
    pattern_type: Optional[str],
    affected_sensors: List[str],
    detectors: Optional[List[str]] = None,
    row: Optional[pd.Series] = None,
) -> str:
    """Human-readable explanation for the anomaly row."""
    parts: List[str] = []

    if row is not None:
        try:
            detector = str(row.get("detector") or "")
            col_name = row.get("column")
            if detector == "stuck_fault":
                frozen_hours = resolve_stuck_duration_hours(row)
                if frozen_hours is not None:
                    if col_name:
                        slot = canonical_slot_for_column(str(col_name)) or str(col_name)
                        label = format_sensor_label(slot)
                        parts.append(f"Frozen {float(frozen_hours):.1f}h on {label}")
            else:
                z_val = row.get("z_score")
                if z_val is not None and not pd.isna(z_val):
                    z = float(z_val)
                    method = str(row.get("method", "rolling")).replace("_", " ")
                    if col_name:
                        slot = canonical_slot_for_column(str(col_name)) or str(col_name)
                        label = format_sensor_label(slot)
                        parts.append(f"{method.title()} z-score {z:.1f}σ on {label}")
        except (TypeError, ValueError):
            pass

    if incident:
        root_cause = incident.get("root_cause")
        if root_cause:
            parts.append(str(root_cause))
        for key in ("reasoning", "pattern_explanation"):
            value = details.get(key)
            if value:
                parts.append(str(value))

    detector_list = list(detectors or [])
    if detector_list and affected_sensors and row is not None:
        primary = affected_sensors[0]
        label = format_sensor_label(primary)
        try:
            reading = float(row[primary]) if primary in row.index else None
        except (TypeError, ValueError):
            reading = None
        if "statistical" in detector_list:
            if reading is not None:
                parts.append(
                    f"Rolling z-score threshold exceeded on {label} (reading {reading:.2f})"
                )
            else:
                parts.append(f"Rolling z-score threshold exceeded on {label}")
        if "lstm" in detector_list:
            parts.append(
                "LSTM sequence reconstruction error exceeded the calibrated upload threshold"
            )

    hint_parts = [hints[col] for col in SENSOR_COLUMNS if hints.get(col)]
    if hint_parts:
        parts.extend(hint_parts)

    if not parts and pattern_type:
        sensors = ", ".join(format_sensor_label(s) for s in affected_sensors) if affected_sensors else "sensor channels"
        parts.append(f"Detected {pattern_type} pattern affecting {sensors}")

    if not parts:
        return "Anomaly score exceeded threshold — no detailed diagnosis available yet"

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique_parts: List[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique_parts.append(part)
    return "; ".join(unique_parts)


def enrich_row(
    row: pd.Series,
    incident: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return enrichment dict for one anomaly row."""
    row = row_with_canonical_sensors(row)
    details = (incident or {}).get("root_cause_details") or {}
    attribution = details.get("attribution") or {}
    affected = list(details.get("affected_sensors") or [])
    pattern_type = details.get("pattern_type") or (incident or {}).get("pattern_type")

    # Cold-run / upload metadata on RawReading rows.
    persisted_cols = row.get("anomaly_columns")
    detectors_raw = row.get("anomaly_detectors")
    detectors = list(detectors_raw) if isinstance(detectors_raw, list) else []

    if isinstance(persisted_cols, list) and persisted_cols:
        affected = affected or persisted_cols
    inferred = infer_affected_sensors_from_row(row)
    if not affected:
        affected = inferred

    if not pattern_type:
        if "statistical" in detectors and "lstm" in detectors:
            pattern_type = "multivariate"
        elif "statistical" in detectors:
            pattern_type = "statistical"
        elif "lstm" in detectors:
            pattern_type = "lstm"

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
    affected_list = affected if affected else [c for c, f in flags.items() if f]
    fault_type = _infer_fault_type(
        pattern_type,
        incident_fault_type=(incident or {}).get("fault_type"),
        details_fault_type=details.get("fault_type"),
        detectors=detectors,
    )
    reasoning = _build_reasoning(
        incident=incident,
        details=details,
        hints=hints,
        pattern_type=pattern_type,
        affected_sensors=affected_list,
        detectors=detectors,
        row=row,
    )

    return {
        "flags": flags,
        "hints": hints,
        "attribution": attribution,
        "pattern_type": pattern_type or "",
        "affected_sensors": affected_list,
        "hint_summary": hint_summary,
        "reasoning": reasoning,
        "incident_id": (incident or {}).get("id"),
        "fault_type": fault_type,
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
    work["fault_type"] = [e["fault_type"] for e in enrichments]
    work["reasoning"] = [e["reasoning"] for e in enrichments]
    work["_enrichment"] = enrichments

    for canon in SENSOR_COLUMNS:
        if canon not in work.columns:
            work[canon] = [
                resolve_canonical_sensor_values(work.iloc[i])[canon]
                for i in range(len(work))
            ]
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


def parse_cold_run_detectors(detector: str | None) -> list[str]:
    """Split cold-run detector label into lstm / statistical tokens."""
    text = str(detector or "").lower()
    found: list[str] = []
    if "lstm" in text:
        found.append("lstm")
    if "statistical" in text:
        found.append("statistical")
    return found


def cold_run_agreement(row: dict[str, Any]) -> bool:
    """True when LSTM and statistical detectors agree on this timestamp."""
    if row.get("agreement"):
        return True
    detector = str(row.get("detector", "")).lower()
    return "lstm+statistical" in detector or (
        "lstm" in detector and "statistical" in detector
    )


def cold_run_row_to_series(row: dict[str, Any]) -> pd.Series:
    """Map upload/cold-run anomaly dict to a row enrich_row understands."""
    data: dict[str, Any] = {
        "timestamp": row.get("timestamp"),
        "anomaly_score": row.get("score", 0),
        "detector": row.get("detector"),
    }
    column = row.get("column")
    value = row.get("value")
    if column is not None:
        data["column"] = column
        data[column] = value
        slot = canonical_slot_for_column(str(column))
        if slot and value is not None:
            data[slot] = value
    detectors = parse_cold_run_detectors(row.get("detector"))
    if detectors:
        data["anomaly_detectors"] = detectors
    if column:
        data["anomaly_columns"] = [column]
    for key in ("z_score", "frozen_hours_at_detection", "method", "severity"):
        if row.get(key) is not None:
            data[key] = row[key]
    return pd.Series(data)


def enrich_cold_run_rows(
    rows: list[dict[str, Any]],
    incidents: Optional[List[Dict[str, Any]]] = None,
) -> pd.DataFrame:
    """Enrich cold-run upload anomalies with the same diagnosis fields as Top Anomalies."""
    if not rows:
        return pd.DataFrame()

    records: list[dict[str, Any]] = []
    for raw in rows:
        series = cold_run_row_to_series(raw)
        matched = (
            _nearest_incident(series["timestamp"], incidents)
            if incidents
            else None
        )
        enrichment = enrich_row(series, matched)
        sensor_vals = resolve_canonical_sensor_values(series)
        records.append(
            {
                "timestamp": series.get("timestamp"),
                "anomaly_score": float(raw.get("score", 0) or 0),
                "pattern_type": enrichment.get("pattern_type", ""),
                "fault_type": enrichment.get("fault_type"),
                "reasoning": enrichment.get("reasoning", ""),
                "affected_sensors": ", ".join(enrichment.get("affected_sensors", [])),
                "agreement": cold_run_agreement(raw),
                "detector": raw.get("detector"),
                "_enrichment": enrichment,
                "_cold_run": dict(raw),
                **{col: sensor_vals.get(col) for col in SENSOR_COLUMNS},
            }
        )
    return pd.DataFrame(records)
