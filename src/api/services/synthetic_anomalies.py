"""Synthetic anomaly generation and cleanup behind the inject-test routes.

Injection learns column names and value ranges from existing RawReadings so
injected spikes match the user's real sensors, then writes SensorReading rows
(anomaly_score + anomaly_type set) and RawReading rows
(source = "synthetic:<type>"). Removal only touches rows carrying those
synthetic markers — real sensor data is never deleted.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.cache_invalidation import invalidate_response_cache
from src.config import (
    GAS_SENSOR_COLUMNS,
    SYNTHETIC_ANOMALY_SCORE_RANGE,
    SYNTHETIC_CONTEXT_SCORE_RANGE,
)
from src.db.models import RawReading, SensorReading

# Gas-panel columns (dedicated instrument, e.g. gas_co2) are a DIFFERENT
# INSTRUMENT from the climate channels of the same short name (e.g. co2) --
# see src/vendors/csv_adapter.py:346. Bound once as a frozenset so both
# _pick_col() and the raw-spike column selection below can exclude gas
# columns without re-deriving the set on every call.
_GAS_SENSOR_COLUMNS: frozenset[str] = frozenset(GAS_SENSOR_COLUMNS)

# Per-type injection profile, as a tuple of:
#   - canonical sensor fields to make anomalous (others stay normal)
#   - direction of deviation for targeted sensors
#   - intra-band score spread expressed as fraction of SYNTHETIC_ANOMALY_SCORE_RANGE
#     (not an absolute range — see ADR-005 §2.2)
#   - raw column keyword substrings to spike (empty list = all raw columns)
#
# ADR-005 §2.2: the synthetic generator emits canonical scores DIRECTLY from
# the config bands (no transform).  The score range below now represents
# intra-band variation within SYNTHETIC_ANOMALY_SCORE_RANGE = (0.75, 0.99).
#
# Previous bug: gradual_drift used (0.60, 0.90) on a scale whose threshold was
# 0.8, meaning it emitted "anomalies" that the scale called normal ~50% of the
# time.  The new explicit canonical bands fix this permanently.
_SCORE_LO, _SCORE_HI = SYNTHETIC_ANOMALY_SCORE_RANGE  # (0.75, 0.99)
_SCORE_SPAN = _SCORE_HI - _SCORE_LO                    # 0.24

# Intra-band spread: (offset_from_lo, width) as fractions of _SCORE_SPAN.
# All types land inside [_SCORE_LO, _SCORE_HI] = [0.75, 0.99].
_TYPE_SCORE_SPREAD: dict[str, tuple[float, float]] = {
    #                             lo_offset  width (both as fraction of _SCORE_SPAN)
    "random":            (0.29, 1.00),  # 0.82 – 0.99
    "temperature_spike": (0.42, 0.58),  # 0.85 – 0.99
    "temperature_drop":  (0.29, 0.71),  # 0.82 – 0.99 (capped at hi)
    "humidity_spike":    (0.21, 0.79),  # 0.80 – 0.99
    "humidity_drop":     (0.13, 0.71),  # 0.78 – 0.95
    "co2_surge":         (0.54, 0.46),  # 0.88 – 0.99
    "light_failure":     (0.42, 0.54),  # 0.85 – 0.98
    "multi_sensor":      (0.63, 0.38),  # 0.90 – 0.99
    "gradual_drift":     (0.00, 1.00),  # 0.75 – 0.99  ← was 0.60–0.90 (bug fixed)
}

_TYPE_PROFILES: dict[str, tuple[set[str], str, tuple[float, float], list[str]]] = {
    "random": ({"temperature", "humidity", "co2", "light"}, "mixed", (0.82, 0.99), []),
    "temperature_spike": ({"temperature"}, "high", (0.85, 0.99), ["temp"]),
    "temperature_drop": ({"temperature"}, "low", (0.82, 0.96), ["temp"]),
    "humidity_spike": ({"humidity"}, "high", (0.80, 0.95), ["humid"]),
    "humidity_drop": ({"humidity"}, "low", (0.78, 0.94), ["humid"]),
    "co2_surge": ({"co2"}, "high", (0.88, 0.99), ["co2"]),
    "light_failure": ({"light"}, "low", (0.85, 0.98), ["light", "lux", "par"]),
    "multi_sensor": ({"temperature", "humidity", "co2", "light"}, "mixed", (0.90, 0.99), []),
    "gradual_drift": ({"temperature", "humidity"}, "drift", (0.75, 0.99), ["temp", "humid"]),
}

ANOMALY_TYPES: list[str] = list(_TYPE_PROFILES)

# Canonical sensor fallback statistics (min, max, mean) when no raw data exists
_CANONICAL_STATS: dict[str, tuple[float, float, float]] = {
    "temperature": (19.0, 28.0, 23.0),
    "humidity": (55.0, 72.0, 63.0),
    "co2": (380.0, 520.0, 420.0),
    "light": (0.0, 950.0, 200.0),
}


def _daily_cycle(hour: float, lo: float, hi: float, peak_hour: float = 14.0) -> float:
    phase = (hour - peak_hour) / 24.0 * 2 * math.pi
    mid = (lo + hi) / 2.0
    amp = (hi - lo) / 2.0
    return mid - amp * math.cos(phase)


def _col_ranges_from_sample(rows: list) -> dict:
    """Compute per-column (min, max, mean) from a sample of RawReading rows."""
    per_col: dict = {}
    for r in rows:
        for k, v in (r.data or {}).items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if k not in per_col:
                per_col[k] = []
            per_col[k].append(fv)
    return {k: (min(vs), max(vs), sum(vs) / len(vs)) for k, vs in per_col.items() if vs}


def _anomaly_value(col: str, col_min: float, col_max: float, col_mean: float) -> float:
    """Generate a plausible anomaly value: spike or drop outside the normal range."""
    span = max(col_max - col_min, 1.0)
    if random.random() < 0.5:
        return round(col_max + span * random.uniform(0.5, 1.5), 2)
    return round(col_min - span * random.uniform(0.3, 1.0), 2)


def _anomaly_value_directed(
    col_min: float, col_max: float, col_mean: float,
    direction: str, drift_intensity: float = 1.0,
) -> float:
    """Generate a directional anomaly value for type-specific injection."""
    span = max(col_max - col_min, 1.0)
    if direction == "high":
        return round(col_max + span * random.uniform(0.5, 1.5), 2)
    if direction == "low":
        return round(max(0.0, col_min - span * random.uniform(0.3, 1.0)), 2)
    if direction == "drift":
        deviation = span * drift_intensity * random.uniform(0.6, 1.2)
        return round(
            col_max + deviation if random.random() < 0.5 else max(0.0, col_min - deviation), 2
        )
    # "mixed": random direction
    if random.random() < 0.5:
        return round(col_max + span * random.uniform(0.5, 1.5), 2)
    return round(max(0.0, col_min - span * random.uniform(0.3, 1.0)), 2)


def _pick_col(col_values: dict, keywords: list, fallback: float) -> float:
    """Return the col_values entry matching keywords, preferring exact matches.

    Gas-panel columns are excluded outright: every canonical field this feeds
    (temperature/humidity/co2/light) is a climate reading, and a gas-panel
    instrument (e.g. gas_co2) must never be mistaken for its climate namesake
    (e.g. co2). Exact keyword-name matches are checked -- in keyword-priority
    order -- before substring matches are tried, also in keyword-priority
    order, so CSV column order can never decide the outcome (e.g. a file
    that happens to list gas_co2 before co2 must still resolve climate co2
    correctly, and greenhouse_temp must still beat a co-present temperature
    column per caller-supplied priority).
    """
    candidates = {
        col: v for col, v in col_values.items() if col.lower() not in _GAS_SENSOR_COLUMNS
    }
    for kw in keywords:
        for col, v in candidates.items():
            if col.lower() == kw:
                return v
    for kw in keywords:
        for col, v in candidates.items():
            if kw in col.lower():
                return v
    return fallback


def _normal_value(col: str, col_min: float, col_max: float, col_mean: float) -> float:
    """Generate a normal value inside the observed range with small noise."""
    noise = (col_max - col_min) * 0.05
    return round(max(col_min, min(col_max, col_mean + random.gauss(0, noise))), 2)


def _resolve_window(
    window_start: Optional[str], window_end: Optional[str]
) -> tuple[datetime, datetime]:
    """Parse the requested injection window; default to the last 48 hours."""
    if not (window_start and window_end):
        end_dt = datetime.now(timezone.utc)
        return end_dt - timedelta(hours=48), end_dt
    try:
        start_dt = datetime.fromisoformat(window_start)
        end_dt = datetime.fromisoformat(window_end)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid window datetime: {exc}")
    if end_dt <= start_dt:
        raise HTTPException(status_code=422, detail="window_end must be after window_start")
    return start_dt, end_dt


async def inject_synthetic_anomalies(
    db: AsyncSession,
    *,
    tenant_id: str, greenhouse_id: str, n_normal: int, n_anomalies: int,
    anomaly_type: str, window_start: Optional[str] = None, window_end: Optional[str] = None,
) -> dict:
    """Insert synthetic readings and return the injection summary payload."""
    if anomaly_type not in ANOMALY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown anomaly_type '{anomaly_type}'. Valid: {ANOMALY_TYPES}",
        )

    start_dt, end_dt = _resolve_window(window_start, window_end)
    window_hours = max(1, int((end_dt - start_dt).total_seconds() / 3600))
    total = n_normal + n_anomalies
    interval_seconds = max(60, int((end_dt - start_dt).total_seconds() / max(total, 1)))

    # Learn column ranges from up to 500 recent real rows
    sample_rows = (
        await db.execute(
            select(RawReading)
            .where(RawReading.tenant_id == tenant_id, RawReading.greenhouse_id == greenhouse_id)
            .order_by(RawReading.timestamp.desc())
            .limit(500)
        )
    ).scalars().all()

    col_stats = _col_ranges_from_sample(sample_rows)
    has_raw_cols = bool(col_stats)
    if not col_stats:
        col_stats = dict(_CANONICAL_STATS)

    # Type-specific configuration.
    # ADR-005 §2.2: anomaly score bands are expressed within the canonical
    # SYNTHETIC_ANOMALY_SCORE_RANGE = (0.75, 0.99).  _TYPE_SCORE_SPREAD holds
    # (lo_offset, width) as fractions of that band's span (0.24).
    canonical_targets, direction, _legacy_range, raw_keywords = _TYPE_PROFILES[anomaly_type]
    _spread_lo_frac, _spread_width_frac = _TYPE_SCORE_SPREAD[anomaly_type]
    score_lo = round(_SCORE_LO + _spread_lo_frac * _SCORE_SPAN, 4)
    score_hi = round(min(_SCORE_HI, _SCORE_LO + (_spread_lo_frac + _spread_width_frac) * _SCORE_SPAN), 4)
    _ctx_lo, _ctx_hi = SYNTHETIC_CONTEXT_SCORE_RANGE  # (0.05, 0.20)
    # Targeted types (non-empty raw_keywords, e.g. co2_surge -> ["co2"]) must
    # not bleed across instruments: "co2" substring-matches "gas_co2", which
    # would fabricate gas-panel spikes for a purely climate anomaly. Untargeted
    # types (random, multi_sensor -- raw_keywords == []) deliberately spike
    # every raw column, gas included, per the profile comment above.
    raw_spike_cols: set[str] = (
        {
            col
            for col in col_stats
            if any(kw in col.lower() for kw in raw_keywords)
            and col.lower() not in _GAS_SENSOR_COLUMNS
        }
        if raw_keywords
        else set(col_stats.keys())
    )

    anomaly_positions = set(random.sample(range(total), min(n_anomalies, total)))

    # Gradual drift: rank sorted anomaly positions so intensity increases over time
    if anomaly_type == "gradual_drift" and anomaly_positions:
        _sorted = sorted(anomaly_positions)
        drift_rank: dict[int, float] = {pos: (i + 1) / len(_sorted) for i, pos in enumerate(_sorted)}
    else:
        drift_rank = {}

    def _canonical_val(sensor: str, is_anom: bool, d_factor: float) -> float:
        cmin, cmax, cmean = _CANONICAL_STATS[sensor]
        if is_anom and sensor in canonical_targets:
            return _anomaly_value_directed(cmin, cmax, cmean, direction, d_factor)
        return _normal_value(sensor, cmin, cmax, cmean)

    sensor_records: list[SensorReading] = []
    raw_records: list[RawReading] = []

    for i in range(total):
        ts = start_dt + timedelta(seconds=i * interval_seconds)
        is_anomaly = i in anomaly_positions
        drift_factor = drift_rank.get(i, 1.0)

        col_values: dict[str, float] = {}
        for col, (cmin, cmax, cmean) in col_stats.items():
            if is_anomaly and col in raw_spike_cols:
                col_values[col] = _anomaly_value_directed(cmin, cmax, cmean, direction, drift_factor)
            else:
                col_values[col] = _normal_value(col, cmin, cmax, cmean)

        temp = _pick_col(col_values, ["greenhouse_temp", "temperature", "temp"], _canonical_val("temperature", is_anomaly, drift_factor))
        hum = _pick_col(col_values, ["relative_humidity", "humidity"], _canonical_val("humidity", is_anomaly, drift_factor))
        co2 = _pick_col(col_values, ["co2"], _canonical_val("co2", is_anomaly, drift_factor))
        light = _pick_col(col_values, ["light"], _canonical_val("light", is_anomaly, drift_factor))
        # ADR-005 §2.2: ground-truth producers emit canonical values directly
        # from the config bands.  No transform is applied.
        # Filler deliberately sits below live's canonical noise floor (~0.17)
        # so fabricated normals never outrank real measured readings.
        score = round(
            random.uniform(score_lo, score_hi) if is_anomaly else random.uniform(_ctx_lo, _ctx_hi),
            4,
        )

        sensor_records.append(SensorReading(
            tenant_id=tenant_id, greenhouse_id=greenhouse_id, timestamp=ts,
            temperature=round(float(temp), 2), humidity=round(float(hum), 2),
            co2=round(float(co2), 1), light=round(float(light), 1),
            anomaly_score=score,
            anomaly_score_raw=None,   # ground-truth: no raw detector score
            score_scale="synthetic",
            anomaly_type=anomaly_type if is_anomaly else "synthetic_context",
        ))

        if has_raw_cols and col_values:
            raw_records.append(RawReading(
                tenant_id=tenant_id, greenhouse_id=greenhouse_id, timestamp=ts,
                source=f"synthetic:{anomaly_type}" if is_anomaly else "synthetic:context",
                data={k: v for k, v in col_values.items() if v is not None},
            ))

    db.add_all(sensor_records)
    if raw_records:
        db.add_all(raw_records)
    await db.commit()
    await invalidate_response_cache("inject_test_anomalies")

    return {
        "injected": len(sensor_records),
        "raw_injected": len(raw_records),
        "anomalies": n_anomalies,
        "normal": n_normal,
        "anomaly_type": anomaly_type,
        "columns_used": list(col_stats.keys()),
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "window_start": start_dt.isoformat(),
        "window_end": end_dt.isoformat(),
        "window_hours": window_hours,
    }


async def delete_synthetic_anomalies(
    db: AsyncSession, *, tenant_id: str, greenhouse_id: str, anomaly_type: Optional[str]
) -> dict:
    """Remove synthetically injected test anomalies. Real sensor data is never touched."""
    # Treat empty string the same as omit (UI "All synthetic" option).
    resolved_type = anomaly_type.strip() if anomaly_type else None
    if resolved_type is not None and resolved_type not in ANOMALY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown anomaly_type '{resolved_type}'. Valid: {ANOMALY_TYPES}",
        )

    # SensorReading rows are tagged via non-null anomaly_type field
    # (spike types + synthetic_context background rows). Live demo rows carry
    # source='live' with anomaly_type=NULL (migration 017) and are cleaned by
    # DELETE /upload/live-data instead; is_not(None) already excludes them.
    sensor_where = [
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.anomaly_type.is_not(None),
    ]
    if resolved_type:
        sensor_where[-1] = SensorReading.anomaly_type == resolved_type

    # RawReading rows are tagged via source = "synthetic:<type>" or "synthetic:context"
    raw_where = [
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
        RawReading.source.like("synthetic:%"),
    ]
    if resolved_type:
        raw_where[-1] = RawReading.source == f"synthetic:{resolved_type}"

    sensor_result = await db.execute(delete(SensorReading).where(*sensor_where))
    raw_result = await db.execute(delete(RawReading).where(*raw_where))
    await db.commit()
    await invalidate_response_cache("remove_synthetic_anomalies")

    return {
        "deleted_sensor": sensor_result.rowcount,
        "deleted_raw": raw_result.rowcount,
        "anomaly_type": resolved_type or "all",
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
    }
