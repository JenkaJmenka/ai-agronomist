"""Persist uploaded CSV rows to SensorReading and RawReading tables."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from fastapi import HTTPException
from sqlalchemy import bindparam, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from src.db.models import RawReading, SensorReading
from src.db.upsert import bulk_upsert_rows
from src.utils.profiling import timed
from src.vendors.csv_adapter import CSVAdapter, pandas_csv_reader_kwargs, parse_raw_rows

logger = logging.getLogger(__name__)

CHUNK_SIZE = 5000


@dataclass(frozen=True)
class CsvPersistResult:
    sensor_rows: int
    raw_rows: int
    sensor_rows_inserted: int = 0
    sensor_rows_updated: int = 0
    raw_rows_inserted: int = 0
    raw_rows_updated: int = 0
    ranges: dict[str, dict[str, float]] | None = None


def _empty_ranges() -> dict[str, dict[str, float]]:
    return {
        col: {"min": float("inf"), "max": float("-inf")}
        for col in ("temperature", "humidity", "co2", "light")
    }


def normalize_persist_timestamp(value: Any) -> datetime:
    """Canonical UTC timestamp for deduplication and upsert keys."""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    elif hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise TypeError(f"Expected datetime-like value, got {type(value)!r}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.replace(microsecond=0)


def persist_timestamp_key(value: Any) -> str:
    """Stable string key for (tenant, greenhouse, timestamp) lookups."""
    key = normalize_timestamp_key(normalize_persist_timestamp(value))
    if key is None:
        raise ValueError("Cannot derive persist key from timestamp")
    return key


def normalize_timestamp_key(value: Any) -> str | None:
    """Normalize timestamps for score lookup across CSV and inference output."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if hasattr(value, "isoformat"):
        dt = value
        if not isinstance(dt, datetime):
            dt = normalize_persist_timestamp(dt)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        dt = dt.replace(microsecond=0)
        return dt.isoformat()
    text = str(value).strip()
    if not text or text == "unknown":
        return None
    try:
        return persist_timestamp_key(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return text


def lookup_anomaly_score(
    timestamp: Any,
    scores_by_timestamp: dict[str, float] | None,
) -> float | None:
    if not scores_by_timestamp:
        return None
    key = normalize_timestamp_key(timestamp)
    if key is None:
        return None
    if key in scores_by_timestamp:
        return scores_by_timestamp[key]
    if "." in key:
        return scores_by_timestamp.get(key.split(".")[0] + "+00:00")
    return None


def lookup_anomaly_metadata(
    timestamp: Any,
    metadata_by_timestamp: dict[str, dict[str, list[str]]] | None,
) -> dict[str, list[str]] | None:
    if not metadata_by_timestamp:
        return None
    key = normalize_timestamp_key(timestamp)
    if key is None:
        return None
    return metadata_by_timestamp.get(key)


async def _existing_timestamp_keys(
    db: AsyncSession,
    model: type,
    tenant_id: str,
    greenhouse_id: str,
    timestamps: list[datetime],
) -> set[str]:
    """Keys of rows that already exist — only used to split inserted/updated counts."""
    if not timestamps:
        return set()
    stmt = select(model.timestamp).where(
        model.tenant_id == tenant_id,
        model.greenhouse_id == greenhouse_id,
        model.timestamp.in_(list(set(timestamps))),
    )
    result = await db.execute(stmt)
    return {persist_timestamp_key(ts) for (ts,) in result.all()}


def _build_raw_data_dict(
    record: dict[str, Any],
    sensor_cols: list[str],
    metadata_by_timestamp: dict[str, dict[str, list[str]]] | None,
) -> dict[str, Any]:
    data_dict = {
        col: record[col]
        for col in sensor_cols
        if record.get(col) is not None
        and not (isinstance(record[col], float) and pd.isna(record[col]))
    }
    meta = lookup_anomaly_metadata(record["timestamp"], metadata_by_timestamp)
    if meta:
        if meta.get("columns"):
            data_dict["anomaly_columns"] = meta["columns"]
        if meta.get("detectors"):
            data_dict["anomaly_detectors"] = meta["detectors"]
    return data_dict


async def persist_readings_chunk(
    *,
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    df: pd.DataFrame | None,
    raw_df: pd.DataFrame | None,
    anomaly_scores_by_timestamp: dict[str, float] | None = None,
    anomaly_metadata_by_timestamp: dict[str, dict[str, list[str]]] | None = None,
    count_split: bool = True,
) -> tuple[int, int, int, int]:
    """Upsert one normalized chunk (SensorReading) and raw chunk (RawReading).

    Returns (sensor_inserted, sensor_updated, raw_inserted, raw_updated).
    Redelivery/re-upload safe: conflicts on (tenant_id, greenhouse_id,
    timestamp) update in place, and a NULL incoming anomaly_score never
    overwrites a previously stored one.

    ``count_split`` controls the inserted-vs-updated breakdown. Computing it
    needs a pre-SELECT of existing timestamp keys — one extra DB round-trip per
    table, per chunk. The portioned worker path only ever re-sums the two into a
    total, so it passes ``count_split=False`` to skip both pre-SELECTs (44 fewer
    round-trips on a 22-portion upload); the whole total is then reported as
    ``inserted``. The synchronous path keeps the accurate split (default True).
    """
    sensor_inserted = sensor_updated = raw_inserted = raw_updated = 0

    if df is not None and not df.empty:
        ts_norm = [normalize_persist_timestamp(v) for v in df["timestamp"]]
        existing_keys = (
            await _existing_timestamp_keys(
                db, SensorReading, tenant_id, greenhouse_id, ts_norm
            )
            if count_split
            else set()
        )
        rows: list[dict[str, Any]] = []
        for ts, temp, hum, co2, light in zip(
            ts_norm, df["temperature"], df["humidity"], df["co2"], df["light"]
        ):
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "greenhouse_id": greenhouse_id,
                    "timestamp": ts,
                    "temperature": float(temp),
                    "humidity": float(hum),
                    "co2": float(co2),
                    "light": float(light),
                    "anomaly_score": lookup_anomaly_score(ts, anomaly_scores_by_timestamp),
                }
            )
        with timed(logger, "persist.sensor_upsert", rows=len(rows), split=int(count_split)):
            written = await bulk_upsert_rows(
                db,
                SensorReading,
                rows,
                update_cols=("temperature", "humidity", "co2", "light", "anomaly_score"),
                preserve_when_null=("anomaly_score",),
            )
        sensor_updated = (
            len({persist_timestamp_key(ts) for ts in ts_norm} & existing_keys)
            if count_split
            else 0
        )
        sensor_inserted = written - sensor_updated

    if raw_df is not None and not raw_df.empty:
        sensor_cols = [c for c in raw_df.columns if c not in ("timestamp", "source")]
        raw_rows: list[dict[str, Any]] = []
        raw_ts_norm: list[datetime] = []
        for record in raw_df.to_dict(orient="records"):
            data_dict = _build_raw_data_dict(
                record, sensor_cols, anomaly_metadata_by_timestamp
            )
            if not data_dict:
                continue
            ts = normalize_persist_timestamp(record["timestamp"])
            raw_ts_norm.append(ts)
            raw_rows.append(
                {
                    "tenant_id": tenant_id,
                    "greenhouse_id": greenhouse_id,
                    "timestamp": ts,
                    "source": record.get("source") or None,
                    "data": data_dict,
                }
            )
        existing_raw_keys = (
            await _existing_timestamp_keys(
                db, RawReading, tenant_id, greenhouse_id, raw_ts_norm
            )
            if count_split
            else set()
        )
        with timed(logger, "persist.raw_upsert", rows=len(raw_rows), split=int(count_split)):
            written = await bulk_upsert_rows(
                db,
                RawReading,
                raw_rows,
                update_cols=("source", "data"),
            )
        raw_updated = (
            len({persist_timestamp_key(ts) for ts in raw_ts_norm} & existing_raw_keys)
            if count_split
            else 0
        )
        raw_inserted = written - raw_updated

    return sensor_inserted, sensor_updated, raw_inserted, raw_updated


async def apply_anomaly_scores(
    *,
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    anomaly_scores_by_timestamp: dict[str, float] | None,
    anomaly_metadata_by_timestamp: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    """Sparse post-pass: attach calibrated scores/metadata to already-persisted rows.

    Used when rows are persisted chunk-by-chunk before the file-level threshold
    is known (single-pass upload and the portioned upload finalize step).
    """
    if anomaly_scores_by_timestamp:
        params = []
        for key, score in anomaly_scores_by_timestamp.items():
            try:
                ts = normalize_persist_timestamp(
                    datetime.fromisoformat(key.replace("Z", "+00:00"))
                )
            except (ValueError, TypeError):
                continue
            params.append({"ts": ts, "score": float(score)})
        if params:
            # Core-level table update: executemany with WHERE bindparams is not
            # available through the ORM-enabled UPDATE path.
            table = SensorReading.__table__
            stmt = (
                update(table)
                .where(
                    table.c.tenant_id == tenant_id,
                    table.c.greenhouse_id == greenhouse_id,
                    table.c.timestamp == bindparam("ts"),
                )
                .values(anomaly_score=bindparam("score"))
            )
            await db.execute(stmt, params)

    if anomaly_metadata_by_timestamp:
        meta_ts: list[datetime] = []
        for key in anomaly_metadata_by_timestamp:
            try:
                meta_ts.append(
                    normalize_persist_timestamp(
                        datetime.fromisoformat(key.replace("Z", "+00:00"))
                    )
                )
            except (ValueError, TypeError):
                continue
        if meta_ts:
            result = await db.execute(
                select(RawReading).where(
                    RawReading.tenant_id == tenant_id,
                    RawReading.greenhouse_id == greenhouse_id,
                    RawReading.timestamp.in_(meta_ts),
                )
            )
            for row in result.scalars().all():
                meta = lookup_anomaly_metadata(row.timestamp, anomaly_metadata_by_timestamp)
                if not meta:
                    continue
                merged = dict(row.data or {})
                if meta.get("columns"):
                    merged["anomaly_columns"] = meta["columns"]
                if meta.get("detectors"):
                    merged["anomaly_detectors"] = meta["detectors"]
                row.data = merged

    await db.commit()


async def persist_csv_file(
    *,
    tmp_path: str,
    tenant_id: str,
    greenhouse_id: str,
    db: AsyncSession,
    adapter: CSVAdapter,
    anomaly_scores_by_timestamp: dict[str, float] | None = None,
    anomaly_metadata_by_timestamp: dict[str, dict[str, list[str]]] | None = None,
) -> CsvPersistResult:
    """Chunk-read a CSV file; upsert SensorReading + RawReading rows (no duplicates)."""
    props = adapter._csv_props
    sensor_inserted = 0
    sensor_updated = 0
    raw_inserted = 0
    raw_updated = 0
    rows_seen = 0
    ranges = _empty_ranges()

    try:
        reader = pd.read_csv(
            tmp_path,
            chunksize=CHUNK_SIZE,
            **pandas_csv_reader_kwargs(props),
        )
    except pd.errors.EmptyDataError as exc:
        raise HTTPException(status_code=400, detail="Invalid CSV format: Empty file") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid CSV format: {exc}") from exc

    for raw_chunk in reader:
        if raw_chunk.empty:
            continue

        try:
            df = adapter.normalize(raw_chunk)
            adapter.validate(df)
            rows_seen += len(df)

            for col in ("temperature", "humidity", "co2", "light"):
                ranges[col]["min"] = min(ranges[col]["min"], float(df[col].min()))
                ranges[col]["max"] = max(ranges[col]["max"], float(df[col].max()))

            s_ins, s_upd, _, _ = await persist_readings_chunk(
                db=db,
                tenant_id=tenant_id,
                greenhouse_id=greenhouse_id,
                df=df,
                raw_df=None,
                anomaly_scores_by_timestamp=anomaly_scores_by_timestamp,
            )
            sensor_inserted += s_ins
            sensor_updated += s_upd
            await db.commit()
        except HTTPException:
            raise
        except Exception as exc:
            await db.rollback()
            logger.info(
                "SensorReading normalization skipped for tenant=%s greenhouse=%s: %s",
                tenant_id,
                greenhouse_id,
                exc,
            )

        try:
            raw_df, _ = parse_raw_rows(raw_chunk)
            if not raw_df.empty:
                _, _, r_ins, r_upd = await persist_readings_chunk(
                    db=db,
                    tenant_id=tenant_id,
                    greenhouse_id=greenhouse_id,
                    df=None,
                    raw_df=raw_df,
                    anomaly_metadata_by_timestamp=anomaly_metadata_by_timestamp,
                )
                raw_inserted += r_ins
                raw_updated += r_upd
                await db.commit()
        except Exception as exc:
            logger.warning(
                "RawReading insert failed for tenant=%s greenhouse=%s: %s",
                tenant_id,
                greenhouse_id,
                exc,
                exc_info=True,
            )
            await db.rollback()

    if rows_seen == 0:
        raise HTTPException(
            status_code=422,
            detail="No data could be extracted from the uploaded CSV. Check column names and data format.",
        )

    touched = sensor_inserted + sensor_updated + raw_inserted + raw_updated
    if touched == 0:
        raise HTTPException(
            status_code=422,
            detail="No data could be extracted from the uploaded CSV. Check column names and data format.",
        )

    if sensor_inserted + sensor_updated == 0:
        for col in ("temperature", "humidity", "co2", "light"):
            ranges[col]["min"] = 0.0
            ranges[col]["max"] = 0.0

    return CsvPersistResult(
        sensor_rows=sensor_inserted + sensor_updated,
        raw_rows=raw_inserted + raw_updated,
        sensor_rows_inserted=sensor_inserted,
        sensor_rows_updated=sensor_updated,
        raw_rows_inserted=raw_inserted,
        raw_rows_updated=raw_updated,
        ranges=ranges,
    )
