"""Persist AnomalyMetrics aggregates to the database."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import MetricsSnapshot

if TYPE_CHECKING:
    from src.monitoring.anomaly_metrics import AnomalyMetrics

logger = logging.getLogger(__name__)


async def flush_metrics_snapshot(db: AsyncSession, metrics: "AnomalyMetrics") -> None:
    """Write current AnomalyMetrics aggregate to the database."""
    try:
        aggregated = metrics.get_metrics()
        snapshot = MetricsSnapshot(
            # Global metrics; per-tenant breakdown is handled via Prometheus labels.
            tenant_id="__global__",
            snapshot_at=datetime.now(timezone.utc),
            total_detections=aggregated.get("total_detections", 0),
            false_positive_rate=aggregated.get("false_positive_rate", 0.0),
            mean_confidence=aggregated.get("mean_confidence", 0.0),
            pattern_distribution=dict(aggregated.get("pattern_distribution", {})),
            confidence_distribution={
                str(k): v
                for k, v in aggregated.get("confidence_distribution", {}).items()
            },
        )
        db.add(snapshot)
        await db.commit()
        logger.debug("Metrics snapshot flushed to DB")
    except Exception:
        logger.exception("Failed to flush metrics snapshot")
        await db.rollback()


async def load_latest_snapshot(
    db: AsyncSession, tenant_id: str = "__global__"
) -> Optional[dict]:
    """Load the most recent metrics snapshot for a tenant."""
    try:
        result = await db.execute(
            select(MetricsSnapshot)
            .where(MetricsSnapshot.tenant_id == tenant_id)
            .order_by(desc(MetricsSnapshot.snapshot_at))
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return {
            "total_detections": row.total_detections,
            "false_positive_rate": row.false_positive_rate,
            "mean_confidence": row.mean_confidence,
            "pattern_distribution": row.pattern_distribution,
            "confidence_distribution": row.confidence_distribution,
            "snapshot_at": row.snapshot_at.isoformat(),
        }
    except Exception:
        logger.exception("Failed to load metrics snapshot")
        return None
