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
from src.models.band_breach_detector import BAND_TYPE_TO_VOTE
from src.models.setpoint_scada_detector import (
    DEHUMIDIFICATION_FAILURE,
    HUMIDITY_SATURATION_RISK,
    OVER_HEATING,
    SENSOR_FLATLINE,
    THERMAL_DIVERGENCE,
    UNDER_HEATING,
)

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
    # In-range case: z = |f - mid| / (span/2) ≤ 1.0 for all f ∈ [lo, hi],
    # so any(z > 1.2 ...) can never be True — the old "accuracy_decline" branch
    # was unreachable dead code.  diagnosis_enricher._zscore_flags has the same
    # bound; see its inline comment.  Without an incident or a range violation,
    # the honest answer is "unknown" — Task 1 (anomaly_type label) covers the
    # synthetic-injection case before this fallback is ever reached.
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

    # Trigger columns from migration 024 (None for pre-migration rows).
    trigger_channel: str | None = row_dict.get("trigger_channel")
    trigger_value: float | None = row_dict.get("trigger_value")

    if incident is not None:
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
            primary_val = trigger_value if trigger_channel == primary else _canonical_sensor_value(
                primary, temp, hum, co2, light
            )
            reasoning = _simple_reasoning_from_values(primary, primary_val)

        fault_type: str = incident.get("fault_type") or _UNKNOWN_KEY
        incident_id: str | None = incident.get("id")
        root_cause_val: str | None = incident.get("root_cause")
    else:
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
        # "synthetic_context" sentinel maps to None → fall through to value
        # inference (marks a normal background reading, not a fault).
        row_anomaly_type: str | None = row_dict.get("anomaly_type")
        mapped_fault = _fault_type_from_anomaly_type(row_anomaly_type)
        fault_type = (
            mapped_fault
            if mapped_fault is not None
            else _infer_fault_type_from_values(temp, hum, co2, light)
        )
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
            SensorReading.anomaly_type,
            SensorReading.trigger_channel,
            SensorReading.trigger_detector,
            SensorReading.detected_severity,
            SensorReading.trigger_value,
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
            SensorReading.anomaly_type,
            SensorReading.trigger_channel,
            SensorReading.trigger_detector,
            SensorReading.detected_severity,
            SensorReading.trigger_value,
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
            r.timestamp, r.anomaly_score, r.temperature, r.humidity, r.co2, r.light,
            r.anomaly_type,
            trigger_channel=getattr(r, "trigger_channel", None),
            trigger_detector=getattr(r, "trigger_detector", None),
            detected_severity=getattr(r, "detected_severity", None),
            trigger_value=getattr(r, "trigger_value", None),
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
    bucket_items: list[tuple[str, float, float | None, float | None, float | None, float | None, str | None]],
    sample_limit: int,
) -> list[tuple[str, float, float | None, float | None, float | None, float | None, str | None]]:
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

    # Items stored per bucket:
    # (timestamp_iso, score, temp, hum, co2, light, anomaly_type)
    BucketItem = tuple[str, float, float | None, float | None, float | None, float | None, str | None]
    buckets: dict[str, list[BucketItem]] = defaultdict(list)

    # Per-timestamp trigger data read from the new migration-024 columns.
    # Stored separately from BucketItem so the compact tuple shape is unchanged.
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
            # No incident match — prefer the stored anomaly_type label over
            # value-based inference.  The sentinel "synthetic_context" maps to
            # None and falls through to value inference (normal background row).
            mapped_fault = _fault_type_from_anomaly_type(r.anomaly_type)
            fault_type = (
                mapped_fault
                if mapped_fault is not None
                else _infer_fault_type_from_values(temp, hum, co2, light)
            )
            # Prefer persisted trigger_channel over value-based inference.
            tc = trigger_by_ts[ts_iso].get("trigger_channel")
            affected_sensors = [tc] if tc else _infer_sensors_from_values(temp, hum, co2, light)
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

    # Build raw dicts for the sample rows — include trigger columns so the
    # enrichment fast path can use them instead of value-based inference.
    sample_raw: dict[str, dict[str, Any]] = {}
    for key, top_items in group_top_items.items():
        for item in top_items:
            ts, score, temp, hum, co2, light, anom_type = item
            if ts not in sample_raw:
                sample_raw[ts] = _row_dict_from_minimal(
                    ts, score, temp, hum, co2, light, anom_type,
                    **trigger_by_ts.get(ts, {}),
                )

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
