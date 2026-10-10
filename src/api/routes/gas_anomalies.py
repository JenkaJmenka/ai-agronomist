"""
Gas sensor anomaly detection API routes.

Provides statistical anomaly detection over gas sensor data stored in the
RawReading table (C2H4, CO2, NO, NO2, NOx, CO).  Synthetic readings are
excluded from both baseline fitting and the returned time-series.

Endpoints:
- GET /api/gas/summary   — per-column baselines + anomaly counts + time-series
- GET /api/gas/readings  — raw time-series only (all gas columns)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.schemas.gas import GasReadingsResponse, GasSummaryResponse
from src.config import SYNTHETIC_SOURCE_PREFIX
from src.db.models import RawReading
from src.db.session import get_db
from src.models.gas_anomaly_detector import GAS_COLUMNS, GasAnomalyDetector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gas", tags=["gas"])

# Climate ``co2`` must NOT alias to ``gas_co2``. Environmental CO₂ from
# climate/mock SCADA is a different sensor than the dedicated gas-panel CO₂.


def _source_is_real(col: Any) -> Any:
    """SQLAlchemy clause: source IS NULL OR source NOT LIKE 'synthetic:%'."""
    return or_(col.is_(None), ~col.like(f"{SYNTHETIC_SOURCE_PREFIX}%"))


def _numeric_or_none(value: Any) -> Optional[float]:
    """Coerce a raw JSON value to float, or None when absent/non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _fetch_real_readings(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    hours: int,
) -> list[RawReading]:
    """Query non-synthetic RawReading rows for the given time window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = (
        select(RawReading)
        .where(
            RawReading.tenant_id == tenant_id,
            RawReading.greenhouse_id == greenhouse_id,
            RawReading.timestamp >= cutoff,
            _source_is_real(RawReading.source),
        )
        .order_by(RawReading.timestamp.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/summary", response_model=GasSummaryResponse)
async def gas_summary(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(..., min_length=1),
    hours: int = Query(24, ge=1, le=8760, description="Lookback window in hours"),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Return per-column gas anomaly baselines and time-series for the window.

    The baseline is fitted from all readings in the window, then each reading
    is scored against it.  Synthetic readings are excluded.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    rows = await _fetch_real_readings(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id, hours=hours
    )

    readings_data = [row.data or {} for row in rows]
    detector = GasAnomalyDetector()
    baselines = detector.fit(readings_data)

    # Accumulate per-column reading entries.
    col_entries: dict[str, list[dict[str, Any]]] = {
        col: [] for col in GAS_COLUMNS if col in baselines
    }

    for row in rows:
        row_data = row.data or {}
        row_values: dict[str, float] = {}
        for col in col_entries:
            raw = row_data.get(col)
            if raw is None:
                continue
            try:
                row_values[col] = float(raw)
            except (TypeError, ValueError):
                pass

        anomalies = detector.detect(row_values, baselines) if row_values else {}
        ts_iso = row.timestamp.isoformat()

        for col in col_entries:
            value: Optional[float] = row_values.get(col)
            anomaly = anomalies.get(col)
            col_entries[col].append(
                {
                    "timestamp": ts_iso,
                    "value": value,
                    "is_anomaly": anomaly.is_anomaly if anomaly else False,
                    "z_score": round(anomaly.z_score, 4) if anomaly else 0.0,
                    "severity": anomaly.severity if anomaly else "normal",
                    "method": anomaly.method if anomaly else "none",
                }
            )

    gas_columns: list[dict[str, Any]] = []
    for col in GAS_COLUMNS:
        if col not in baselines:
            continue
        baseline = baselines[col]
        entries = col_entries[col]
        anomaly_count = sum(1 for e in entries if e["is_anomaly"])

        current_value: Optional[float] = None
        for entry in reversed(entries):
            if entry["value"] is not None:
                current_value = entry["value"]
                break

        gas_columns.append(
            {
                "column": col,
                "mean": round(baseline.mean, 4),
                "std": round(baseline.std, 4),
                "anomaly_count": anomaly_count,
                "current_value": current_value,
                "readings": entries,
            }
        )

    return {"gas_columns": gas_columns, "window_hours": hours}


@router.get("/readings", response_model=GasReadingsResponse)
async def gas_readings(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(..., min_length=1),
    hours: int = Query(24, ge=1, le=8760, description="Lookback window in hours"),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Return raw gas sensor time-series without anomaly scoring.

    Useful for charting tools that want unprocessed data.  Synthetic
    readings are excluded.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    rows = await _fetch_real_readings(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id, hours=hours
    )

    readings = [
        {
            "timestamp": row.timestamp.isoformat(),
            "data": {
                col: _numeric_or_none((row.data or {}).get(col)) for col in GAS_COLUMNS
            },
        }
        for row in rows
    ]

    return {"readings": readings, "total": len(readings)}
