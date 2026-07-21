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


def _window_filters(
    model: Any,
    tenant_id: str,
    greenhouse_id: str,
    start: Optional[datetime],
    end: Optional[datetime],
) -> list[Any]:
    conditions = [model.tenant_id == tenant_id, model.greenhouse_id == greenhouse_id]
    if start is not None:
        conditions.append(model.timestamp >= start)
    if end is not None:
        conditions.append(model.timestamp <= end)
    return conditions


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

    from src.dashboard.services.tz_normalize import ts_merge_key

    merged["timestamp"] = pd.to_datetime(merged["timestamp"])
    scores["timestamp"] = pd.to_datetime(scores["timestamp"])
    merged["_ts_key"] = ts_merge_key(merged["timestamp"])
    scores["_ts_key"] = ts_merge_key(scores["timestamp"])
    scores = scores.groupby("_ts_key", as_index=False)["anomaly_score"].max()
    merged = merged.drop_duplicates(subset=["_ts_key"], keep="first")
    merged = merged.merge(
        scores[["_ts_key", "anomaly_score"]],
        on="_ts_key",
        how="left",
        suffixes=("_db", "_upload"),
    )
    upload_col = "anomaly_score_upload"
    db_col = "anomaly_score_db"
    if upload_col in merged.columns:
        upload_scores = pd.to_numeric(merged[upload_col], errors="coerce")
        db_scores = (
            pd.to_numeric(merged[db_col], errors="coerce")
            if db_col in merged.columns
            else pd.Series(pd.NA, index=merged.index)
        )
        # Prefer upload/LSTM scores; fall back to persisted DB values.
        merged["anomaly_score"] = upload_scores.combine_first(db_scores)
        drop_cols = [c for c in (upload_col, db_col) if c in merged.columns]
        merged = merged.drop(columns=drop_cols)
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
    # Charts and table clients expect ascending time; merge/dedupe can scramble order.
    if "timestamp" in out.columns:
        out = out.sort_values("timestamp", kind="mergesort")
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
    """Raw rows (full CSV fidelity) merged with canonical anomaly scores.

    When the window exceeds the row cap, the NEWEST rows win: a monitoring
    window must drop history, never the most recent days (ascending
    LIMIT used to silently cut the tail of wide windows).
    """
    cap = _cap(limit)

    raw_stmt = (
        select(RawReading)
        .where(*_window_filters(RawReading, tenant_id, greenhouse_id, start, end))
        .order_by(RawReading.timestamp.desc())
        .limit(cap)
    )
    canon_stmt = (
        select(SensorReading)
        .where(*_window_filters(SensorReading, tenant_id, greenhouse_id, start, end))
        .order_by(SensorReading.timestamp.desc())
        .limit(cap)
    )

    # Downstream merge/LTTB expect ascending time — restore it post-cap.
    raw_records = list(reversed((await db.execute(raw_stmt)).scalars().all()))
    canon_records = list(reversed((await db.execute(canon_stmt)).scalars().all()))

    raw_df = pd.DataFrame(_raw_records_to_rows(raw_records))
    scores_df = pd.DataFrame(_canonical_records_to_rows(canon_records))
    if not raw_df.empty:
        merged = merge_anomaly_scores_into_readings(
            raw_df, scores_df, tenant_id, greenhouse_id
        )
    else:
        merged = scores_df
    return _df_to_json_rows(merged)


async def fetch_flagged_window(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    threshold: float,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Canonical rows scoring ≥ threshold, filtered in SQL (G2).

    The score filter runs BEFORE any row cap, so wide windows keep every
    flagged row. Filtering the capped merged window instead dropped the
    newest anomalies once a window passed READINGS_MAX_ROWS — a 6-month
    count could come out smaller than its own 1-month subset.
    """
    stmt = (
        select(SensorReading)
        .where(
            *_window_filters(SensorReading, tenant_id, greenhouse_id, start, end),
            SensorReading.anomaly_score >= threshold,
        )
        .order_by(SensorReading.timestamp)
        .limit(_cap(limit))
    )
    records = (await db.execute(stmt)).scalars().all()
    # Merge parity: duplicate upload timestamps keep the max score.
    best: dict[str, dict[str, Any]] = {}
    for row in _canonical_records_to_rows(records):
        key = _iso(row["timestamp"])
        row["timestamp"] = key
        prev = best.get(key)
        if prev is None or (row["anomaly_score"] or 0.0) > (prev["anomaly_score"] or 0.0):
            best[key] = row
    return [best[key] for key in sorted(best)]


async def count_flagged_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    threshold: float,
) -> int:
    """Exact flagged count for a window — COUNT(DISTINCT ts), never capped."""
    stmt = select(func.count(distinct(SensorReading.timestamp))).where(
        *_window_filters(SensorReading, tenant_id, greenhouse_id, start, end),
        SensorReading.anomaly_score >= threshold,
    )
    return int((await db.execute(stmt)).scalar() or 0)


async def count_window_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
) -> int:
    """Full-window row count (distinct timestamps; raw preferred), never capped."""
    for model in (RawReading, SensorReading):
        stmt = select(func.count(distinct(model.timestamp))).where(
            *_window_filters(model, tenant_id, greenhouse_id, start, end)
        )
        count = int((await db.execute(stmt)).scalar() or 0)
        if count:
            return count
    return 0


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
