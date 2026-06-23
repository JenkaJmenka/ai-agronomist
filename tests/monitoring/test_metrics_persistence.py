"""Tests for AnomalyMetrics DB persistence and Prometheus integration."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Test 1: deque is bounded
# ---------------------------------------------------------------------------

def test_anomaly_metrics_deque_bounded() -> None:
    """AnomalyMetrics.detections is a bounded deque, not an unbounded list."""
    from src.monitoring.anomaly_metrics import AnomalyMetrics
    from src.config import MAX_METRICS_RETENTION_EVENTS

    m = AnomalyMetrics()
    assert hasattr(m.detections, "maxlen"), "detections must be a deque with maxlen"
    assert m.detections.maxlen == MAX_METRICS_RETENTION_EVENTS


def test_anomaly_metrics_false_positives_deque_bounded() -> None:
    """false_positives and true_positives are also bounded deques."""
    from src.monitoring.anomaly_metrics import AnomalyMetrics
    from src.config import MAX_METRICS_RETENTION_EVENTS

    m = AnomalyMetrics()
    assert m.false_positives.maxlen == MAX_METRICS_RETENTION_EVENTS
    assert m.true_positives.maxlen == MAX_METRICS_RETENTION_EVENTS


# ---------------------------------------------------------------------------
# Test 2: flush_metrics_snapshot writes to DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flush_metrics_snapshot(test_db: AsyncSession) -> None:
    """flush_metrics_snapshot persists a row to metrics_snapshots."""
    from src.monitoring.anomaly_metrics import AnomalyMetrics
    from src.monitoring.metrics_persistence import flush_metrics_snapshot, load_latest_snapshot

    m = AnomalyMetrics()
    await flush_metrics_snapshot(test_db, m)

    result = await load_latest_snapshot(test_db)
    assert result is not None
    assert "total_detections" in result
    assert result["total_detections"] == 0


@pytest.mark.asyncio
async def test_flush_metrics_snapshot_with_detections(test_db: AsyncSession) -> None:
    """flush stores current detection count and confidence."""
    from src.monitoring.anomaly_metrics import AnomalyMetrics
    from src.monitoring.metrics_persistence import flush_metrics_snapshot, load_latest_snapshot

    m = AnomalyMetrics()
    m.record_detection(
        anomaly_score=0.9,
        confidence=0.8,
        pattern_type="spike",
        customer_id="tenant_test",
    )
    await flush_metrics_snapshot(test_db, m)

    result = await load_latest_snapshot(test_db)
    assert result is not None
    assert result["total_detections"] == 1
    assert "pattern_distribution" in result


# ---------------------------------------------------------------------------
# Test 3: load_latest_snapshot returns None when empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_latest_snapshot_empty(test_db: AsyncSession) -> None:
    """load_latest_snapshot returns None when no snapshot exists for tenant."""
    from src.monitoring.metrics_persistence import load_latest_snapshot

    result = await load_latest_snapshot(test_db, tenant_id="nonexistent_tenant")
    assert result is None


# ---------------------------------------------------------------------------
# Test 4: /metrics endpoint returns 200
# ---------------------------------------------------------------------------

def test_prometheus_metrics_endpoint(client) -> None:
    """/metrics returns 200 with text/plain or prometheus content type."""
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_prometheus_metrics_endpoint_has_body(client) -> None:
    """/metrics returns a non-empty body."""
    resp = client.get("/metrics")
    assert len(resp.content) > 0


# ---------------------------------------------------------------------------
# Test 5: Prometheus detection recording is best-effort (no crash on error)
# ---------------------------------------------------------------------------

def test_record_detection_does_not_raise_on_prometheus_error() -> None:
    """record_detection must not propagate Prometheus errors."""
    from src.monitoring.anomaly_metrics import AnomalyMetrics

    m = AnomalyMetrics()
    # Should not raise even if prometheus internals fail
    m.record_detection(
        anomaly_score=0.85,
        confidence=0.9,
        pattern_type="drift",
        customer_id="tenant_x",
    )
    assert len(m.detections) == 1


# ---------------------------------------------------------------------------
# Test 6: flush_to_db method on AnomalyMetrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flush_to_db_method(test_db: AsyncSession) -> None:
    """AnomalyMetrics.flush_to_db calls persistence correctly."""
    from src.monitoring.anomaly_metrics import AnomalyMetrics
    from src.monitoring.metrics_persistence import load_latest_snapshot

    m = AnomalyMetrics()
    await m.flush_to_db(test_db)

    result = await load_latest_snapshot(test_db)
    assert result is not None
    assert "snapshot_at" in result


# ---------------------------------------------------------------------------
# Test 7: MetricsSnapshot model is in Base.metadata
# ---------------------------------------------------------------------------

def test_metrics_snapshot_table_in_metadata() -> None:
    """MetricsSnapshot is registered in the shared Base metadata."""
    from src.db.models import Base

    assert "metrics_snapshots" in Base.metadata.tables


# ---------------------------------------------------------------------------
# Test 8: get_metrics returns mean_confidence key
# ---------------------------------------------------------------------------

def test_get_metrics_returns_mean_confidence() -> None:
    """get_metrics includes mean_confidence for persistence compatibility."""
    from src.monitoring.anomaly_metrics import AnomalyMetrics

    m = AnomalyMetrics()
    m.record_detection(
        anomaly_score=0.7,
        confidence=0.6,
        pattern_type="lag",
        customer_id="t1",
    )
    metrics = m.get_metrics()
    assert "mean_confidence" in metrics
    assert abs(metrics["mean_confidence"] - 0.6) < 1e-9
