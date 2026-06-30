"""Unified anomaly events from persisted scores and incident records."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from src.dashboard.components.charts import apply_datetime_filter
from src.dashboard.services.diagnosis_enricher import (
    columns_refer_to_same_sensor,
    enrich_row,
    infer_affected_sensors_from_row,
)

_INCIDENT_EVENT_COLUMNS = [
    "id",
    "detected_at",
    "anomaly_score",
    "fault_type",
    "root_cause",
    "root_cause_details",
    "source",
    "reasoning",
    "affected_sensors",
]


def flagged_anomaly_mask(scores: pd.Series, threshold: float) -> pd.Series:
    """Return True for rows whose numeric score meets or exceeds threshold."""
    if scores is None or scores.empty:
        return pd.Series(False, index=scores.index)

    numeric = pd.to_numeric(scores, errors="coerce")
    if numeric.notna().sum() == 0:
        return pd.Series(False, index=numeric.index)

    return (numeric >= threshold).fillna(False)


def count_anomalies_at_threshold(scores: pd.Series, threshold: float) -> int:
    """Count rows flagged at the given threshold."""
    return int(flagged_anomaly_mask(scores, threshold).sum())


def score_summary(scores: pd.Series) -> dict[str, float | int]:
    """Basic distribution stats for anomaly scores in a window."""
    numeric = pd.to_numeric(scores, errors="coerce")
    valid = numeric.dropna()
    if valid.empty:
        return {"scored": 0, "min": float("nan"), "max": float("nan"), "median": float("nan")}
    return {
        "scored": int(valid.count()),
        "min": float(valid.min()),
        "max": float(valid.max()),
        "median": float(valid.median()),
    }


def _event_affects_column(event: pd.Series | dict[str, Any], column: str) -> bool:
    if isinstance(event, pd.Series):
        event = event.to_dict()

    affected = event.get("affected_sensors")
    if isinstance(affected, list):
        if not affected:
            return False
        return any(columns_refer_to_same_sensor(str(item), column) for item in affected)
    if isinstance(affected, str) and affected.strip():
        parts = [part.strip() for part in affected.split(",") if part.strip()]
        return any(columns_refer_to_same_sensor(part, column) for part in parts)

    details = event.get("root_cause_details")
    if isinstance(details, dict):
        sensors = details.get("affected_sensors") or []
        if sensors:
            return any(columns_refer_to_same_sensor(str(item), column) for item in sensors)
    return column in str(details or "")


def count_events_for_column(events_df: pd.DataFrame, column: str) -> int:
    if events_df.empty:
        return 0
    return int(events_df.apply(lambda row: _event_affects_column(row, column), axis=1).sum())


def _reading_row_to_event(row: pd.Series) -> dict[str, Any]:
    enrichment = enrich_row(row, None)
    affected = enrichment.get("affected_sensors") or infer_affected_sensors_from_row(row)
    detectors = row.get("anomaly_detectors")
    if not isinstance(detectors, list):
        detectors = []
    return {
        "id": None,
        "detected_at": row["timestamp"],
        "anomaly_score": float(row["anomaly_score"]),
        "fault_type": enrichment.get("fault_type"),
        "root_cause": enrichment.get("reasoning", "")[:256] or None,
        "root_cause_details": {
            "attribution": enrichment.get("attribution") or {},
            "affected_sensors": affected,
            "pattern_type": enrichment.get("pattern_type"),
            "source": "persisted_score",
            "detectors": detectors,
        },
        "source": "reading",
        "reasoning": enrichment.get("reasoning", ""),
        "affected_sensors": affected,
    }


def _incident_row_to_event(row: pd.Series) -> dict[str, Any]:
    details = row.get("root_cause_details") if isinstance(row.get("root_cause_details"), dict) else {}
    affected = details.get("affected_sensors") or []
    return {
        "id": row.get("id"),
        "detected_at": row.get("detected_at"),
        "anomaly_score": row.get("anomaly_score"),
        "fault_type": row.get("fault_type"),
        "root_cause": row.get("root_cause"),
        "root_cause_details": details,
        "source": "incident",
        "reasoning": row.get("root_cause") or details.get("pattern_explanation") or "",
        "affected_sensors": affected,
    }


def _dedupe_events(events: list[dict[str, Any]], window_minutes: int = 5) -> list[dict[str, Any]]:
    """Prefer incident records when a reading event is at the same time."""
    if not events:
        return []

    events = sorted(events, key=lambda e: pd.to_datetime(e["detected_at"]))
    kept: list[dict[str, Any]] = []
    for event in events:
        ts = pd.to_datetime(event["detected_at"])
        replaced = False
        for idx, existing in enumerate(kept):
            existing_ts = pd.to_datetime(existing["detected_at"])
            if abs((ts - existing_ts).total_seconds()) <= window_minutes * 60:
                if event.get("source") == "incident" and existing.get("source") != "incident":
                    kept[idx] = event
                replaced = True
                break
        if not replaced:
            kept.append(event)
    return kept


def build_unified_anomaly_events(
    readings_df: pd.DataFrame,
    incidents_df: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    threshold: float,
) -> pd.DataFrame:
    """Merge scored readings and incidents into one timeline for monitoring pages."""
    events: list[dict[str, Any]] = []

    if not readings_df.empty and "anomaly_score" in readings_df.columns:
        windowed = apply_datetime_filter(readings_df, start, end)
        mask = flagged_anomaly_mask(windowed["anomaly_score"], threshold)
        for _, row in windowed[mask].iterrows():
            events.append(_reading_row_to_event(row))

    if not incidents_df.empty:
        work = incidents_df.copy()
        work["detected_at"] = pd.to_datetime(work["detected_at"])
        windowed_incidents = work[
            (work["detected_at"] >= start) & (work["detected_at"] <= end)
        ]
        for _, row in windowed_incidents.iterrows():
            events.append(_incident_row_to_event(row))

    if not events:
        return pd.DataFrame(columns=_INCIDENT_EVENT_COLUMNS)

    deduped = _dedupe_events(events)
    result = pd.DataFrame(deduped)
    result["detected_at"] = pd.to_datetime(result["detected_at"])
    return result.sort_values("detected_at", ascending=False).reset_index(drop=True)


def data_anchored_window(
    readings_df: pd.DataFrame,
    hours_back: int,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Anchor 'last N hours' to the newest data point, not wall-clock now."""
    if readings_df.empty:
        end = pd.Timestamp.utcnow()
        return end - pd.Timedelta(hours=hours_back), end

    ts = pd.to_datetime(readings_df["timestamp"])
    data_end = ts.max()
    now = pd.Timestamp.now(tz=data_end.tz) if data_end.tzinfo is not None else pd.Timestamp.now()
    window_end = min(now, data_end)
    window_start = window_end - pd.Timedelta(hours=hours_back)
    data_min = ts.min()
    if window_start < data_min:
        window_start = data_min
    return window_start, window_end


event_affects_column = _event_affects_column
