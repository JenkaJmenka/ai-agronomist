"""
Phase 4.1 integration tests: comprehensive pipeline, feedback wiring, metrics.

Covers:
- AnomalyMetrics in-memory observability
- ComprehensiveInferencePipeline service orchestration
- POST /api/inference/comprehensive and /batch
- POST /api/feedback/mark-anomaly (DB-backed, no double-retrain)
- GET /api/monitoring/metrics
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Generator, List
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.main import app
from src.api.services.orchestrator_registry import (
    clear_orchestrator_cache,
    get_memory_cache,
)
from src.api.services.comprehensive_pipeline import (
    ComprehensiveInferencePipeline,
    build_default_baseline,
)
from src.config import INPUT_DIM, LATENT_DIM, MODEL_CONFIG, RANDOM_SEED, SEQ_LEN
from src.db.incident_memory import IncidentMemory
from src.db.models import Base, Incident
from src.models.inference import AnomalyDetector
from src.monitoring.anomaly_metrics import AnomalyMetrics, reset_anomaly_metrics
from tests.integration.conftest_pipeline import baseline_sequences, sample_sequence


# ---------------------------------------------------------------------------
# AnomalyMetrics (unit)
# ---------------------------------------------------------------------------


class TestAnomalyMetrics:
    def test_empty_metrics_returns_zeros(self) -> None:
        metrics = AnomalyMetrics()
        result = metrics.get_metrics(hours=24)
        assert result["total_detections"] == 0
        assert result["false_positive_rate"] == 0.0
        assert result["avg_confidence"] == 0.0

    def test_record_detection_increments_total(self) -> None:
        metrics = AnomalyMetrics()
        metrics.record_detection(0.9, 0.85, "drift", "cust_a")
        result = metrics.get_metrics()
        assert result["total_detections"] == 1
        assert result["avg_confidence"] == pytest.approx(0.85)

    def test_pattern_distribution_tracked(self) -> None:
        metrics = AnomalyMetrics()
        metrics.record_detection(0.8, 0.7, "spike", "cust_a")
        metrics.record_detection(0.8, 0.7, "drift", "cust_a")
        result = metrics.get_metrics()
        assert result["pattern_distribution"]["spike"] == 1
        assert result["pattern_distribution"]["drift"] == 1

    def test_false_positive_rate_computed(self) -> None:
        metrics = AnomalyMetrics()
        metrics.record_detection(0.9, 0.9, "drift", "cust_a")
        metrics.record_false_positive("inc_1")
        result = metrics.get_metrics()
        assert result["false_positives"] == 1
        assert result["false_positive_rate"] == pytest.approx(1.0)

    def test_true_positive_recorded(self) -> None:
        metrics = AnomalyMetrics()
        metrics.record_true_positive("inc_2")
        result = metrics.get_metrics()
        assert result["true_positives"] == 1

    def test_old_detections_excluded_by_window(self) -> None:
        metrics = AnomalyMetrics()
        old_ts = datetime.now(timezone.utc) - timedelta(hours=48)
        metrics.detections.append(
            {
                "timestamp": old_ts,
                "anomaly_score": 0.5,
                "confidence": 0.5,
                "pattern_type": "drift",
                "customer_id": "cust_a",
            }
        )
        result = metrics.get_metrics(hours=24)
        assert result["total_detections"] == 0

    def test_reset_clears_all(self) -> None:
        metrics = AnomalyMetrics()
        metrics.record_detection(0.9, 0.9, "drift", "cust_a")
        metrics.reset()
        assert metrics.get_metrics()["total_detections"] == 0


class TestBuildDefaultBaseline:
    def test_shape(self) -> None:
        baseline = build_default_baseline(5)
        assert baseline.shape == (5, SEQ_LEN, INPUT_DIM)


# ---------------------------------------------------------------------------
# ComprehensiveInferencePipeline (service)
# ---------------------------------------------------------------------------


class TestComprehensiveInferencePipeline:
    @pytest.mark.asyncio
    async def test_run_returns_all_expected_fields(
        self, detector: AnomalyDetector, db_session: AsyncSession
    ) -> None:
        pipeline = ComprehensiveInferencePipeline(detector=detector)
        result = await pipeline.run(
            customer_id="cust_svc",
            sequence=sample_sequence(),
            db=db_session,
            baseline_sequences=baseline_sequences(),
        )
        expected = {
            "is_anomaly",
            "anomaly_score",
            "confidence",
            "confidence_level",
            "pattern_type",
            "attribution",
            "affected_sensors",
            "pattern_scores",
            "root_cause",
            "root_cause_confidence",
            "reasoning",
            "alternative_causes",
            "severity",
            "urgency",
            "immediate_actions",
            "follow_up_actions",
            "all_recommendations",
            "requires_technician",
            "estimated_resolution_minutes",
            "incident_id",
            "timestamp",
            "pipeline_version",
        }
        assert expected.issubset(result.keys())
        assert result["pipeline_version"] == "2.0"

    @pytest.mark.asyncio
    async def test_run_stores_incident_in_db(
        self, detector: AnomalyDetector, db_session: AsyncSession, patch_anomaly_score
    ) -> None:
        """Incident is stored in DB only when is_anomaly=True.

        patch_anomaly_score forces raw_score=2.0 → anomaly_score=1.0 which
        exceeds ANOMALY_THRESHOLD (0.8), guaranteeing is_anomaly=True so that
        the store_incident gate is exercised.
        """
        pipeline = ComprehensiveInferencePipeline(detector=detector)
        result = await pipeline.run(
            customer_id="cust_store",
            sequence=sample_sequence(),
            db=db_session,
            baseline_sequences=baseline_sequences(),
        )
        assert result["is_anomaly"] is True, "patch_anomaly_score should force anomaly"
        memory = IncidentMemory(db_session)
        stored = await memory.find_similar(
            embedding=[0.0] * LATENT_DIM,
            tenant_id="cust_store",
            k=10,
        )
        assert len(stored) >= 1
        assert result["incident_id"] is not None

    @pytest.mark.asyncio
    async def test_run_records_metrics(
        self, detector: AnomalyDetector, db_session: AsyncSession
    ) -> None:
        metrics = AnomalyMetrics()
        pipeline = ComprehensiveInferencePipeline(
            detector=detector, metrics=metrics
        )
        await pipeline.run(
            customer_id="cust_metrics",
            sequence=sample_sequence(),
            db=db_session,
            baseline_sequences=baseline_sequences(),
        )
        assert metrics.get_metrics()["total_detections"] == 1

    @pytest.mark.asyncio
    async def test_learning_progress_when_orchestrator_exists(
        self, detector: AnomalyDetector, db_session: AsyncSession
    ) -> None:
        from src.models.retraining_orchestrator import RetrainingOrchestrator

        customer_id = "cust_progress"
        orch = RetrainingOrchestrator(customer_id=customer_id)
        orch.observe(
            incident_id="inc_prog",
            is_true_anomaly=True,
            operator_id="op_1",
            pattern_type_observed="drift",
        )
        get_memory_cache()[customer_id] = orch
        pipeline = ComprehensiveInferencePipeline(
            detector=detector,
            orchestrators=get_memory_cache(),
        )
        result = await pipeline.run(
            customer_id=customer_id,
            sequence=sample_sequence(),
            db=db_session,
            baseline_sequences=baseline_sequences(),
        )
        assert result["learning_progress"] is not None
        assert "high_quality_feedback" in result["learning_progress"]
        assert "needed_for_retrain" in result["learning_progress"]

    @pytest.mark.asyncio
    async def test_invalid_sequence_shape_raises(
        self, detector: AnomalyDetector, db_session: AsyncSession
    ) -> None:
        pipeline = ComprehensiveInferencePipeline(detector=detector)
        bad = [[1.0] * INPUT_DIM]  # too short
        with pytest.raises(ValueError, match="sequence shape"):
            await pipeline.run(
                customer_id="cust_bad",
                sequence=bad,
                db=db_session,
            )


# ---------------------------------------------------------------------------
# API: comprehensive inference
# ---------------------------------------------------------------------------


class TestComprehensiveInferenceAPI:
    def test_comprehensive_returns_200(
        self, client: TestClient, comprehensive_payload: dict, checkpoint_path: str
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            response = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert "incident_id" in body
        assert "confidence" in body
        assert "root_cause" in body
        assert "immediate_actions" in body

    def test_comprehensive_all_required_fields_present(
        self, client: TestClient, comprehensive_payload: dict, checkpoint_path: str
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            response = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        body = response.json()
        required = [
            "is_anomaly",
            "anomaly_score",
            "confidence",
            "pattern_type",
            "attribution",
            "root_cause",
            "immediate_actions",
            "incident_id",
        ]
        for field in required:
            assert field in body, f"missing {field}"

    def test_comprehensive_invalid_sequence_returns_422(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/api/inference/comprehensive",
            json={
                "customer_id": "cust",
                "sequence": [[1.0, 2.0, 3.0, 4.0]],
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_comprehensive_empty_customer_returns_422(
        self, client: TestClient, comprehensive_payload: dict
    ) -> None:
        comprehensive_payload["customer_id"] = ""
        response = client.post(
            "/api/inference/comprehensive", json=comprehensive_payload
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestBatchInferenceAPI:
    def test_batch_returns_all_results(
        self, client: TestClient, checkpoint_path: str
    ) -> None:
        payload = {
            "customer_id": "tenant_batch",
            "greenhouse_id": "gh_1",
            "sequences": [sample_sequence(1), sample_sequence(2)],
        }
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            response = client.post("/api/inference/batch", json=payload)
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["batch_size"] == 2
        assert len(body["results"]) == 2
        assert "avg_confidence" in body
        assert "anomalies_detected" in body

    def test_batch_empty_sequences_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/inference/batch",
            json={"customer_id": "cust", "sequences": []},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# API: integrated feedback
# ---------------------------------------------------------------------------


async def _store_test_incident(
    db: AsyncSession, tenant_id: str = "tenant_fb"
) -> str:
    memory = IncidentMemory(db)
    embedding = [0.1] * LATENT_DIM
    return await memory.store_incident(
        tenant_id=tenant_id,
        greenhouse_id="gh_1",
        embedding=embedding,
        anomaly_score=0.75,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
        root_cause="ventilation_failure",
    )


class TestFeedbackIntegratedAPI:
    @pytest.mark.asyncio
    async def test_mark_anomaly_records_feedback(
        self, client: TestClient, db_session: AsyncSession
    ) -> None:
        incident_id = await _store_test_incident(db_session)
        response = client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": incident_id,
                "is_true_anomaly": True,
                "operator_id": "op_alice",
                "customer_id": "tenant_fb",
                "pattern_type_observed": "drift",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "feedback_recorded"
        assert body["incident_id"] == incident_id
        assert body["learning_triggered"] is False

    @pytest.mark.asyncio
    async def test_mark_anomaly_updates_incident_tags(
        self, client: TestClient, db_session: AsyncSession
    ) -> None:
        incident_id = await _store_test_incident(db_session)
        client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": incident_id,
                "is_true_anomaly": False,
                "operator_id": "op_bob",
                "customer_id": "tenant_fb",
            },
        )
        from sqlalchemy import select

        result = await db_session.execute(
            select(Incident).where(Incident.id == incident_id)
        )
        incident = result.scalar_one()
        assert "false_positive" in (incident.tags or [])
        assert incident.resolved is True

    @pytest.mark.asyncio
    async def test_mark_anomaly_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": "nonexistent-id",
                "is_true_anomaly": True,
                "operator_id": "op_alice",
                "customer_id": "tenant_fb",
            },
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_mark_anomaly_wrong_tenant_returns_403(
        self, client: TestClient, db_session: AsyncSession
    ) -> None:
        incident_id = await _store_test_incident(db_session, tenant_id="tenant_a")
        response = client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": incident_id,
                "is_true_anomaly": True,
                "operator_id": "op_alice",
                "customer_id": "tenant_b",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.asyncio
    async def test_mark_anomaly_no_double_retrain_trigger(
        self, client: TestClient, db_session: AsyncSession
    ) -> None:
        """Retraining must come only from collect_operator_feedback auto-trigger."""
        incident_id = await _store_test_incident(db_session, tenant_id="cust_retrain")
        mock_orch = MagicMock()
        mock_orch.observe.return_value = {
            "status": "feedback_recorded",
            "learning_triggered": True,
            "feedback_count": 20,
            "high_quality_count": 20,
            "retraining": {"status": "retraining_completed"},
        }
        with patch(
            "src.api.routes.feedback_integrated.get_orchestrator",
            new=AsyncMock(return_value=mock_orch),
        ), patch(
            "src.api.routes.feedback_integrated.save_orchestrator",
            new=AsyncMock(),
        ):
            response = client.post(
                "/api/feedback/mark-anomaly",
                json={
                    "incident_id": incident_id,
                    "is_true_anomaly": True,
                    "operator_id": "op_alice",
                    "customer_id": "cust_retrain",
                },
            )
        assert response.status_code == status.HTTP_200_OK
        mock_orch.observe.assert_called_once()
        body = response.json()
        assert body["learning_triggered"] is True
        assert body["retraining"]["status"] == "retraining_completed"


# ---------------------------------------------------------------------------
# API: monitoring metrics
# ---------------------------------------------------------------------------


class TestMonitoringAPI:
    def test_metrics_empty_returns_200(self, client: TestClient) -> None:
        response = client.get("/api/monitoring/metrics")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total_detections"] == 0
        assert "false_positive_rate" in body

    def test_metrics_after_detection(
        self, client: TestClient, comprehensive_payload: dict, checkpoint_path: str
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        response = client.get("/api/monitoring/metrics?hours=24")
        body = response.json()
        assert body["total_detections"] >= 1

    def test_metrics_hours_query_param(self, client: TestClient) -> None:
        response = client.get("/api/monitoring/metrics?hours=48")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["period_hours"] == 48

    def test_metrics_invalid_hours_rejected(self, client: TestClient) -> None:
        response = client.get("/api/monitoring/metrics?hours=0")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# End-to-end feedback loop
# ---------------------------------------------------------------------------


class TestFeedbackLoopIntegration:
    @pytest.mark.asyncio
    async def test_detect_then_feedback_updates_metrics(
        self,
        client: TestClient,
        db_session: AsyncSession,
        comprehensive_payload: dict,
        checkpoint_path: str,
        patch_anomaly_score,
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            detect = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        assert detect.status_code == status.HTTP_200_OK
        incident_id = detect.json()["incident_id"]
        assert incident_id is not None, "patch_anomaly_score should produce anomaly"

        feedback = client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": incident_id,
                "is_true_anomaly": True,
                "operator_id": "op_loop",
                "customer_id": comprehensive_payload["customer_id"],
                "pattern_type_observed": "drift",
            },
        )
        assert feedback.status_code == status.HTTP_200_OK

        metrics = client.get("/api/monitoring/metrics").json()
        assert metrics["total_detections"] >= 1
        assert metrics["true_positives"] >= 1

    def test_pipeline_version_in_response(
        self, client: TestClient, comprehensive_payload: dict, checkpoint_path: str
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            response = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        assert response.json()["pipeline_version"] == "2.0"

    def test_confidence_level_is_string(
        self, client: TestClient, comprehensive_payload: dict, checkpoint_path: str
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            response = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        assert isinstance(response.json()["confidence_level"], str)

    def test_attribution_sums_to_one(
        self, client: TestClient, comprehensive_payload: dict, checkpoint_path: str
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            response = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        attribution = response.json()["attribution"]
        total = sum(attribution.values())
        assert total == pytest.approx(1.0, abs=0.05)

    def test_pattern_scores_has_seven_patterns(
        self, client: TestClient, comprehensive_payload: dict, checkpoint_path: str
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            response = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        scores = response.json()["pattern_scores"]
        assert len(scores) == 7

    def test_learning_progress_after_feedback(
        self,
        client: TestClient,
        comprehensive_payload: dict,
        checkpoint_path: str,
        patch_anomaly_score,
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            detect = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        assert detect.status_code == status.HTTP_200_OK
        incident_id = detect.json()["incident_id"]
        assert incident_id is not None, "patch_anomaly_score should produce anomaly"
        client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": incident_id,
                "is_true_anomaly": True,
                "operator_id": "op_progress",
                "customer_id": comprehensive_payload["customer_id"],
                "pattern_type_observed": "drift",
            },
        )
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            second = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        progress = second.json().get("learning_progress")
        assert progress is not None
        assert "high_quality_feedback" in progress
        assert "needed_for_retrain" in progress

    def test_comprehensive_internal_error_returns_500(
        self, client: TestClient, comprehensive_payload: dict
    ) -> None:
        with patch(
            "src.api.routes.inference_integrated._get_pipeline",
            side_effect=RuntimeError("model load failed"),
        ):
            response = client.post(
                "/api/inference/comprehensive", json=comprehensive_payload
            )
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_batch_internal_error_returns_500(self, client: TestClient) -> None:
        payload = {
            "customer_id": "cust_err",
            "sequences": [sample_sequence()],
        }
        with patch(
            "src.api.routes.inference_integrated._get_pipeline",
            side_effect=RuntimeError("batch failed"),
        ):
            response = client.post("/api/inference/batch", json=payload)
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
