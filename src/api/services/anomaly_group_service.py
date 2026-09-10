"""Optimised anomaly grouping — type/sensor grouping and public entry point.

Replaces the 33-second full-range scan in /api/anomalies/groups:

  by=date:
    Delegated to anomaly_group_queries._groups_by_date — SQL GROUP BY with
    window-function sample selection.

  by=type / by=sensor:
    Implemented here.  Incidents fetched once, binary-search nearest-incident
    lookup: O(log N_incidents) per row.  Column-projection query avoids full
    ORM object construction overhead.

Performance target: full-range by=type under 1.0 s (was 33 s).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.anomaly_group_queries import (  # noqa: F401
    _fetch_minimal_flagged_rows,
    _groups_by_date,
    utc_date as utc_date,
)
from src.api.services.fault_classification import (  # noqa: F401
    _SIMPLE_REASONING_UNITS as _SIMPLE_REASONING_UNITS,
    _UNKNOWN_KEY,
    _affected_sensors_from_incident,
    _fault_type_from_anomaly_type as _fault_type_from_anomaly_type,
    _fault_type_from_incident,
    _infer_fault_type_from_values,
    _infer_sensors_from_values,
    _simple_reasoning_from_values as _simple_reasoning_from_values,
)
from src.api.services.sample_enrichment import (  # noqa: F401
    _build_sample as _build_sample,
    _build_sorted_incidents as _build_sorted_incidents,
    _enrich_sample_fast as _enrich_sample_fast,
    _enrich_samples_fast as _enrich_samples_fast,
    _iso,
    _nearest_incident_fast,
    _row_dict_from_minimal as _row_dict_from_minimal,
)

# Type alias for bucket items in type/sensor grouping.
BucketItem = tuple[
    str, float, float | None, float | None, float | None, float | None, str | None
]


# ─── type / sensor grouping helpers ─────────────────────────────────────────


def _bucket_rows_by_group(
    minimal_rows: list[Any],
    sorted_times: list[Any],
    sorted_incidents: list[dict[str, Any]],
    group_by: Literal["type", "sensor"],
) -> tuple[dict[str, list[BucketItem]], dict[str, dict[str, Any]]]:
    """Classify each row and bucket it by fault-type or affected sensor."""
    buckets: dict[str, list[BucketItem]] = defaultdict(list)
    trigger_by_ts: dict[str, dict[str, Any]] = {}

    for r in minimal_rows:
        ts_iso = _iso(r.timestamp) or ""
        score = float(r.anomaly_score) if r.anomaly_score is not None else 0.0
        temp = r.temperature
        hum = r.humidity
        co2 = r.co2
        light = r.light
        item: BucketItem = (ts_iso, score, temp, hum, co2, light, r.anomaly_type)

        # Capture trigger columns (NULL for rows pre-dating migration 024).
        trigger_by_ts[ts_iso] = {
            "trigger_channel": getattr(r, "trigger_channel", None),
            "trigger_detector": getattr(r, "trigger_detector", None),
            "detected_severity": getattr(r, "detected_severity", None),
            "trigger_value": getattr(r, "trigger_value", None),
        }

        incident = _nearest_incident_fast(r.timestamp, sorted_times, sorted_incidents)
        incident_fault_type = _fault_type_from_incident(incident)
        affected_sensors = _affected_sensors_from_incident(incident)

        if incident is None:
            # No incident match — prefer stored anomaly_type label over
            # value-based inference.  "synthetic_context" maps to None and
            # falls through to value inference (normal background row).
            mapped_fault = _fault_type_from_anomaly_type(r.anomaly_type)
            fault_type = (
                mapped_fault
                if mapped_fault is not None
                else _infer_fault_type_from_values(temp, hum, co2, light)
            )
            # Prefer persisted trigger_channel over value-based inference.
            tc = trigger_by_ts[ts_iso].get("trigger_channel")
            affected_sensors = (
                [tc] if tc
                else _infer_sensors_from_values(temp, hum, co2, light)
            )
        else:
            fault_type = incident_fault_type or _UNKNOWN_KEY
            if not affected_sensors:
                affected_sensors = _infer_sensors_from_values(temp, hum, co2, light)

        if group_by == "type":
            buckets[fault_type].append(item)
        else:  # sensor
            for sensor in (affected_sensors or [_UNKNOWN_KEY]):
                buckets[str(sensor)].append(item)

    return buckets, trigger_by_ts


def _collect_and_enrich_group_samples(
    buckets: dict[str, list[BucketItem]],
    sample_limit: int,
    trigger_by_ts: dict[str, dict[str, Any]],
    sorted_times: list[Any],
    sorted_incidents: list[dict[str, Any]],
) -> tuple[dict[str, list[BucketItem]], dict[str, dict[str, Any]]]:
    """Collect top-N samples per group, deduplicate, and enrich them."""
    sample_ts_set: set[str] = set()
    group_top_items: dict[str, list[BucketItem]] = {}

    for key, items in buckets.items():
        ranked = sorted(items, key=lambda t: t[1], reverse=True)
        top_items = sorted(ranked[:sample_limit], key=lambda t: t[0], reverse=True)
        group_top_items[key] = top_items
        for item in top_items:
            ts = item[0]
            if ts not in sample_ts_set:
                sample_ts_set.add(ts)

    # Build raw dicts for the sample rows — include trigger columns so the
    # enrichment fast path can use them instead of value-based inference.
    sample_raw: dict[str, dict[str, Any]] = {}
    for top_items in group_top_items.values():
        for item in top_items:
            ts, score, temp, hum, co2, light, anom_type = item
            if ts not in sample_raw:
                sample_raw[ts] = _row_dict_from_minimal(
                    ts, score, temp, hum, co2, light, anom_type,
                    **trigger_by_ts.get(ts, {}),
                )

    # Enrich only the sample rows (small N <= N_groups x sample_limit).
    unique_sample_dicts = list(sample_raw.values())
    enriched_list = (
        _enrich_samples_fast(unique_sample_dicts, sorted_times, sorted_incidents)
        if unique_sample_dicts
        else []
    )
    enriched_by_ts: dict[str, dict[str, Any]] = {
        str(e.get("timestamp") or ""): e for e in enriched_list
    }
    return group_top_items, enriched_by_ts


def _assemble_type_sensor_groups(
    buckets: dict[str, list[BucketItem]],
    group_top_items: dict[str, list[BucketItem]],
    enriched_by_ts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build and sort the final group dicts for type/sensor grouping."""
    groups: list[dict[str, Any]] = []
    for key, items in buckets.items():
        count = len(items)
        top_score = max((item[1] for item in items), default=0.0)
        top_items = group_top_items[key]

        group_samples: list[dict[str, Any]] = []
        sample_ids: list[str] = []
        for item in top_items:
            ts = item[0]
            sample_ids.append(ts)
            enriched = enriched_by_ts.get(
                ts, {"timestamp": ts, "anomaly_score": item[1]},
            )
            group_samples.append(_build_sample(enriched))

        groups.append(
            {
                "key": key,
                "count": count,
                "top_score": top_score,
                "sample_ids": sample_ids,
                "samples": group_samples,
            }
        )

    # Highest-count first, then alphabetical key (mirrors group_anomaly_rows).
    return sorted(groups, key=lambda g: (-g["count"], g["key"]))


# ─── type / sensor grouping (main) ──────────────────────────────────────────


async def _groups_by_type_or_sensor(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    threshold: float,
    group_by: Literal["type", "sensor"],
    sample_limit: int,
    incidents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Binary-search incident join + minimal-column fetch for type/sensor grouping.

    Steps:
      1. Build binary-searchable incident index.
      2. Fetch minimal columns only (no full ORM objects).
      3. Binary-search nearest incident per row -> fault_type + affected_sensors.
      4. Group into buckets (fast dict operations, no DataFrame).
      5. Enrich only the top-N sample rows.
    """
    sorted_times, sorted_incidents = _build_sorted_incidents(incidents)

    minimal_rows = await _fetch_minimal_flagged_rows(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=start,
        end=end,
        threshold=threshold,
    )
    if not minimal_rows:
        return []

    buckets, trigger_by_ts = _bucket_rows_by_group(
        minimal_rows, sorted_times, sorted_incidents, group_by,
    )
    group_top_items, enriched_by_ts = _collect_and_enrich_group_samples(
        buckets, sample_limit, trigger_by_ts, sorted_times, sorted_incidents,
    )
    return _assemble_type_sensor_groups(buckets, group_top_items, enriched_by_ts)


# ─── public entry point ─────────────────────────────────────────────────────


async def fetch_anomaly_groups(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    threshold: float,
    group_by: Literal["date", "type", "sensor"],
    sample_limit: int,
    incidents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Dispatch to the optimised grouping implementation for the given mode.

    Args:
        incidents: Pre-fetched incident list for the same window.  Caller is
            responsible for fetching incidents so the route can reuse them for
            both the group and the anomaly_count queries without an extra round
            trip.

    Returns:
        List of group dicts with keys: key, count, top_score, sample_ids, samples.
    """
    if group_by == "date":
        return await _groups_by_date(
            db,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            start=start,
            end=end,
            threshold=threshold,
            sample_limit=sample_limit,
            incidents=incidents,
        )
    return await _groups_by_type_or_sensor(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=start,
        end=end,
        threshold=threshold,
        group_by=group_by,  # type: ignore[arg-type]
        sample_limit=sample_limit,
        incidents=incidents,
    )
