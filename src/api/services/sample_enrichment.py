"""Incident binary-search lookup, row-dict building, and sample enrichment."""

from __future__ import annotations

import bisect
from typing import Any

import pandas as pd

from src.api.services.fault_classification import (
    _SENSOR_BOUNDS,
    _SENSOR_VALUE_KEYS,
    _UNKNOWN_KEY,
    _fault_type_from_anomaly_type,
    _infer_fault_type_from_values,
    _infer_sensors_from_values,
    _simple_reasoning_from_values,
)

# Mirror the +/- window used by diagnosis_enricher._nearest_incident.
_INCIDENT_WINDOW_MINUTES: int = 15


# ─── helpers ─────────────────────────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    """Convert datetime/date to ISO string, pass through strings unchanged."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _canonical_sensor_value(
    channel: str,
    temperature: float | None,
    humidity: float | None,
    co2: float | None,
    light: float | None,
) -> float | None:
    """Return the stored SensorReading value for a canonical channel name.

    Only the four stored columns are supported.  Non-canonical channel names
    (e.g. ``measured_pipe_1_temp``) return None — the caller must use a
    separately persisted ``trigger_value`` for those.
    """
    _MAP: dict[str, float | None] = {
        "temperature": temperature,
        "humidity": humidity,
        "co2": co2,
        "light": light,
    }
    return _MAP.get(channel)


def _sensor_values_from_dict(
    row: dict[str, Any], sensors: list[str]
) -> dict[str, float]:
    """Extract numeric sensor readings for the given sensor list.

    Mirrors anomaly_insights._sensor_values exactly.
    """
    keys: list[str] = sensors if sensors else list(_SENSOR_VALUE_KEYS)
    out: dict[str, float] = {}
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _build_sample(row: dict[str, Any]) -> dict[str, Any]:
    """Build the sample sub-object from an enriched row dict.

    Mirrors anomaly_insights._sample_from_row exactly.
    """
    sensors = [str(s) for s in (row.get("affected_sensors") or []) if s]
    score = row.get("anomaly_score")
    return {
        "timestamp": str(row.get("timestamp") or ""),
        "anomaly_score": float(score) if score is not None else None,
        "affected_sensors": sensors,
        "reasoning": row.get("reasoning"),
        "values": _sensor_values_from_dict(row, sensors),
        "detected_severity": row.get("detected_severity"),
    }


def _numeric_score(row: dict[str, Any]) -> float | None:
    value = row.get("anomaly_score")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _row_dict_from_minimal(
    timestamp: Any,
    anomaly_score: float | None,
    temperature: float | None,
    humidity: float | None,
    co2: float | None,
    light: float | None,
    anomaly_type: str | None = None,
    *,
    trigger_channel: str | None = None,
    trigger_detector: str | None = None,
    detected_severity: str | None = None,
    trigger_value: float | None = None,
) -> dict[str, Any]:
    """Build a row dict suitable for enrich_anomaly_rows from column values."""
    return {
        "timestamp": _iso(timestamp),
        "anomaly_score": anomaly_score,
        "temperature": temperature,
        "humidity": humidity,
        "co2": co2,
        "light": light,
        "anomaly_type": anomaly_type,
        "trigger_channel": trigger_channel,
        "trigger_detector": trigger_detector,
        "detected_severity": detected_severity,
        "trigger_value": trigger_value,
    }


# ─── incident binary-search lookup ──────────────────────────────────────────


def _build_sorted_incidents(
    incidents: list[dict[str, Any]],
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Sort incidents by detected_at for O(log N) binary-search lookup.

    Returns (sorted_times, sorted_incidents) where times[i] is the
    pd.Timestamp of incidents[i].detected_at.
    """
    pairs: list[tuple[Any, dict[str, Any]]] = [
        (pd.to_datetime(inc["detected_at"]), inc)
        for inc in incidents
        if inc.get("detected_at") is not None
    ]
    pairs.sort(key=lambda p: p[0])
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _nearest_incident_fast(
    timestamp: Any,
    sorted_times: list[Any],
    sorted_incidents: list[dict[str, Any]],
    window_minutes: int = _INCIDENT_WINDOW_MINUTES,
) -> dict[str, Any] | None:
    """Find the nearest incident within +/-window_minutes using binary search.

    O(log N + K) where K is incidents within the window (typically 0-5).
    Replaces the O(N) linear scan in diagnosis_enricher._nearest_incident.
    """
    if not sorted_times:
        return None
    ts = pd.to_datetime(timestamp)
    window = pd.Timedelta(minutes=window_minutes)
    lo = ts - window
    hi = ts + window

    start_pos = bisect.bisect_left(sorted_times, lo)
    best: dict[str, Any] | None = None
    best_delta = window + pd.Timedelta(seconds=1)  # sentinel

    i = start_pos
    while i < len(sorted_times) and sorted_times[i] <= hi:
        delta = abs(ts - sorted_times[i])
        if delta < best_delta:
            best = sorted_incidents[i]
            best_delta = delta
        i += 1

    return best


# ─── single-row enrichment ──────────────────────────────────────────────────


def _resolve_incident_enrichment(
    row_dict: dict[str, Any],
    incident: dict[str, Any],
) -> dict[str, Any]:
    """Resolve enrichment fields when an incident is matched."""
    temp = row_dict.get("temperature")
    hum = row_dict.get("humidity")
    co2 = row_dict.get("co2")
    light = row_dict.get("light")
    trigger_channel: str | None = row_dict.get("trigger_channel")
    trigger_value: float | None = row_dict.get("trigger_value")

    details = incident.get("root_cause_details") or {}
    sensors = list(details.get("affected_sensors") or [])
    if not sensors:
        # Prefer persisted trigger_channel over value-based inference.
        sensors = (
            [trigger_channel]
            if trigger_channel
            else _infer_sensors_from_values(temp, hum, co2, light)
        )

    # Persisted reasoning (from pipeline) takes priority — mirrors enrich_row.
    reasoning: str = details.get("reasoning") or ""
    if not reasoning:
        root_cause = incident.get("root_cause")
        reasoning = str(root_cause) if root_cause else ""
    if not reasoning:
        primary = sensors[0] if sensors else (trigger_channel or "unknown")
        primary_val = (
            trigger_value
            if trigger_channel == primary
            else _canonical_sensor_value(primary, temp, hum, co2, light)
        )
        reasoning = _simple_reasoning_from_values(primary, primary_val)

    return {
        "affected_sensors": sensors,
        "reasoning": reasoning,
        "fault_type": incident.get("fault_type") or _UNKNOWN_KEY,
        "incident_id": incident.get("id"),
        "root_cause": incident.get("root_cause"),
    }


def _resolve_fallback_enrichment(
    row_dict: dict[str, Any],
) -> dict[str, Any]:
    """Resolve enrichment fields when no incident is matched."""
    temp = row_dict.get("temperature")
    hum = row_dict.get("humidity")
    co2 = row_dict.get("co2")
    light = row_dict.get("light")
    trigger_channel: str | None = row_dict.get("trigger_channel")
    trigger_value: float | None = row_dict.get("trigger_value")

    # Prefer persisted trigger_channel over value-based inference (D1).
    if trigger_channel:
        sensors = [trigger_channel]
        reasoning = _simple_reasoning_from_values(trigger_channel, trigger_value)
    else:
        inferred = _infer_sensors_from_values(temp, hum, co2, light)
        sensors = inferred
        primary = inferred[0] if inferred else "unknown"
        primary_val = _canonical_sensor_value(primary, temp, hum, co2, light)
        reasoning = _simple_reasoning_from_values(primary, primary_val)

    # Prefer the stored anomaly_type label over value-based inference.
    # anomaly_type is written by the inject endpoint for synthetic rows.
    # "synthetic_context" sentinel maps to None -> fall through to value
    # inference (marks a normal background reading, not a fault).
    row_anomaly_type: str | None = row_dict.get("anomaly_type")
    mapped_fault = _fault_type_from_anomaly_type(row_anomaly_type)
    fault_type = (
        mapped_fault
        if mapped_fault is not None
        else _infer_fault_type_from_values(temp, hum, co2, light)
    )

    return {
        "affected_sensors": sensors,
        "reasoning": reasoning,
        "fault_type": fault_type,
        "incident_id": None,
        "root_cause": None,
    }


def _enrich_sample_fast(
    row_dict: dict[str, Any],
    incident: dict[str, Any] | None,
) -> dict[str, Any]:
    """Lightweight single-row enrichment that avoids pandas DataFrame overhead.

    Semantically equivalent to enrich_anomaly_rows for the fields consumed by
    _build_sample() — affected_sensors, reasoning, fault_type, incident_id,
    root_cause.  Both the fast path and enrich_anomaly_rows were exercised by
    the parity tests (tests/api/test_anomaly_groups_parity.py) and produce
    identical affected_sensors and sample_ids.

    Note: the reasoning string uses a simplified generation path that differs
    from enrich_row/_build_reasoning in phrasing but not in meaning.  Reasoning
    is informational only and is not compared in the parity test.
    """
    if incident is not None:
        fields = _resolve_incident_enrichment(row_dict, incident)
    else:
        fields = _resolve_fallback_enrichment(row_dict)
    return {**row_dict, **fields}


def _enrich_samples_fast(
    sample_row_dicts: list[dict[str, Any]],
    sorted_times: list[Any],
    sorted_incidents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enrich a batch of sample row dicts using the binary-search incident lookup.

    Avoids creating a pandas DataFrame from the sample rows, which adds
    ~5ms of overhead per row via enrich_dataframe / iterrows().  For
    365 x 5 = 1825 sample rows (full-year by=date grouping) this saves
    ~9 seconds vs. calling enrich_anomaly_rows.
    """
    enriched: list[dict[str, Any]] = []
    for row in sample_row_dicts:
        incident = _nearest_incident_fast(
            row.get("timestamp"), sorted_times, sorted_incidents
        )
        enriched.append(_enrich_sample_fast(row, incident))
    return enriched
