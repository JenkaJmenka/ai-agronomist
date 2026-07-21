import logging
import math
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.sensor_data import UploadResponse
from src.db.models import RawReading, SensorReading
from src.db.session import get_db
from src.vendors.csv_adapter import CSVAdapter, pandas_csv_reader_kwargs, parse_raw_rows

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Anomaly-injection type catalogue ────────────────────────────────────────

ANOMALY_TYPES: list[str] = [
    "random",
    "temperature_spike",
    "temperature_drop",
    "humidity_spike",
    "humidity_drop",
    "co2_surge",
    "light_failure",
    "multi_sensor",
    "gradual_drift",
]

# Canonical sensor fields to make anomalous for each type (others stay normal)
_TYPE_CANONICAL_TARGETS: dict[str, set[str]] = {
    "random": {"temperature", "humidity", "co2", "light"},
    "temperature_spike": {"temperature"},
    "temperature_drop": {"temperature"},
    "humidity_spike": {"humidity"},
    "humidity_drop": {"humidity"},
    "co2_surge": {"co2"},
    "light_failure": {"light"},
    "multi_sensor": {"temperature", "humidity", "co2", "light"},
    "gradual_drift": {"temperature", "humidity"},
}

# Direction of deviation for targeted sensors
_TYPE_DIRECTION: dict[str, str] = {
    "random": "mixed",
    "temperature_spike": "high",
    "temperature_drop": "low",
    "humidity_spike": "high",
    "humidity_drop": "low",
    "co2_surge": "high",
    "light_failure": "low",
    "multi_sensor": "mixed",
    "gradual_drift": "drift",
}

# Anomaly score range per type
_TYPE_SCORE_RANGE: dict[str, tuple[float, float]] = {
    "random": (0.82, 0.99),
    "temperature_spike": (0.85, 0.99),
    "temperature_drop": (0.82, 0.96),
    "humidity_spike": (0.80, 0.95),
    "humidity_drop": (0.78, 0.94),
    "co2_surge": (0.88, 0.99),
    "light_failure": (0.85, 0.98),
    "multi_sensor": (0.90, 0.99),
    "gradual_drift": (0.60, 0.90),
}

# Raw column keyword substrings to spike (empty list = all raw columns)
_TYPE_RAW_KEYWORDS: dict[str, list[str]] = {
    "random": [],
    "temperature_spike": ["temp"],
    "temperature_drop": ["temp"],
    "humidity_spike": ["humid"],
    "humidity_drop": ["humid"],
    "co2_surge": ["co2"],
    "light_failure": ["light", "lux", "par"],
    "multi_sensor": [],
    "gradual_drift": ["temp", "humid"],
}

# Canonical sensor fallback statistics when no raw data exists
_CANONICAL_STATS: dict[str, tuple[float, float, float]] = {
    "temperature": (19.0, 28.0, 23.0),
    "humidity": (55.0, 72.0, 63.0),
    "co2": (380.0, 520.0, 420.0),
    "light": (0.0, 950.0, 200.0),
}


@router.post("/upload", response_model=UploadResponse)
async def upload_csv(
    file: UploadFile = File(...),
    tenant_id: str = Form(...),
    greenhouse_id: str = Form("default"),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload CSV file with sensor readings. Uses a memory-safe chunking
    approach with CSVAdapter to validate and process data before database insertion.
    Also stores raw readings (all columns) for full-fidelity dashboard display.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported")

    # Write to a temporary file in chunks to prevent memory explosion
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            while chunk := await file.read(1024 * 1024):  # 1MB buffer chunk
                tmp.write(chunk)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write uploaded file: {e}")

    adapter = CSVAdapter({"filepath": tmp_path})
    try:
        adapter.connect()
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=422, detail=f"Invalid CSV format: {e}")

    CHUNK_SIZE = 5000
    total_uploaded = 0
    total_raw_uploaded = 0

    ranges = {
        "temperature": {"min": float("inf"), "max": float("-inf")},
        "humidity": {"min": float("inf"), "max": float("-inf")},
        "co2": {"min": float("inf"), "max": float("-inf")},
        "light": {"min": float("inf"), "max": float("-inf")},
    }

    props = adapter._csv_props

    try:
        reader = pd.read_csv(
            tmp_path,
            chunksize=CHUNK_SIZE,
            **pandas_csv_reader_kwargs(props),
        )
    except pd.errors.EmptyDataError:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail="Invalid CSV format: Empty file")
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail=f"Invalid CSV format: {str(e)}")

    try:
        for raw_chunk in reader:
            if raw_chunk.empty:
                continue

            # --- Primary path (optional): normalize to canonical 4-sensor schema ---
            # Non-fatal: CSVs without temperature/humidity skip this path gracefully.
            try:
                df = adapter.normalize(raw_chunk)
                adapter.validate(df)

                for col in ("temperature", "humidity", "co2", "light"):
                    ranges[col]["min"] = min(ranges[col]["min"], float(df[col].min()))
                    ranges[col]["max"] = max(ranges[col]["max"], float(df[col].max()))

                from src.db.upsert import bulk_upsert_rows
                # Vectorized: convert timestamps once, build sensor dicts without iterrows
                _py_timestamps = df["timestamp"].dt.to_pydatetime()
                _sensor_rows = df[["temperature", "humidity", "co2", "light"]].to_dict("records")
                records = [
                    {
                        "tenant_id": tenant_id,
                        "greenhouse_id": greenhouse_id,
                        "timestamp": ts,
                        **sensor_row,
                    }
                    for ts, sensor_row in zip(_py_timestamps, _sensor_rows)
                ]

                try:
                    await bulk_upsert_rows(
                        db,
                        SensorReading,
                        records,
                        update_cols=["temperature", "humidity", "co2", "light"],
                        preserve_when_null=["anomaly_score", "anomaly_type"],
                    )
                    await db.commit()
                    total_uploaded += len(records)
                except Exception as e:
                    await db.rollback()
                    raise HTTPException(
                        status_code=500,
                        detail=f"Database error during chunk insert: {str(e)}",
                    )

            except HTTPException:
                raise
            except Exception as e:
                await db.rollback()
                logger.info(
                    "SensorReading normalization skipped for tenant=%s greenhouse=%s: %s",
                    tenant_id,
                    greenhouse_id,
                    e,
                )

            # --- Always-run path: store raw readings with all CSV columns ---
            try:
                raw_df, _ = parse_raw_rows(raw_chunk)
                if not raw_df.empty:
                    sensor_cols = [c for c in raw_df.columns if c not in ("timestamp", "source")]
                    from src.db.upsert import bulk_upsert_rows
                    # Vectorized: convert timestamps + source once; iterate dicts (no Series per row)
                    _raw_py_ts = raw_df["timestamp"].dt.to_pydatetime()
                    _raw_sources = raw_df["source"].tolist()
                    _raw_row_dicts = (
                        raw_df[sensor_cols].to_dict("records")
                        if sensor_cols
                        else [{} for _ in range(len(raw_df))]
                    )
                    raw_records = []
                    for ts, source, row_dict in zip(_raw_py_ts, _raw_sources, _raw_row_dicts):
                        data_dict = {
                            col: row_dict[col]
                            for col in sensor_cols
                            if row_dict[col] is not None
                            and not (isinstance(row_dict[col], float) and pd.isna(row_dict[col]))
                        }
                        if not data_dict:
                            continue
                        raw_records.append({
                            "tenant_id": tenant_id,
                            "greenhouse_id": greenhouse_id,
                            "timestamp": ts,
                            "source": source or None,
                            "data": data_dict,
                        })
                    if raw_records:
                        await bulk_upsert_rows(
                            db,
                            RawReading,
                            raw_records,
                            update_cols=["source", "data"],
                        )
                        await db.commit()
                        total_raw_uploaded += len(raw_records)
            except Exception as raw_exc:
                logger.warning(
                    "RawReading insert failed for tenant=%s greenhouse=%s: %s",
                    tenant_id,
                    greenhouse_id,
                    raw_exc,
                    exc_info=True,
                )
                await db.rollback()

    finally:
        adapter.disconnect()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if total_uploaded == 0 and total_raw_uploaded == 0:
        raise HTTPException(
            status_code=422,
            detail="No data could be extracted from the uploaded CSV. Check column names and data format.",
        )

    # Fix ranges if no canonical sensor data was found
    if total_uploaded == 0:
        for col in ("temperature", "humidity", "co2", "light"):
            ranges[col]["min"] = 0.0
            ranges[col]["max"] = 0.0

    return UploadResponse(
        uploaded=total_uploaded or total_raw_uploaded,
        errors=0,
        skipped=0,
        ranges=ranges,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
    )


@router.delete("/upload/data")
async def clear_tenant_data(
    tenant_id: str,
    greenhouse_id: str = "default",
    db: AsyncSession = Depends(get_db),
):
    """Delete all greenhouse-scoped data for a tenant/greenhouse.

    Removes SensorReading + RawReading (including synthetic inject rows),
    related Incident / IncidentAction rows, and the live Redis stream so
    mock/live buffers cannot immediately re-surface cleared history.
    """
    from sqlalchemy import select as _select

    from src.db.models import Incident, IncidentAction

    raw_result = await db.execute(
        delete(RawReading).where(
            RawReading.tenant_id == tenant_id,
            RawReading.greenhouse_id == greenhouse_id,
        )
    )
    sensor_result = await db.execute(
        delete(SensorReading).where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
        )
    )

    id_result = await db.execute(
        _select(Incident.id).where(
            Incident.tenant_id == tenant_id,
            Incident.greenhouse_id == greenhouse_id,
        )
    )
    incident_ids = [row[0] for row in id_result.fetchall()]
    deleted_actions = 0
    if incident_ids:
        actions_result = await db.execute(
            delete(IncidentAction).where(IncidentAction.incident_id.in_(incident_ids))
        )
        deleted_actions = actions_result.rowcount or 0
    incidents_result = await db.execute(
        delete(Incident).where(
            Incident.tenant_id == tenant_id,
            Incident.greenhouse_id == greenhouse_id,
        )
    )
    await db.commit()

    # Best-effort: drop the live replay buffer so cleared rows don't reappear
    # from Redis XREAD until fresh events are published.
    live_stream_cleared = False
    try:
        from src.api.services.live_hub_provider import get_live_hub

        live_stream_cleared = await get_live_hub().clear_stream(tenant_id, greenhouse_id)
    except Exception:  # noqa: BLE001 — clear must succeed even if Redis is down
        logger.warning(
            "clear_tenant_data: live stream clear failed for %s/%s",
            tenant_id,
            greenhouse_id,
            exc_info=True,
        )

    return {
        "cleared": True,
        "deleted_raw": raw_result.rowcount or 0,
        "deleted_sensor": sensor_result.rowcount or 0,
        "deleted_incidents": incidents_result.rowcount or 0,
        "deleted_incident_actions": deleted_actions,
        "live_stream_cleared": live_stream_cleared,
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
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
    return {
        k: (min(vs), max(vs), sum(vs) / len(vs))
        for k, vs in per_col.items()
        if vs
    }


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
    """Return the first col_values entry whose name contains any of the keywords."""
    for kw in keywords:
        for col, v in col_values.items():
            if kw in col.lower():
                return v
    return fallback


def _normal_value(col: str, col_min: float, col_max: float, col_mean: float) -> float:
    """Generate a normal value inside the observed range with small noise."""
    noise = (col_max - col_min) * 0.05
    return round(max(col_min, min(col_max, col_mean + random.gauss(0, noise))), 2)


@router.post("/upload/inject-test-anomalies")
async def inject_test_anomalies(
    tenant_id: str = Form(...),
    greenhouse_id: str = Form("default"),
    n_normal: int = Form(96),
    n_anomalies: int = Form(8),
    anomaly_type: str = Form("random"),
    window_start: Optional[str] = Form(None),
    window_end: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Insert synthetic readings into the database.

    Reads column names and value ranges from existing RawReadings so injected
    spikes match the user's real sensors. Writes SensorReading (anomaly_score +
    anomaly_type set) and RawReading (source = synthetic:<type>).
    """
    from sqlalchemy import select as _select

    if anomaly_type not in ANOMALY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown anomaly_type '{anomaly_type}'. Valid: {ANOMALY_TYPES}",
        )

    # Resolve time window
    if window_start and window_end:
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
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=48)

    window_hours = max(1, int((end_dt - start_dt).total_seconds() / 3600))
    total = n_normal + n_anomalies
    interval_seconds = max(60, int((end_dt - start_dt).total_seconds() / max(total, 1)))

    # Learn column ranges from up to 500 recent real rows
    sample_rows = (
        await db.execute(
            _select(RawReading)
            .where(
                RawReading.tenant_id == tenant_id,
                RawReading.greenhouse_id == greenhouse_id,
            )
            .order_by(RawReading.timestamp.desc())
            .limit(500)
        )
    ).scalars().all()

    col_stats = _col_ranges_from_sample(sample_rows)
    has_raw_cols = bool(col_stats)

    if not col_stats:
        col_stats = dict(_CANONICAL_STATS)

    # Type-specific configuration
    direction = _TYPE_DIRECTION.get(anomaly_type, "mixed")
    canonical_targets = _TYPE_CANONICAL_TARGETS.get(anomaly_type, set())
    raw_keywords = _TYPE_RAW_KEYWORDS.get(anomaly_type, [])
    score_lo, score_hi = _TYPE_SCORE_RANGE.get(anomaly_type, (0.82, 0.99))

    raw_spike_cols: set[str] = (
        {col for col in col_stats if any(kw in col.lower() for kw in raw_keywords)}
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
        score = round(
            random.uniform(score_lo, score_hi) if is_anomaly else random.uniform(0.02, 0.28), 4
        )

        sensor_records.append(SensorReading(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            timestamp=ts,
            temperature=round(float(temp), 2),
            humidity=round(float(hum), 2),
            co2=round(float(co2), 1),
            light=round(float(light), 1),
            anomaly_score=score,
            anomaly_type=anomaly_type if is_anomaly else "synthetic_context",
        ))

        if has_raw_cols and col_values:
            raw_records.append(RawReading(
                tenant_id=tenant_id,
                greenhouse_id=greenhouse_id,
                timestamp=ts,
                source=f"synthetic:{anomaly_type}" if is_anomaly else "synthetic:context",
                data={k: v for k, v in col_values.items() if v is not None},
            ))

    db.add_all(sensor_records)
    if raw_records:
        db.add_all(raw_records)
    await db.commit()

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


@router.delete("/upload/inject-test-anomalies")
async def remove_synthetic_anomalies(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query("default"),
    anomaly_type: Optional[str] = Query(
        None,
        description="Specific type to remove; omit to remove all synthetic anomalies",
    ),
    db: AsyncSession = Depends(get_db),
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
    # (spike types + synthetic_context background rows).
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

    return {
        "deleted_sensor": sensor_result.rowcount,
        "deleted_raw": raw_result.rowcount,
        "anomaly_type": resolved_type or "all",
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
    }
