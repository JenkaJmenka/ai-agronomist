"""SQL constructs and date-grouping queries for anomaly grouping.

Contains the dialect-aware ``utc_date`` construct, shared query helpers, and
the ``_groups_by_date`` implementation that uses SQL GROUP BY + window functions
to aggregate anomalies by day with minimal row fetching.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, types as sqltypes
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

from src.api.services.sample_enrichment import (
    _build_sample,
    _build_sorted_incidents,
    _enrich_samples_fast,
    _row_dict_from_minimal,
)
from src.db.models import SensorReading


# ─── dialect-aware date construct ────────────────────────────────────────────


class utc_date(FunctionElement):
    """DATE() of a timestamp column, always in UTC regardless of dialect.

    Problem this solves
    -------------------
    SensorReading.timestamp is DateTime(timezone=True) -> 'timestamptz' in
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


# ─── shared query helpers ────────────────────────────────────────────────────


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


# ─── date grouping (SQL) ────────────────────────────────────────────────────


def _build_date_sample_subquery(
    conditions: list[Any],
    sample_limit: int,
) -> Any:
    """Build the window-function subquery to pick top-N samples per day."""
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

    return (
        select(inner)
        .where(inner.c.rn <= sample_limit)
        .order_by(inner.c.day.desc(), inner.c.timestamp.desc())
    )


def _assemble_date_groups(
    agg_rows: list[Any],
    sample_rows: list[Any],
    incidents: list[dict[str, Any]],
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Enrich sample rows and assemble date-bucketed groups."""
    sorted_times, sorted_incidents = _build_sorted_incidents(incidents)
    sample_dicts: list[dict[str, Any]] = [
        _row_dict_from_minimal(
            r.timestamp, r.anomaly_score, r.temperature, r.humidity,
            r.co2, r.light, r.anomaly_type,
            trigger_channel=getattr(r, "trigger_channel", None),
            trigger_detector=getattr(r, "trigger_detector", None),
            detected_severity=getattr(r, "detected_severity", None),
            trigger_value=getattr(r, "trigger_value", None),
        )
        for r in sample_rows
    ]
    enriched_samples = _enrich_samples_fast(
        sample_dicts, sorted_times, sorted_incidents,
    )

    samples_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r, enriched in zip(sample_rows, enriched_samples):
        samples_by_day[str(r.day)].append(enriched)

    groups: list[dict[str, Any]] = []
    for row in agg_rows:
        day_key = str(row.day)
        day_samples = samples_by_day.get(day_key, [])
        top_samples = day_samples[:sample_limit]
        groups.append(
            {
                "key": day_key,
                "count": int(row.cnt),
                "top_score": float(row.top_score or 0.0),
                "sample_ids": [
                    str(s.get("timestamp") or "") for s in top_samples
                ],
                "samples": [_build_sample(s) for s in top_samples],
            }
        )
    return groups


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
    """SQL GROUP BY date — O(N_days + N_days x sample_limit) rows fetched.

    Two queries:
      1. Aggregate: GROUP BY date(timestamp) -> count + top_score per day.
      2. Samples:   ROW_NUMBER() OVER (...) to pick top-N per day by score.
    """
    conditions = _base_conditions(tenant_id, greenhouse_id, start, end, threshold)
    day_col = utc_date(SensorReading.timestamp).label("day")

    # Query 1: aggregate per day
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

    # Query 2: top-N sample rows per day (window function)
    sample_stmt = _build_date_sample_subquery(conditions, sample_limit)
    sample_rows = (await db.execute(sample_stmt)).all()

    return _assemble_date_groups(agg_rows, sample_rows, incidents, sample_limit)
