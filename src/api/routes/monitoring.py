"""Monitoring API routes for pipeline observability."""

from fastapi import APIRouter, Query

from src.api.schemas.inference_integrated import MetricsResponse
from src.monitoring.anomaly_metrics import get_anomaly_metrics

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(
    hours: int = Query(24, ge=1, le=8760, description="Lookback window in hours"),
) -> MetricsResponse:
    """Return aggregate anomaly detection metrics."""
    raw = get_anomaly_metrics().get_metrics(hours=hours)
    return MetricsResponse(**raw)
