"""Optimised anomaly grouping — SQL aggregation + targeted enrichment.

Replaces the 33-second full-range scan in /api/anomalies/groups:

  Old path (slow):
    1. fetch_flagged_window  → up to 50 000 full ORM rows
    2. enrich_anomaly_rows  → pandas DataFrame + iterrows() over all 50 000
                               × O(N_incidents) _nearest_incident per row
    3. group_anomaly_rows   → Python bucketing over all rows

  New path (fast):
    by=date:
      SQL GROUP BY date(timestamp) → count + top_score per day  (1 query)
      SQL ROW_NUMBER() OVER (…)    → top-N timestamps per day   (1 query)
      enrich_anomaly_rows called only on top-N sample rows
      → total rows fetched: N_days + N_days × sample_limit  (≤ 2 190 for a year)

    by=type / by=sensor:
      Incidents fetched once (small N)
      Binary-search nearest-incident lookup: O(log N_incidents) per row
        vs. the old O(N_incidents) linear scan
      Column-projection query: only (timestamp, anomaly_score, temp, hum,
        co2, light) — avoids full ORM object construction overhead
      enrich_anomaly_rows called only on top-N sample rows
      → Python-visible rows: N_flagged × 6 cols (not full ORM objects)

Performance target: full-range by=type under 1.0 s (was 33 s).
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

import pandas as pd
from sqlalchemy import func, select, types as sqltypes
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

from src.config import (
    CO2_RANGE,
    HUMIDITY_RANGE,
    LIGHT_RANGE,
    TEMP_RANGE,
)
from src.db.models import SensorReading

# Mirror the ±window used by diagnosis_enricher._nearest_incident.
_INCIDENT_WINDOW_MINUTES: int = 15
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


# ─── helpers ─────────────────────────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    """Convert datetime/date to ISO string, pass through strings unchanged."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


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
) -> dict[str, Any]:
    """Build a row dict suitable for enrich_anomaly_rows from column values."""
    return {
        "timestamp": _iso(timestamp),
        "anomaly_score": anomaly_score,
        "temperature": temperature,
        "humidity": humidity,
        "co2": co2,
        "light": light,
    }


# ─── incident binary-search lookup ───────────────────────────────────────────


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
    """Find the nearest incident within ±window_minutes using binary search.

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


def _fault_type_from_incident(incident: dict[str, Any] | None) -> str | None:
    """Extract fault_type from a matched incident, or None when no incident."""
    if incident is None:
        return None
    return incident.get("fault_type") or None


def _infer_fault_type_from_values(
    temperature: float | None,
    humidity: float | None,
    co2: float | None,
    light: float | None,
) -> str:
    """Infer fault_type from sensor values when no incident is matched.

    Mirrors the pattern_type + _infer_fault_type fallback path inside
    enrich_row / enrich_dataframe that fires when incident is None.

    Logic:
      - If any sensor reads above its upper bound → "spike" → spike_fault.
      - If any sensor reads below its lower bound → "drop" → spike_fault.
      - If z-score flags are present but no range violation → "multivariate"
        → accuracy_decline (mirrors in-range deviation branch).
      - Otherwise → "unknown".
    """
    vals: dict[str, float | None] = {
        "temperature": temperature,
        "humidity": humidity,
        "co2": co2,
        "light": light,
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
        # Mirrors: pattern_type = "spike" → spike_fault.
        return "spike_fault"
    if below:
        # Mirrors: pattern_type = "drop" → spike_fault.
        return "spike_fault"
    # In-range z-score deviation — mirrors multivariate branch.
    has_flags = any(z > 1.2 for z in zscores.values())
    if has_flags:
        return "accuracy_decline"
    # Fallback for multivariate/sequence anomalies with no clear deviation.
    # enrich_row returns None here (no pattern_type, no incident).
    return _UNKNOWN_KEY


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
    # Range violations are strong signals → return those first.
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


# ─── fast single-row enrichment (avoids pandas) ───────────────────────────────

_SIMPLE_REASONING_UNITS: dict[str, str] = {
    "temperature": "°C",
    "humidity": "%",
    "co2": " ppm",
    "light": " lux",
}


def _simple_reasoning_from_values(
    temperature: float | None,
    humidity: float | None,
    co2: float | None,
    light: float | None,
    sensors: list[str],
) -> str:
    """Generate a compact reasoning string from sensor readings.

    Used by _enrich_sample_fast when no persisted reasoning is available.
    Not intended to be identical to enrich_row._build_reasoning — it is a
    lighter-weight fallback for sample objects in group responses.
    """
    vals: dict[str, float | None] = {
        "temperature": temperature,
        "humidity": humidity,
        "co2": co2,
        "light": light,
    }
    parts: list[str] = []
    for sensor in sensors:
        v = vals.get(sensor)
        if v is None:
            continue
        bounds = _SENSOR_BOUNDS.get(sensor)
        unit = _SIMPLE_REASONING_UNITS.get(sensor, "")
        if bounds is not None:
            lo, hi = bounds
            if v > hi:
                parts.append(
                    f"Unexpected {sensor} spike: {v:.1f}{unit} ({hi:.0f}{unit} upper bound)"
                )
            elif v < lo:
                parts.append(
                    f"Unexpected {sensor} drop: {v:.1f}{unit} ({lo:.0f}{unit} lower bound)"
                )
            else:
                parts.append(f"Unusual {sensor} reading: {v:.1f}{unit}")
    if not parts:
        return "Anomaly score exceeded threshold — no detailed diagnosis available yet"
    return "; ".join(parts)


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
    temp = row_dict.get("temperature")
    hum = row_dict.get("humidity")
    co2 = row_dict.get("co2")
    light = row_dict.get("light")

    if incident is not None:
        details = incident.get("root_cause_details") or {}
        sensors = list(details.get("affected_sensors") or [])
        if not sensors:
            sensors = _infer_sensors_from_values(temp, hum, co2, light)

        # Persisted reasoning (from pipeline) takes priority — mirrors enrich_row.
        reasoning: str = details.get("reasoning") or ""
        if not reasoning:
            root_cause = incident.get("root_cause")
            reasoning = str(root_cause) if root_cause else ""
        if not reasoning:
            reasoning = _simple_reasoning_from_values(temp, hum, co2, light, sensors)

        fault_type: str = incident.get("fault_type") or _UNKNOWN_KEY
        incident_id: str | None = incident.get("id")
        root_cause_val: str | None = incident.get("root_cause")
    else:
        sensors = _infer_sensors_from_values(temp, hum, co2, light)
        fault_type = _infer_fault_type_from_values(temp, hum, co2, light)
        reasoning = _simple_reasoning_from_values(temp, hum, co2, light, sensors)
        incident_id = None
        root_cause_val = None

    return {
        **row_dict,
        "affected_sensors": sensors,
        "reasoning": reasoning,
        "fault_type": fault_type,
        "incident_id": incident_id,
        "root_cause": root_cause_val,
    }


def _enrich_samples_fast(
    sample_row_dicts: list[dict[str, Any]],
    sorted_times: list[Any],
    sorted_incidents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enrich a batch of sample row dicts using the binary-search incident lookup.

    Avoids creating a pandas DataFrame from the sample rows, which adds
    ~5ms of overhead per row via enrich_dataframe / iterrows().  For
    365 × 5 = 1825 sample rows (full-year by=date grouping) this saves
    ~9 seconds vs. calling enrich_anomaly_rows.
    """
    enriched: list[dict[str, Any]] = []
    for row in sample_row_dicts:
        incident = _nearest_incident_fast(
            row.get("timestamp"), sorted_times, sorted_incidents
        )
        enriched.append(_enrich_sample_fast(row, incident))
    return enriched


# ─── SQL helpers ──────────────────────────────────────────────────────────────


class utc_date(FunctionElement):
    """DATE() of a timestamp column, always in UTC regardless of dialect.

    Problem this solves
    -------------------
    SensorReading.timestamp is DateTime(timezone=True) → 'timestamptz' in
    PostgreSQL.  date(timestamptz) uses the *session* TimeZone GUC, so if that
    GUC is not UTC, day boundaries shift and rows land in the wrong group.
    SQLite stores ISO strings which are always UTC, so date() there is correct
    as-is.  Because the raw SQL text is identical on both dialects, no SQLite
    test can catch the Postgres regression — hence this dialect-aware construct.

    Compiled output
    ---------------
    PostgreSQL : date((col) AT TIME ZONE 'UTC')
    All others : date(col)          — SQLite ISO strings are always UTC

    Usage
    -----
    Use utc_date(SensorReading.timestamp) everywhere a day expression is needed
    — GROUP BY, ORDER BY, PARTITION BY, SELECT — so all four sites agree.
    Disagreement between GROUP BY and PARTITION BY would silently mis-assign
    sample rows, which is worse than the original bug.
    """

    type = sqltypes.NullType()
    inherit_cache = True


@compiles(utc_date, "postgresql")
def _pg_utc_date(element: utc_date, compiler: Any, **kw: Any) -> str:
    """Postgres: wrap column in AT TIME ZONE 'UTC' before extracting the date."""
    (col,) = list(element.clauses)
    col_sql = compiler.process(col, **kw)
    return f"date(({col_sql}) AT TIME ZONE 'UTC')"


@compiles(utc_date)
def _default_utc_date(element: utc_date, compiler: Any, **kw: Any) -> str:
    """All other dialects (SQLite): plain date() — ISO strings are always UTC."""
    (col,) = list(element.clauses)
    col_sql = compiler.process(col, **kw)
    return f"date({col_sql})"


def _base_conditions(
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    threshold: float,
) -> list[Any]:
    return [
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.timestamp >= start,
        SensorReading.timestamp <= end,
        SensorReading.anomaly_score >= threshold,
    ]


async def _fetch_minimal_flagged_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    threshold: float,
) -> list[Any]:
    """Fetch (timestamp, anomaly_score, temperature, humidity, co2, light) tuples.

    Uses a column-projection query to avoid ORM object construction overhead.
    Returns lightweight Row objects (named tuples), not SensorReading instances.
    """
    stmt = (
        select(
            SensorReading.timestamp,
            SensorReading.anomaly_score,
            SensorReading.temperature,
            SensorReading.humidity,
            SensorReading.co2,
            SensorReading.light,
        )
        .where(*_base_conditions(tenant_id, greenhouse_id, start, end, threshold))
        .order_by(SensorReading.timestamp)
    )
    result = await db.execute(stmt)
    return result.all()


# ─── date grouping (SQL) ──────────────────────────────────────────────────────


async def _groups_by_date(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    threshold: float,
    sample_limit: int,
    incidents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """SQL GROUP BY date — O(N_days + N_days × sample_limit) rows fetched.

    Two queries:
      1. Aggregate: GROUP BY date(timestamp) → count + top_score per day.
      2. Samples:   ROW_NUMBER() OVER (...) to pick top-N per day by score.

    Enrichment uses _enrich_samples_fast which avoids pandas DataFrame creation
    + iterrows() overhead (~5ms/row via enrich_anomaly_rows).  For a full-year
    window with 365 days × 5 samples = 1825 sample rows, the pandas path
    would add ~9 s; the fast path adds ~0.02 s.
    """
    conditions = _base_conditions(tenant_id, greenhouse_id, start, end, threshold)
    day_col = utc_date(SensorReading.timestamp).label("day")

    # ── Query 1: aggregate per day ──────────────────────────────────────────
    agg_stmt = (
        select(
            day_col,
            func.count().label("cnt"),
            func.max(SensorReading.anomaly_score).label("top_score"),
        )
        .where(*conditions)
        .group_by(utc_date(SensorReading.timestamp))
        .order_by(utc_date(SensorReading.timestamp).desc())
    )
    agg_rows = (await db.execute(agg_stmt)).all()
    if not agg_rows:
        return []

    # ── Query 2: top-N sample rows per day (window function) ────────────────
    # ROW_NUMBER() OVER (PARTITION BY date ORDER BY score DESC) to pick
    # the sample_limit highest-scoring rows per day.  Secondary sort by
    # timestamp ASC matches the Python stable-sort in group_anomaly_rows.
    rn_expr = (
        func.row_number()
        .over(
            partition_by=utc_date(SensorReading.timestamp),
            order_by=[
                SensorReading.anomaly_score.desc(),
                SensorReading.timestamp.asc(),
            ],
        )
        .label("rn")
    )

    inner = (
        select(
            SensorReading.timestamp,
            SensorReading.anomaly_score,
            SensorReading.temperature,
            SensorReading.humidity,
            SensorReading.co2,
            SensorReading.light,
            utc_date(SensorReading.timestamp).label("day"),
            rn_expr,
        )
        .where(*conditions)
    ).subquery("ranked_anomalies")

    sample_stmt = (
        select(inner)
        .where(inner.c.rn <= sample_limit)
        .order_by(inner.c.day.desc(), inner.c.timestamp.desc())
    )
    sample_rows = (await db.execute(sample_stmt)).all()

    # ── Enrich sample rows (fast path — no pandas) ───────────────────────────
    sorted_times, sorted_incidents = _build_sorted_incidents(incidents)
    sample_dicts: list[dict[str, Any]] = [
        _row_dict_from_minimal(
            r.timestamp, r.anomaly_score, r.temperature, r.humidity, r.co2, r.light
        )
        for r in sample_rows
    ]
    enriched_samples = _enrich_samples_fast(sample_dicts, sorted_times, sorted_incidents)

    # ── Bucket enriched samples by day ──────────────────────────────────────
    # day is a string like '2026-06-01'; samples are already newest-first.
    samples_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r, enriched in zip(sample_rows, enriched_samples):
        day_key = str(r.day)
        samples_by_day[day_key].append(enriched)

    # ── Assemble groups ──────────────────────────────────────────────────────
    groups: list[dict[str, Any]] = []
    for row in agg_rows:
        day_key = str(row.day)
        day_samples = samples_by_day.get(day_key, [])
        # Samples are already ordered by timestamp DESC from the SQL ORDER BY.
        top_samples = day_samples[:sample_limit]
        groups.append(
            {
                "key": day_key,
                "count": int(row.cnt),
                "top_score": float(row.top_score or 0.0),
                "sample_ids": [str(s.get("timestamp") or "") for s in top_samples],
                "samples": [_build_sample(s) for s in top_samples],
            }
        )

    # Newest day first (already ordered by SQL).
    return groups


# ─── type / sensor grouping (binary-search) ───────────────────────────────────


def _sort_samples_newest_first(
    bucket_items: list[tuple[str, float, float, float, float, float]],
    sample_limit: int,
) -> list[tuple[str, float, float, float, float, float]]:
    """Pick the top-N items by score, then sort them newest-first by timestamp.

    Mirrors group_anomaly_rows: ranked[:sample_limit] then sort by timestamp DESC.
    Items: (timestamp_iso, anomaly_score, temperature, humidity, co2, light)
    """
    # Sort by score descending (stable sort — earliest timestamp wins on ties).
    ranked = sorted(bucket_items, key=lambda t: t[1], reverse=True)
    top = ranked[:sample_limit]
    # Show newest-first by timestamp string (ISO sorts lexicographically).
    return sorted(top, key=lambda t: t[0], reverse=True)


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

    Avoids loading all 45 000 full ORM rows and the O(N×M) incident-scan loop
    that made full-year queries take 33 seconds.

    Steps:
      1. Build binary-searchable incident index.
      2. Fetch (timestamp, anomaly_score, temperature, humidity, co2, light) only.
      3. Binary-search nearest incident per row → fault_type + affected_sensors.
      4. Group into buckets in Python (fast dict operations, no DataFrame).
      5. Enrich only the top-N sample rows via enrich_anomaly_rows.
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

    # Items stored per bucket: (timestamp_iso, score, temp, hum, co2, light)
    BucketItem = tuple[str, float, float | None, float | None, float | None, float | None]
    buckets: dict[str, list[BucketItem]] = defaultdict(list)

    for r in minimal_rows:
        ts_iso = _iso(r.timestamp) or ""
        score = float(r.anomaly_score) if r.anomaly_score is not None else 0.0
        temp = r.temperature
        hum = r.humidity
        co2 = r.co2
        light = r.light
        item: BucketItem = (ts_iso, score, temp, hum, co2, light)

        incident = _nearest_incident_fast(r.timestamp, sorted_times, sorted_incidents)
        incident_fault_type = _fault_type_from_incident(incident)
        affected_sensors = _affected_sensors_from_incident(incident)

        if incident is None:
            # No incident match — infer both fault_type and sensors from values.
            # Mirrors the enrich_row fallback path (_zscore_flags + _infer_fault_type).
            fault_type = _infer_fault_type_from_values(temp, hum, co2, light)
            affected_sensors = _infer_sensors_from_values(temp, hum, co2, light)
        else:
            fault_type = incident_fault_type or _UNKNOWN_KEY
            if not affected_sensors:
                affected_sensors = _infer_sensors_from_values(temp, hum, co2, light)

        if group_by == "type":
            buckets[fault_type].append(item)
        else:  # sensor
            # A row counts once per affected sensor (mirrors _group_keys "sensor" logic).
            for sensor in (affected_sensors or [_UNKNOWN_KEY]):
                buckets[str(sensor)].append(item)

    # ── Collect all sample rows for batch enrichment ────────────────────────
    # Gather the top-N rows per group, deduplicating by timestamp.
    sample_ts_set: set[str] = set()
    sample_ts_list: list[str] = []
    group_top_items: dict[str, list[BucketItem]] = {}

    for key, items in buckets.items():
        top_items = _sort_samples_newest_first(items, sample_limit)
        group_top_items[key] = top_items
        for item in top_items:
            ts = item[0]
            if ts not in sample_ts_set:
                sample_ts_set.add(ts)
                sample_ts_list.append(ts)

    # Build raw dicts for the sample rows.
    sample_raw: dict[str, dict[str, Any]] = {}
    for key, top_items in group_top_items.items():
        for item in top_items:
            ts, score, temp, hum, co2, light = item
            if ts not in sample_raw:
                sample_raw[ts] = _row_dict_from_minimal(ts, score, temp, hum, co2, light)

    # Enrich only the sample rows (small N ≤ N_groups × sample_limit).
    # Use the fast path (binary-search incident lookup, no pandas) since
    # sorted_times / sorted_incidents are already built above.
    unique_sample_dicts = list(sample_raw.values())
    enriched_list = (
        _enrich_samples_fast(unique_sample_dicts, sorted_times, sorted_incidents)
        if unique_sample_dicts
        else []
    )
    enriched_by_ts: dict[str, dict[str, Any]] = {
        str(e.get("timestamp") or ""): e for e in enriched_list
    }

    # ── Assemble groups ──────────────────────────────────────────────────────
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
            enriched = enriched_by_ts.get(ts, {"timestamp": ts, "anomaly_score": item[1]})
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


# ─── public entry point ────────────────────────────────────────────────────────


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
