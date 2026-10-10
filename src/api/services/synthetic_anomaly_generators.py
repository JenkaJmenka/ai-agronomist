"""Value generators and type profiles backing synthetic anomaly injection.

Split out of ``synthetic_anomalies.py`` for the 300-line guardrail: this
module owns the per-type injection profiles, the pure value-generation
helpers, and the per-row ``_build_injected_row`` builder -- leaving the two
service entry points and the one DB-querying helper (``_resolve_sample_stats``)
in the original module. Nothing here performs I/O (no DB session, no
FastAPI) -- it only builds values and ORM objects in memory. See that
module's docstring for the provenance contract these values feed into.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.config import (
    GAS_SENSOR_COLUMNS,
    SYNTHETIC_ANOMALY_SCORE_RANGE,
    SYNTHETIC_CANONICAL_STATS,
    synthetic_source,
)
from src.db.models import RawReading, SensorReading

# Gas-panel columns (dedicated instrument, e.g. gas_co2) are a DIFFERENT
# INSTRUMENT from the climate channels of the same short name (e.g. co2) --
# see src/vendors/csv_adapter.py:346. Bound once as a frozenset so both
# _pick_col() and the raw-spike column selection in synthetic_anomalies.py
# can exclude gas columns without re-deriving the set on every call.
_GAS_SENSOR_COLUMNS: frozenset[str] = frozenset(GAS_SENSOR_COLUMNS)

# Per-type injection profile, as a tuple of:
#   - canonical sensor fields to make anomalous (others stay normal)
#   - direction of deviation for targeted sensors
#   - intra-band score spread: superseded by _TYPE_SCORE_SPREAD below and
#     otherwise unread, but NOT dead -- test_synthetic_anomalies_gas_isolation.py
#     reads raw_keywords via a positional profile[3] index, which pins this
#     tuple's 4-element shape
#   - raw column keyword substrings to spike (empty list = all raw columns)
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

# ADR-005 §2.2: the synthetic generator emits canonical scores DIRECTLY from
# the config bands (no transform). All types land inside
# SYNTHETIC_ANOMALY_SCORE_RANGE = (0.75, 0.99). _SCORE_LO/_SCORE_HI/_SCORE_SPAN
# combine with _TYPE_SCORE_SPREAD below in _type_config_for() to compute each
# type's (score_lo, score_hi).
_SCORE_LO, _SCORE_HI = SYNTHETIC_ANOMALY_SCORE_RANGE  # (0.75, 0.99)
_SCORE_SPAN = _SCORE_HI - _SCORE_LO                    # 0.24

# Previous bug: gradual_drift used (0.60, 0.90) on a scale whose threshold was
# 0.8, meaning it emitted "anomalies" that the scale called normal ~50% of the
# time. The new explicit canonical bands fix this permanently.
_TYPE_SCORE_SPREAD: dict[str, tuple[float, float]] = {
    #                             lo_offset  width (both as fraction of the band span)
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


@dataclass
class _AnomalyTypeConfig:
    """Resolved per-type shape for one injection run (ADR-005 §2.2)."""

    canonical_targets: set[str]
    direction: str
    raw_keywords: list[str]
    score_lo: float
    score_hi: float


def _type_config_for(anomaly_type: str) -> _AnomalyTypeConfig:
    """Combine ``_TYPE_PROFILES``/``_TYPE_SCORE_SPREAD`` into one lookup -- ADR-005
    §2.2's score band plus that type's intra-band (lo_offset, width) fractions."""
    canonical_targets, direction, _unused_legacy_range, raw_keywords = _TYPE_PROFILES[anomaly_type]
    spread_lo_frac, spread_width_frac = _TYPE_SCORE_SPREAD[anomaly_type]
    score_lo = round(_SCORE_LO + spread_lo_frac * _SCORE_SPAN, 4)
    score_hi = round(min(_SCORE_HI, _SCORE_LO + (spread_lo_frac + spread_width_frac) * _SCORE_SPAN), 4)
    return _AnomalyTypeConfig(canonical_targets, direction, raw_keywords, score_lo, score_hi)


def _resolve_raw_spike_cols(col_stats: dict, raw_keywords: list[str]) -> set[str]:
    """Raw columns a targeted type is allowed to spike.

    Targeted types (non-empty raw_keywords, e.g. co2_surge -> ["co2"]) must not
    bleed across instruments: "co2" substring-matches "gas_co2", fabricating
    gas-panel spikes for a purely climate anomaly. Untargeted types (random,
    multi_sensor -- raw_keywords == []) deliberately spike every raw column,
    gas included, per the _TYPE_PROFILES comment."""
    if not raw_keywords:
        return set(col_stats.keys())
    return {
        col
        for col in col_stats
        if any(kw in col.lower() for kw in raw_keywords) and col.lower() not in _GAS_SENSOR_COLUMNS
    }


def _drift_rank_for(anomaly_type: str, anomaly_positions: set[int]) -> dict[int, float]:
    """Rank sorted anomaly positions so gradual_drift intensity increases over time."""
    if anomaly_type != "gradual_drift" or not anomaly_positions:
        return {}
    sorted_positions = sorted(anomaly_positions)
    return {pos: (i + 1) / len(sorted_positions) for i, pos in enumerate(sorted_positions)}


@dataclass
class _InjectionRunContext:
    """Everything constant across the per-row loop in one injection run."""

    tenant_id: str
    greenhouse_id: str
    anomaly_type: str
    type_config: _AnomalyTypeConfig
    col_stats: dict
    raw_spike_cols: set[str]
    has_raw_cols: bool
    ctx_score_range: tuple[float, float]
    start_dt: datetime
    interval_seconds: int
    anomaly_positions: set[int]
    drift_rank: dict[int, float]


def _canonical_fallback_value(
    context: _InjectionRunContext, sensor: str, is_anomaly: bool, drift_factor: float
) -> float:
    """``_pick_col`` fallback when no matching raw column exists for ``sensor``."""
    cmin, cmax, cmean = SYNTHETIC_CANONICAL_STATS[sensor]
    if is_anomaly and sensor in context.type_config.canonical_targets:
        return _anomaly_value_directed(cmin, cmax, cmean, context.type_config.direction, drift_factor)
    return _normal_value(sensor, cmin, cmax, cmean)


def _col_values_for_row(
    context: _InjectionRunContext, is_anomaly: bool, drift_factor: float
) -> dict[str, float]:
    """One row's raw-column values: spiked where targeted and anomalous, else normal."""
    direction = context.type_config.direction
    col_values: dict[str, float] = {}
    for col, (cmin, cmax, cmean) in context.col_stats.items():
        if is_anomaly and col in context.raw_spike_cols:
            col_values[col] = _anomaly_value_directed(cmin, cmax, cmean, direction, drift_factor)
        else:
            col_values[col] = _normal_value(col, cmin, cmax, cmean)
    return col_values


def _canonical_sensor_values(
    context: _InjectionRunContext, col_values: dict[str, float], is_anomaly: bool, drift_factor: float
) -> tuple[float, float, float, float]:
    """Resolve (temperature, humidity, co2, light) via ``_pick_col``, falling back
    to the canonical stats when no matching raw column exists."""
    return tuple(
        _pick_col(col_values, keywords, _canonical_fallback_value(context, sensor, is_anomaly, drift_factor))
        for sensor, keywords in (
            ("temperature", ["greenhouse_temp", "temperature", "temp"]),
            ("humidity", ["relative_humidity", "humidity"]),
            ("co2", ["co2"]),
            ("light", ["light"]),
        )
    )


def _score_for_row(context: _InjectionRunContext, is_anomaly: bool) -> float:
    """ADR-005 §2.2: ground-truth producers emit canonical values directly from
    the config bands, no transform. Filler deliberately sits below live's
    canonical noise floor (~0.17) so fabricated normals never outrank real
    measured readings."""
    if is_anomaly:
        return round(random.uniform(context.type_config.score_lo, context.type_config.score_hi), 4)
    ctx_lo, ctx_hi = context.ctx_score_range
    return round(random.uniform(ctx_lo, ctx_hi), 4)


def _build_injected_row(
    context: _InjectionRunContext, i: int
) -> tuple[SensorReading, Optional[RawReading]]:
    """Build one (SensorReading, RawReading | None) pair for loop index ``i``."""
    ts = context.start_dt + timedelta(seconds=i * context.interval_seconds)
    is_anomaly = i in context.anomaly_positions
    drift_factor = context.drift_rank.get(i, 1.0)

    col_values = _col_values_for_row(context, is_anomaly, drift_factor)
    temp, hum, co2, light = _canonical_sensor_values(context, col_values, is_anomaly, drift_factor)
    score = _score_for_row(context, is_anomaly)

    # source carries the same "synthetic:<type>" / "synthetic:context" marker
    # as the RawReading below -- this is what delete_synthetic_anomalies keys
    # off, not anomaly_type (see module docstring and ADR-010).
    row_source = synthetic_source(context.anomaly_type if is_anomaly else None)
    sensor_row = SensorReading(
        tenant_id=context.tenant_id, greenhouse_id=context.greenhouse_id, timestamp=ts,
        temperature=round(float(temp), 2), humidity=round(float(hum), 2),
        co2=round(float(co2), 1), light=round(float(light), 1),
        anomaly_score=score,
        anomaly_score_raw=None,   # ground-truth: no raw detector score
        score_scale="synthetic",
        anomaly_type=context.anomaly_type if is_anomaly else "synthetic_context",
        source=row_source,
    )

    raw_row = None
    if context.has_raw_cols and col_values:
        raw_row = RawReading(
            tenant_id=context.tenant_id, greenhouse_id=context.greenhouse_id, timestamp=ts,
            source=row_source,
            data={k: v for k, v in col_values.items() if v is not None},
        )
    return sensor_row, raw_row
