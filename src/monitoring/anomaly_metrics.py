"""
Monitoring and observability for the anomaly detection system.

In-memory metrics collector used by the integrated inference pipeline.
Aggregates are periodically flushed to the database via flush_to_db.
"""

import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict

from src.config import MAX_METRICS_RETENTION_EVENTS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_metrics_instance: "AnomalyMetrics | None" = None


class AnomalyMetrics:
    """Track detection volume, false-positive rate, and confidence distribution."""

    def __init__(self) -> None:
        self.detections: deque = deque(maxlen=MAX_METRICS_RETENTION_EVENTS)
        self.false_positives: deque = deque(maxlen=MAX_METRICS_RETENTION_EVENTS)
        self.true_positives: deque = deque(maxlen=MAX_METRICS_RETENTION_EVENTS)
        self.pattern_distribution: Dict[str, int] = defaultdict(int)
        self.confidence_distribution: Dict[float, int] = defaultdict(int)

    def record_detection(
        self,
        anomaly_score: float,
        confidence: float,
        pattern_type: str,
        customer_id: str,
    ) -> None:
        """Record one pipeline detection event."""
        self.detections.append(
            {
                "timestamp": datetime.now(timezone.utc),
                "anomaly_score": anomaly_score,
                "confidence": confidence,
                "pattern_type": pattern_type,
                "customer_id": customer_id,
            }
        )
        self.pattern_distribution[pattern_type] += 1
        confidence_bucket = round(confidence * 10) / 10
        self.confidence_distribution[confidence_bucket] += 1

        # Prometheus is best-effort; any error must never crash inference.
        try:
            from src.monitoring.prometheus_metrics import record_prometheus_detection
            record_prometheus_detection(
                tenant_id=customer_id if customer_id else "unknown",
                pattern_type=pattern_type if pattern_type else "unknown",
                confidence=confidence if confidence is not None else 0.0,
            )
        except Exception:
            pass

    def record_false_positive(self, incident_id: str) -> None:
        """Operator marked an incident as a false positive."""
        self.false_positives.append(
            {
                "timestamp": datetime.now(timezone.utc),
                "incident_id": incident_id,
            }
        )

    def record_true_positive(self, incident_id: str) -> None:
        """Operator confirmed a true anomaly."""
        self.true_positives.append(
            {
                "timestamp": datetime.now(timezone.utc),
                "incident_id": incident_id,
            }
        )

    def get_metrics(self, hours: int = 24) -> Dict[str, Any]:
        """Return aggregate metrics for the last *hours*."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent = [d for d in self.detections if d["timestamp"] >= cutoff]
        total = len(recent)

        recent_fp = [f for f in self.false_positives if f["timestamp"] >= cutoff]
        recent_tp = [t for t in self.true_positives if t["timestamp"] >= cutoff]

        if total == 0:
            return {
                "period_hours": hours,
                "total_detections": 0,
                "false_positives": len(recent_fp),
                "true_positives": len(recent_tp),
                "false_positive_rate": 0.0,
                "avg_confidence": 0.0,
                "mean_confidence": 0.0,
                "pattern_distribution": {},
                "confidence_distribution": {},
            }
        avg_confidence = sum(d["confidence"] for d in recent) / total

        return {
            "period_hours": hours,
            "total_detections": total,
            "false_positives": len(recent_fp),
            "true_positives": len(recent_tp),
            "false_positive_rate": len(recent_fp) / max(total, 1),
            "avg_confidence": avg_confidence,
            "mean_confidence": avg_confidence,
            "pattern_distribution": dict(self.pattern_distribution),
            "confidence_distribution": {
                str(k): v for k, v in self.confidence_distribution.items()
            },
        }

    async def flush_to_db(self, db: "AsyncSession") -> None:
        """Persist current aggregate metrics to the database."""
        from src.monitoring.metrics_persistence import flush_metrics_snapshot
        await flush_metrics_snapshot(db, self)

    def reset(self) -> None:
        """Clear all recorded metrics (for tests)."""
        self.detections.clear()
        self.false_positives.clear()
        self.true_positives.clear()
        self.pattern_distribution.clear()
        self.confidence_distribution.clear()


def get_anomaly_metrics() -> AnomalyMetrics:
    """Return the process-wide metrics singleton."""
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = AnomalyMetrics()
    return _metrics_instance


def reset_anomaly_metrics() -> None:
    """Reset the singleton (test helper)."""
    global _metrics_instance
    if _metrics_instance is not None:
        _metrics_instance.reset()
