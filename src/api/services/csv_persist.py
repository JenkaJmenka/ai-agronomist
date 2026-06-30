"""Persist uploaded CSV rows to SensorReading and RawReading tables."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from src.db.models import RawReading, SensorReading
from src.vendors.csv_adapter import CSVAdapter, parse_raw_rows

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


async def _load_sensor_by_key(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    timestamps: list[datetime],
) -> dict[str, SensorReading]:
    if not timestamps:
        return {}
    norm_ts = [normalize_persist_timestamp(ts) for ts in timestamps]
    stmt = select(SensorReading).where(
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.timestamp >= min(norm_ts),
        SensorReading.timestamp <= max(norm_ts),
    )
    result = await db.execute(stmt)
    mapping: dict[str, SensorReading] = {}
    for row in result.scalars().all():
        key = persist_timestamp_key(row.timestamp)
        mapping.setdefault(key, row)
    return mapping


async def _load_raw_by_key(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    timestamps: list[datetime],
) -> dict[str, RawReading]:
    if not timestamps:
        return {}
    norm_ts = [normalize_persist_timestamp(ts) for ts in timestamps]
    stmt = select(RawReading).where(
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
        RawReading.timestamp >= min(norm_ts),
        RawReading.timestamp <= max(norm_ts),
    )
    result = await db.execute(stmt)
    mapping: dict[str, RawReading] = {}
    for row in result.scalars().all():
        key = persist_timestamp_key(row.timestamp)
        mapping.setdefault(key, row)
    return mapping


def _apply_sensor_update(
    existing: SensorReading,
    row: pd.Series,
    anomaly_score: float | None,
) -> None:
    existing.timestamp = normalize_persist_timestamp(row["timestamp"])
    existing.temperature = float(row["temperature"])
    existing.humidity = float(row["humidity"])
    existing.co2 = float(row["co2"])
    existing.light = float(row["light"])
    if anomaly_score is not None:
        existing.anomaly_score = anomaly_score


def _build_raw_data_dict(
    row: pd.Series,
    sensor_cols: list[str],
    metadata_by_timestamp: dict[str, dict[str, list[str]]] | None,
) -> dict[str, Any]:
    data_dict = {
        col: row[col]
        for col in sensor_cols
        if row[col] is not None
        and not (isinstance(row[col], float) and pd.isna(row[col]))
    }
    meta = lookup_anomaly_metadata(row["timestamp"], metadata_by_timestamp)
    if meta:
        if meta.get("columns"):
            data_dict["anomaly_columns"] = meta["columns"]
        if meta.get("detectors"):
            data_dict["anomaly_detectors"] = meta["detectors"]
    return data_dict


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
            on_bad_lines="skip",
            sep=props.get("sep", ","),
            encoding=props.get("encoding", "utf-8"),
            decimal=props.get("decimal", "."),
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

            chunk_timestamps = [row["timestamp"].to_pydatetime() for _, row in df.iterrows()]
            existing_sensor = await _load_sensor_by_key(
                db, tenant_id, greenhouse_id, chunk_timestamps
            )
            new_records: list[SensorReading] = []
            for _, row in df.iterrows():
                ts = normalize_persist_timestamp(row["timestamp"])
                key = persist_timestamp_key(ts)
                score = lookup_anomaly_score(ts, anomaly_scores_by_timestamp)
                existing = existing_sensor.get(key)
                if existing is not None:
                    _apply_sensor_update(existing, row, score)
                    sensor_updated += 1
                else:
                    new_records.append(
                        SensorReading(
                            tenant_id=tenant_id,
                            greenhouse_id=greenhouse_id,
                            timestamp=ts,
                            temperature=float(row["temperature"]),
                            humidity=float(row["humidity"]),
                            co2=float(row["co2"]),
                            light=float(row["light"]),
                            anomaly_score=score,
                        )
                    )
                    sensor_inserted += 1

            if new_records:
                db.add_all(new_records)
            if new_records or sensor_updated:
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
                sensor_cols = [c for c in raw_df.columns if c not in ("timestamp", "source")]
                chunk_timestamps = [
                    row["timestamp"].to_pydatetime() for _, row in raw_df.iterrows()
                ]
                existing_raw = await _load_raw_by_key(
                    db, tenant_id, greenhouse_id, chunk_timestamps
                )
                new_raw_records: list[RawReading] = []
                for _, row in raw_df.iterrows():
                    ts = normalize_persist_timestamp(row["timestamp"])
                    key = persist_timestamp_key(ts)
                    data_dict = _build_raw_data_dict(
                        row, sensor_cols, anomaly_metadata_by_timestamp
                    )
                    if not data_dict:
                        continue
                    existing = existing_raw.get(key)
                    if existing is not None:
                        existing.timestamp = ts
                        existing.source = row["source"] or None
                        existing.data = data_dict
                        raw_updated += 1
                    else:
                        new_raw_records.append(
                            RawReading(
                                tenant_id=tenant_id,
                                greenhouse_id=greenhouse_id,
                                timestamp=ts,
                                source=row["source"] or None,
                                data=data_dict,
                            )
                        )
                        raw_inserted += 1
                if new_raw_records:
                    db.add_all(new_raw_records)
                if new_raw_records or raw_updated:
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
