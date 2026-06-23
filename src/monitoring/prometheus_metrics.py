"""Prometheus metrics for the anomaly detection pipeline."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

    _registry = CollectorRegistry()

    anomaly_detections_total = Counter(
        "anomaly_detections_total",
        "Total anomaly detections recorded",
        ["tenant_id", "pattern_type"],
        registry=_registry,
    )
    anomaly_confidence_histogram = Histogram(
        "anomaly_confidence",
        "Distribution of anomaly confidence scores",
        ["tenant_id"],
        buckets=[0.1, 0.2, 0.4, 0.6, 0.75, 0.85, 0.9, 0.95, 1.0],
        registry=_registry,
    )
    false_positive_rate_gauge = Gauge(
        "false_positive_rate",
        "Estimated false positive rate",
        ["tenant_id"],
        registry=_registry,
    )

    _PROMETHEUS_AVAILABLE = True

except ImportError:
    _PROMETHEUS_AVAILABLE = False
    _registry = None
    logger.warning("prometheus-client not installed; /metrics endpoint disabled")


def record_prometheus_detection(
    tenant_id: str, pattern_type: str, confidence: float
) -> None:
    """Increment detection counter and observe confidence histogram."""
    if not _PROMETHEUS_AVAILABLE:
        return
    anomaly_detections_total.labels(
        tenant_id=tenant_id, pattern_type=pattern_type
    ).inc()
    anomaly_confidence_histogram.labels(tenant_id=tenant_id).observe(confidence)


def generate_prometheus_output() -> tuple[bytes, str]:
    """Return (body_bytes, content_type) for the /metrics scrape endpoint."""
    if not _PROMETHEUS_AVAILABLE or _registry is None:
        return b"# prometheus-client not installed\n", "text/plain"
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    return generate_latest(_registry), CONTENT_TYPE_LATEST
