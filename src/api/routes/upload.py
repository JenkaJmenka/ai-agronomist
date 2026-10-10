import logging
import os
import tempfile

import pandas as pd
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.schemas.sensor_data import UploadResponse
from src.api.services.cache_invalidation import invalidate_response_cache
from src.api.services.synthetic_anomalies import (
    delete_synthetic_anomalies,
    inject_synthetic_anomalies,
)
from src.api.services.tenant_data_wipe import wipe_live_demo_data, wipe_tenant_data
from src.db.models import RawReading, SensorReading
from src.db.session import get_db
from src.db.upsert import bulk_upsert_rows
from src.vendors.csv_adapter import CSVAdapter, pandas_csv_reader_kwargs, parse_raw_rows

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/upload", response_model=UploadResponse)
async def upload_csv(
    file: UploadFile = File(...),
    tenant_id: str = Form(...),
    greenhouse_id: str = Form("default"),
    db: AsyncSession = Depends(get_db),
    auth_tenant: Optional[str] = Depends(require_api_key),
):
    """
    Upload CSV file with sensor readings. Uses a memory-safe chunking
    approach with CSVAdapter to validate and process data before database insertion.
    Also stores raw readings (all columns) for full-fidelity dashboard display.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
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
        col: {"min": float("inf"), "max": float("-inf")}
        for col in ("temperature", "humidity", "co2", "light")
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
                        # "anomaly_type" is a no-op here: preserve_when_null only
                        # applies to columns in update_cols (src/db/upsert.py),
                        # and update_cols is just the four sensor floats -- a
                        # re-upload never touches anomaly_type/source either way.
                        preserve_when_null=["anomaly_score"],
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

    await invalidate_response_cache("upload_csv")

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
    request: Request,
    tenant_id: str,
    greenhouse_id: str = "default",
    db: AsyncSession = Depends(get_db),
    auth_tenant: Optional[str] = Depends(require_api_key),
):
    """Delete all greenhouse-scoped data (cascade rules in services.tenant_data_wipe)."""
    enforce_tenant_access(auth_tenant, tenant_id)
    return await wipe_tenant_data(
        db,
        getattr(request.app.state, "live_runtime", None),
        tenant_id,
        greenhouse_id,
    )


@router.delete("/upload/live-data")
async def remove_live_demo_data(
    request: Request,
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query("default"),
    db: AsyncSession = Depends(get_db),
    auth_tenant: Optional[str] = Depends(require_api_key),
) -> dict:
    """Delete Go Live / mock SCADA rows only (filter rules in services.tenant_data_wipe)."""
    enforce_tenant_access(auth_tenant, tenant_id)
    return await wipe_live_demo_data(
        db,
        getattr(request.app.state, "live_runtime", None),
        tenant_id,
        greenhouse_id,
    )


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
    auth_tenant: Optional[str] = Depends(require_api_key),
):
    """Insert synthetic readings (generation rules in services.synthetic_anomalies)."""
    enforce_tenant_access(auth_tenant, tenant_id)
    return await inject_synthetic_anomalies(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        n_normal=n_normal,
        n_anomalies=n_anomalies,
        anomaly_type=anomaly_type,
        window_start=window_start,
        window_end=window_end,
    )


@router.delete("/upload/inject-test-anomalies")
async def remove_synthetic_anomalies(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query("default"),
    anomaly_type: Optional[str] = Query(
        None,
        description="Specific type to remove; omit to remove all synthetic anomalies",
    ),
    db: AsyncSession = Depends(get_db),
    auth_tenant: Optional[str] = Depends(require_api_key),
) -> dict:
    """Remove synthetically injected test anomalies. Real sensor data is never touched."""
    enforce_tenant_access(auth_tenant, tenant_id)
    return await delete_synthetic_anomalies(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        anomaly_type=anomaly_type,
    )
