"""Server-side read queries for the dashboard (item C3: reads go through the API).

Ports the query + merge logic that previously lived in the dashboard's
charts.py with direct DB access. Every list query is LIMIT-capped by
READINGS_MAX_ROWS so a huge tenant history cannot be pulled in one request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pandas as pd
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import defer
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import READINGS_MAX_ROWS
from src.db.models import Incident, RawReading, SensorReading


def _cap(limit: Optional[int]) -> int:
    if limit is None or limit <= 0:
        return READINGS_MAX_ROWS
    return min(limit, READINGS_MAX_ROWS)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def merge_anomaly_scores_into_readings(
    readings_df: pd.DataFrame,
    scores_df: pd.DataFrame,
    tenant_id: str,
    greenhouse_id: str,
) -> pd.DataFrame:
    """Attach canonical anomaly scores; dedupe duplicate upload rows."""
    if readings_df.empty or scores_df.empty or "anomaly_score" not in scores_df.columns:
        return readings_df
    merged = readings_df.copy()
    scores = scores_df.copy()
    if "tenant_id" in scores.columns:
        scores = scores[scores["tenant_id"] == tenant_id]
    if "greenhouse_id" in scores.columns:
        scores = scores[scores["greenhouse_id"] == greenhouse_id]
    if scores.empty:
        if "anomaly_score" not in merged.columns:
            merged["anomaly_score"] = pd.NA
        return merged

    merged["timestamp"] = pd.to_datetime(merged["timestamp"])
    scores["timestamp"] = pd.to_datetime(scores["timestamp"])
    merged["_ts_key"] = merged["timestamp"].dt.floor("s")
    scores["_ts_key"] = scores["timestamp"].dt.floor("s")
    scores = scores.groupby("_ts_key", as_index=False)["anomaly_score"].max()
    merged = merged.drop_duplicates(subset=["_ts_key"], keep="first")
    merged = merged.merge(
        scores[["_ts_key", "anomaly_score"]],
        on="_ts_key",
        how="left",
        suffixes=("", "_sensor"),
    )
    if "anomaly_score_sensor" in merged.columns:
        merged["anomaly_score"] = merged["anomaly_score_sensor"].combine_first(
            merged.get("anomaly_score")
        )
        merged = merged.drop(columns=["anomaly_score_sensor"])
    return merged.drop(columns=["_ts_key"])


def _raw_records_to_rows(records) -> list[dict[str, Any]]:
    rows = []
    for r in records:
        row = {"timestamp": r.timestamp, "source": r.source}
        row.update(r.data or {})
        rows.append(row)
    return rows


def _canonical_records_to_rows(records) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": r.timestamp,
            "temperature": r.temperature,
            "humidity": r.humidity,
            "co2": r.co2,
            "light": r.light,
            "anomaly_score": r.anomaly_score,
        }
        for r in records
    ]


def _df_to_json_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    out["timestamp"] = out["timestamp"].map(_iso)
    # NaN → None so the JSON body carries proper nulls.
    return [
        {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.items()}
        for row in out.to_dict(orient="records")
    ]


async def fetch_merged_readings(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Raw rows (full CSV fidelity) merged with canonical anomaly scores."""
    cap = _cap(limit)

    raw_stmt = select(RawReading).where(
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
    )
    canon_stmt = select(SensorReading).where(
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
    )
    if start is not None:
        raw_stmt = raw_stmt.where(RawReading.timestamp >= start)
        canon_stmt = canon_stmt.where(SensorReading.timestamp >= start)
    if end is not None:
        raw_stmt = raw_stmt.where(RawReading.timestamp <= end)
        canon_stmt = canon_stmt.where(SensorReading.timestamp <= end)
    raw_stmt = raw_stmt.order_by(RawReading.timestamp).limit(cap)
    canon_stmt = canon_stmt.order_by(SensorReading.timestamp).limit(cap)

    raw_records = (await db.execute(raw_stmt)).scalars().all()
    canon_records = (await db.execute(canon_stmt)).scalars().all()

    raw_df = pd.DataFrame(_raw_records_to_rows(raw_records))
    scores_df = pd.DataFrame(_canonical_records_to_rows(canon_records))
    if not raw_df.empty:
        merged = merge_anomaly_scores_into_readings(
            raw_df, scores_df, tenant_id, greenhouse_id
        )
    else:
        merged = scores_df
    return _df_to_json_rows(merged)


async def fetch_canonical_window(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(SensorReading)
        .where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
            SensorReading.timestamp >= start,
            SensorReading.timestamp <= end,
        )
        .order_by(SensorReading.timestamp)
        .limit(_cap(limit))
    )
    records = (await db.execute(stmt)).scalars().all()
    return _df_to_json_rows(pd.DataFrame(_canonical_records_to_rows(records)))


async def fetch_raw_window(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(RawReading)
        .where(
            RawReading.tenant_id == tenant_id,
            RawReading.greenhouse_id == greenhouse_id,
            RawReading.timestamp >= start,
            RawReading.timestamp <= end,
        )
        .order_by(RawReading.timestamp)
        .limit(_cap(limit))
    )
    records = (await db.execute(stmt)).scalars().all()
    return _df_to_json_rows(pd.DataFrame(_raw_records_to_rows(records)))


async def fetch_timestamp_bounds(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
) -> tuple[Optional[str], Optional[str]]:
    raw_bounds = await db.execute(
        select(func.min(RawReading.timestamp), func.max(RawReading.timestamp)).where(
            RawReading.tenant_id == tenant_id,
            RawReading.greenhouse_id == greenhouse_id,
        )
    )
    raw_min, raw_max = raw_bounds.one()
    if raw_min is not None and raw_max is not None:
        return _iso(raw_min), _iso(raw_max)

    sensor_bounds = await db.execute(
        select(
            func.min(SensorReading.timestamp), func.max(SensorReading.timestamp)
        ).where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
        )
    )
    sensor_min, sensor_max = sensor_bounds.one()
    if sensor_min is not None and sensor_max is not None:
        return _iso(sensor_min), _iso(sensor_max)
    return None, None


async def raw_readings_exist(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> bool:
    stmt = select(func.count()).select_from(RawReading).where(
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
    )
    if start is not None:
        stmt = stmt.where(RawReading.timestamp >= start)
    if end is not None:
        stmt = stmt.where(RawReading.timestamp <= end)
    count = (await db.execute(stmt)).scalar()
    return bool(count)


async def list_greenhouses(db: AsyncSession, *, tenant_id: str) -> list[str]:
    rows = (
        (
            await db.execute(
                select(distinct(RawReading.greenhouse_id)).where(
                    RawReading.tenant_id == tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        rows = (
            (
                await db.execute(
                    select(distinct(SensorReading.greenhouse_id)).where(
                        SensorReading.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return sorted(rows)


async def fetch_incidents(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    stmt = select(Incident).where(
        Incident.tenant_id == tenant_id,
        Incident.greenhouse_id == greenhouse_id,
    )
    if start is not None:
        stmt = stmt.where(Incident.detected_at >= start)
    if end is not None:
        stmt = stmt.where(Incident.detected_at <= end)
    stmt = stmt.order_by(Incident.detected_at.desc()).limit(_cap(limit))
    stmt = stmt.options(defer(Incident.sequence), defer(Incident.embedding))
    records = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "detected_at": _iso(r.detected_at),
            "anomaly_score": r.anomaly_score,
            "fault_type": r.fault_type,
            "root_cause": r.root_cause,
            "root_cause_details": r.root_cause_details,
            "actions_taken": r.actions_taken,
            "resolved": r.resolved,
        }
        for r in records
    ]
