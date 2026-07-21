"""Anomaly filtering, enrichment and grouping (migration plan §5 G2/G3/G4).

Pure functions over the JSON-row shape produced by ``readings_service``
(timestamps already ISO strings, NaN already None). Enrichment reuses the
same incident-join + fault-classification logic the Streamlit page uses
(``diagnosis_enricher``), so both frontends agree on fault types.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

import pandas as pd

from src.dashboard.services.diagnosis_enricher import enrich_dataframe

_SCORE_KEY = "anomaly_score"
_TIMESTAMP_KEY = "timestamp"

GROUP_BY_CHOICES = ("date", "type", "sensor")
_UNKNOWN_KEY = "unknown"


def _numeric_score(row: dict[str, Any]) -> Optional[float]:
    value = row.get(_SCORE_KEY)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def filter_anomaly_rows(
    rows: list[dict[str, Any]], threshold: float
) -> list[dict[str, Any]]:
    """Rows whose numeric anomaly_score meets or exceeds the threshold.

    Mirrors the dashboard's ``flagged_anomaly_mask`` semantics: non-numeric
    and missing scores never flag.
    """
    kept = []
    for row in rows:
        score = _numeric_score(row)
        if score is not None and score >= threshold:
            kept.append(row)
    return kept


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def enrich_anomaly_rows(
    rows: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach fault_type / root_cause / reasoning via the incident join.

    Server-side port of the dashboard's ``load_enriched_top_anomalies``:
    each row gains ``fault_type``, ``pattern_type``, ``reasoning``,
    ``hint_summary``, ``affected_sensors`` (list), ``incident_id`` and
    ``root_cause`` (from the matched incident, when any).
    """
    if not rows:
        return []
    enriched = enrich_dataframe(pd.DataFrame(rows), incidents)
    incidents_by_id = {
        incident["id"]: incident for incident in incidents or [] if incident.get("id")
    }
    out: list[dict[str, Any]] = []
    for record in enriched.to_dict(orient="records"):
        enrichment = record.pop("_enrichment", None) or {}
        incident = incidents_by_id.get(enrichment.get("incident_id"))
        shaped = {key: _json_safe(value) for key, value in record.items()}
        # enrich_dataframe flattens affected_sensors to a comma string;
        # the API contract ships the structured list instead.
        shaped["affected_sensors"] = list(enrichment.get("affected_sensors") or [])
        shaped["incident_id"] = enrichment.get("incident_id")
        shaped["root_cause"] = (incident or {}).get("root_cause")
        # When the pipeline persisted a reasoning on the matched incident,
        # it is authoritative: it names the exact algorithm that fired
        # (including the newer Mahalanobis / change-point detectors the
        # z-score-based regeneration in enrich_dataframe cannot know about).
        details = (incident or {}).get("root_cause_details") or {}
        persisted_reasoning = details.get("reasoning")
        if persisted_reasoning:
            shaped["reasoning"] = persisted_reasoning
        out.append(shaped)
    return out


def top_anomalies(
    rows: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Highest-scoring rows first, capped at ``limit``."""
    ranked = sorted(rows, key=lambda row: _numeric_score(row) or 0.0, reverse=True)
    return ranked[:limit]


def _group_keys(row: dict[str, Any], group_by: str) -> list[str]:
    if group_by == "date":
        timestamp = str(row.get(_TIMESTAMP_KEY) or "")
        return [timestamp[:10] or _UNKNOWN_KEY]
    if group_by == "type":
        return [str(row.get("fault_type") or _UNKNOWN_KEY)]
    sensors = row.get("affected_sensors")
    if isinstance(sensors, list) and sensors:
        return [str(sensor) for sensor in sensors]
    return [_UNKNOWN_KEY]


_SENSOR_VALUE_KEYS = (
    "temperature",
    "humidity",
    "co2",
    "light",
    "vpd",
    "soil_moisture",
    "root_zone_temp",
)


def _sensor_values(row: dict[str, Any], sensors: list[str]) -> dict[str, float]:
    """Numeric readings for the sensors that caused (or best explain) the anomaly."""
    keys = sensors if sensors else list(_SENSOR_VALUE_KEYS)
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


def _sample_from_row(row: dict[str, Any]) -> dict[str, Any]:
    sensors = [str(s) for s in (row.get("affected_sensors") or []) if s]
    return {
        "timestamp": str(row.get(_TIMESTAMP_KEY) or ""),
        "anomaly_score": _numeric_score(row),
        "affected_sensors": sensors,
        "reasoning": row.get("reasoning"),
        "values": _sensor_values(row, sensors),
    }


def group_anomaly_rows(
    rows: list[dict[str, Any]],
    group_by: str,
    *,
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Aggregate anomaly rows into ``{key, count, top_score, sample_ids, samples}``.

    ``group_by='sensor'`` counts a row once per affected sensor. Rows carry
    no stable primary key, so ``sample_ids`` / ``samples`` are the group's
    highest-scoring rows — the FE uses them to jump to Monitoring and to
    show sensors / values / reasoning inside each group.
    Groups are ordered by count (desc), then key.
    """
    if group_by not in GROUP_BY_CHOICES:
        raise ValueError(f"group_by must be one of {GROUP_BY_CHOICES}, got {group_by!r}")

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for key in _group_keys(row, group_by):
            buckets[key].append(row)

    groups = []
    for key, bucket in buckets.items():
        ranked = sorted(bucket, key=lambda row: _numeric_score(row) or 0.0, reverse=True)
        top = ranked[:sample_limit]
        groups.append(
            {
                "key": key,
                "count": len(bucket),
                "top_score": _numeric_score(ranked[0]) or 0.0,
                "sample_ids": [str(row.get(_TIMESTAMP_KEY)) for row in top],
                "samples": [_sample_from_row(row) for row in top],
            }
        )
    return sorted(groups, key=lambda group: (-group["count"], group["key"]))
