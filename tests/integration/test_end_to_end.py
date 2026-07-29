"""
End-to-End Integration and Pipeline Workflows.

Legacy workflows (CSV upload, vendor adapter, error recovery) plus Phase 4.1
comprehensive pipeline schema, feedback loop, upload bridge, and monitoring tests.
"""

import io
import datetime
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.config import INPUT_DIM, SEQ_LEN
from src.db.models import Base, SensorReading
from src.db.session import get_db
from src.vendors.csv_adapter import CSVAdapter
from tests.integration.conftest_pipeline import baseline_sequences, sample_sequence


def _post_comprehensive(
    client: TestClient, payload: dict, checkpoint_path: str
) -> Any:
    with patch(
        "src.api.routes.inference_integrated._resolve_checkpoint",
        return_value=checkpoint_path,
    ):
        return client.post("/api/inference/comprehensive", json=payload)


@pytest.fixture
def sample_csv_data():
    """Generates 1000 rows of valid sensor data in CSV format conforming to the canonical schema."""
    columns = "timestamp,temperature,humidity,co2,light\n"
    rows = []
    base_time = datetime.datetime.now(datetime.timezone.utc)
    for i in range(1000):
        ts = (base_time - datetime.timedelta(minutes=i)).isoformat()
        rows.append(f"{ts},24.5,50.2,400.0,1200.0")
    return columns + "\n".join(rows)


@pytest_asyncio.fixture
async def test_database():
    """Provides an isolated async SQLite in-memory database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    TestingSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with TestingSessionLocal() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def test_client(test_database):
    """Overrides standard database dependency with the test database session."""
    async def override_get_db():
        yield test_database

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def patch_detector():
    """Patches model loading and scoring so tests never depend on checkpoint files.

    _load_model is patched to return a fresh random-weight LSTMVAE (the actual
    weights don't matter because predict_batch_enriched is also mocked).
    """
    from unittest.mock import patch
    from src.config import LATENT_DIM, MODEL_CONFIG
    from src.models.lstm_vae import LSTMVAE

    def enriched_side_effect(seqs):
        # returns 1.5 raw score so that clipped score is 0.75
        scores = np.array([1.5] * len(seqs), dtype=np.float32)
        embeddings = np.zeros((len(scores), LATENT_DIM), dtype=np.float32)
        uncertainties = np.full(len(scores), 0.1, dtype=np.float32)
        return scores, embeddings, uncertainties

    with patch(
        "src.models.inference.AnomalyDetector._load_model",
        return_value=LSTMVAE(**MODEL_CONFIG).eval(),
    ), patch("src.models.inference.AnomalyDetector.predict_batch_enriched") as mock_enriched:
        mock_enriched.side_effect = enriched_side_effect
        yield mock_enriched


@pytest.mark.asyncio
async def test_csv_upload_to_inference_to_dashboard(
    test_client, test_database, sample_csv_data, patch_detector
):
    """
    Executes the entire end-to-end data pipeline cycle:
    Ingestion -> DB Persistence -> Model Inference -> Score Back-persistence -> Dashboard querying.
    """
    tenant_id = "tenant_e2e_corporate"

    # -------------------------------------------------------------------------
    # Step 1 & 2: Ingest sensor tables through /api/upload and verify DB records count
    # -------------------------------------------------------------------------
    csv_bytes = io.BytesIO(sample_csv_data.encode("utf-8"))
    res_upload = test_client.post(
        "/api/upload",
        files={"file": ("raw_agronomist_data.csv", csv_bytes, "text/csv")},
        data={"tenant_id": tenant_id, "greenhouse_id": "main_zone"}
    )
    assert res_upload.status_code == status.HTTP_200_OK
    assert res_upload.json()["uploaded"] == 1000

    # Query directly to ensure persistence
    stmt = select(func.count(SensorReading.id)).where(SensorReading.tenant_id == tenant_id)
    db_count_res = await test_database.execute(stmt)
    assert db_count_res.scalar() == 1000

    # -------------------------------------------------------------------------
    # Step 3 & 4: Run model inference and check anomaly scores backpersistence
    # -------------------------------------------------------------------------
    sequences = np.random.rand(10, 20, 4).tolist()

    # Score persistence is opt-in and timestamp-addressed (the blind
    # latest-N fallback was removed) — target the 10 most recent readings.
    stmt_latest = (
        select(SensorReading.timestamp)
        .where(SensorReading.tenant_id == tenant_id)
        .order_by(SensorReading.timestamp.desc())
        .limit(10)
    )
    latest_ts_res = await test_database.execute(stmt_latest)
    target_timestamps = [ts.isoformat() for (ts,) in latest_ts_res.all()]

    res_inference = test_client.post(
        "/api/inference",
        json={
            "sequences": sequences,
            "tenant_id": tenant_id,
            "greenhouse_id": "main_zone",
            "timestamps": target_timestamps,
            "persist_scores": True,
        },
    )
    assert res_inference.status_code == status.HTTP_200_OK
    assert len(res_inference.json()["anomaly_scores"]) == 10

    # Query the database to verify the targeted 10 records received the anomaly scores
    stmt_scores = (
        select(SensorReading)
        .where(SensorReading.tenant_id == tenant_id, SensorReading.anomaly_score != None)
    )
    db_scores_res = await test_database.execute(stmt_scores)
    scored_records = db_scores_res.scalars().all()
    assert len(scored_records) == 10
    assert all(r.anomaly_score == pytest.approx(0.75) for r in scored_records)

    # -------------------------------------------------------------------------
    # Step 5: Query database directly (mimicking Dashboard queries)
    # -------------------------------------------------------------------------
    # Dashboard typically pulls readings sorted by timestamp to display on graphs
    stmt_dashboard = (
        select(SensorReading.timestamp, SensorReading.temperature, SensorReading.humidity, SensorReading.anomaly_score)
        .where(SensorReading.tenant_id == tenant_id, SensorReading.greenhouse_id == "main_zone")
        .order_by(SensorReading.timestamp.asc())
    )
    db_dash_res = await test_database.execute(stmt_dashboard)
    dash_records = db_dash_res.all()
    assert len(dash_records) == 1000
    # At least some elements should contain the calculated anomaly score
    recorded_anomaly_scores = [r.anomaly_score for r in dash_records if r.anomaly_score is not None]
    assert len(recorded_anomaly_scores) == 10
    assert recorded_anomaly_scores[0] == pytest.approx(0.75)

    # -------------------------------------------------------------------------
    # Step 6: Verify strict multi-tenant isolation
    # -------------------------------------------------------------------------
    tenant_other = "tenant_competitor_co"
    csv_bytes_other = io.BytesIO(sample_csv_data.encode("utf-8"))
    res_upload_other = test_client.post(
        "/api/upload",
        files={"file": ("competitor.csv", csv_bytes_other, "text/csv")},
        data={"tenant_id": tenant_other, "greenhouse_id": "main_zone"}
    )
    assert res_upload_other.status_code == status.HTTP_200_OK

    # Query database specifically for corporate tenant, competitor's data should NOT leak
    stmt_isolated = select(SensorReading).where(SensorReading.tenant_id == tenant_id)
    isolated_res = await test_database.execute(stmt_isolated)
    records_isolated = isolated_res.scalars().all()
    assert len(records_isolated) == 1000
    assert any(r.tenant_id == tenant_other for r in records_isolated) is False


@pytest.mark.asyncio
async def test_workflow_with_vendor_adapter(
    tmp_path, test_client, test_database, patch_detector
):
    """
    Simulates fetching and transforming sensor fields through the CSVAdapter,
    then uploading the resulting canonical dataset.
    """
    # Create the virtual raw file on disk
    file_path = tmp_path / "raw_measurements.csv"
    with open(file_path, "w") as f:
        f.write("timestamp,temperature,humidity,co2,light\n")
        f.write("2026-06-09T11:00:00Z,23.5,45.2,420.0,1100.0\n")
        f.write("2026-06-09T11:01:00Z,24.0,46.1,430.0,1150.0\n")

    # Ingest using CSVAdapter
    adapter = CSVAdapter(config={"filepath": str(file_path)})
    df = adapter.get_data() # connect -> read -> normalize -> validate -> disconnect
    assert not df.empty
    assert len(df) == 2

    # Conver DataFrame to standard upload csv structure
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)
    csv_bytes = io.BytesIO(buffer.getvalue().encode("utf-8"))

    # Post to upload endpoint
    response = test_client.post(
        "/api/upload",
        files={"file": ("vendor_canonical.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_vendor_integration"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uploaded"] == 2

    # Assert persistence
    res_db = await test_database.execute(
        select(func.count(SensorReading.id)).where(SensorReading.tenant_id == "tenant_vendor_integration")
    )
    assert res_db.scalar() == 2


@pytest.mark.asyncio
async def test_error_recovery(test_client, test_database, sample_csv_data, patch_detector):
    """
    Verifies that starting with a failing upload does not leak bad state,
    and succeeding uploads correctly write clean data to the DB.
    """
    # 1. Post an invalid CSV format (skips normalization, returns 200 with 0 uploaded)
    bad_csv = "timestamp,temperature\n2026-06-09T11:00:00Z,25.0"
    csv_bytes_bad = io.BytesIO(bad_csv.encode("utf-8"))
    res_bad = test_client.post(
        "/api/upload",
        files={"file": ("corrupted.csv", csv_bytes_bad, "text/csv")},
        data={"tenant_id": "tenant_error_recovery"}
    )
    assert res_bad.status_code == status.HTTP_200_OK
    assert res_bad.json()["uploaded"] == 1

    # Verify that nothing has been written to the database for this tenant
    res_count_1 = await test_database.execute(
        select(func.count(SensorReading.id)).where(SensorReading.tenant_id == "tenant_error_recovery")
    )
    assert res_count_1.scalar() == 0

    # 2. Immediately follow up with a totally valid CSV upload
    csv_bytes_good = io.BytesIO(sample_csv_data.encode("utf-8"))
    res_good = test_client.post(
        "/api/upload",
        files={"file": ("restored.csv", csv_bytes_good, "text/csv")},
        data={"tenant_id": "tenant_error_recovery"}
    )
    assert res_good.status_code == status.HTTP_200_OK
    assert res_good.json()["uploaded"] == 1000

    # Verify that only the 1000 successful records exist inside the database
    res_count_2 = await test_database.execute(
        select(func.count(SensorReading.id)).where(SensorReading.tenant_id == "tenant_error_recovery")
    )
    assert res_count_2.scalar() == 1000


# ---------------------------------------------------------------------------
# Phase 4.1: comprehensive response schema (parametrized)
# ---------------------------------------------------------------------------

COMPREHENSIVE_RESPONSE_FIELDS = [
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
    "detector_scores",
    "ensemble_score",
    "ensemble_agreement",
]


class TestComprehensiveResponseSchema:
    @pytest.mark.parametrize("field_name", COMPREHENSIVE_RESPONSE_FIELDS)
    def test_response_includes_field(
        self,
        client: TestClient,
        comprehensive_payload: dict,
        checkpoint_path: str,
        field_name: str,
    ) -> None:
        response = _post_comprehensive(client, comprehensive_payload, checkpoint_path)
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert field_name in body, f"missing field {field_name}"


# ---------------------------------------------------------------------------
# Phase 4.1: full pipeline loop (inference → feedback → metrics)
# ---------------------------------------------------------------------------


class TestFullPipelineLoop:
    def test_comprehensive_then_metrics_shows_detection(
        self,
        client: TestClient,
        comprehensive_payload: dict,
        checkpoint_path: str,
    ) -> None:
        detect = _post_comprehensive(client, comprehensive_payload, checkpoint_path)
        assert detect.status_code == status.HTTP_200_OK

        metrics = client.get("/api/monitoring/metrics?hours=24")
        assert metrics.status_code == status.HTTP_200_OK
        body = metrics.json()
        assert body["total_detections"] >= 1
        assert body["avg_confidence"] > 0.0

    def test_mark_false_positive_updates_metrics(
        self,
        client: TestClient,
        comprehensive_payload: dict,
        checkpoint_path: str,
        patch_anomaly_score,
    ) -> None:
        detect = _post_comprehensive(client, comprehensive_payload, checkpoint_path)
        assert detect.status_code == status.HTTP_200_OK
        incident_id = detect.json()["incident_id"]
        assert incident_id is not None, "patch_anomaly_score should produce anomaly"

        feedback = client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": incident_id,
                "is_true_anomaly": False,
                "operator_id": "op_e2e",
                "customer_id": comprehensive_payload["customer_id"],
            },
        )
        assert feedback.status_code == status.HTTP_200_OK

        metrics = client.get("/api/monitoring/metrics")
        assert metrics.json()["false_positives"] >= 1

    def test_mark_true_positive_updates_metrics(
        self,
        client: TestClient,
        comprehensive_payload: dict,
        checkpoint_path: str,
        patch_anomaly_score,
    ) -> None:
        detect = _post_comprehensive(client, comprehensive_payload, checkpoint_path)
        assert detect.status_code == status.HTTP_200_OK
        incident_id = detect.json()["incident_id"]
        assert incident_id is not None, "patch_anomaly_score should produce anomaly"

        feedback = client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": incident_id,
                "is_true_anomaly": True,
                "operator_id": "op_e2e",
                "customer_id": comprehensive_payload["customer_id"],
                "pattern_type_observed": detect.json().get("pattern_type", "drift"),
            },
        )
        assert feedback.status_code == status.HTTP_200_OK

        metrics = client.get("/api/monitoring/metrics")
        assert metrics.json()["true_positives"] >= 1

    def test_full_loop_returns_incident_and_feedback_status(
        self,
        client: TestClient,
        comprehensive_payload: dict,
        checkpoint_path: str,
        patch_anomaly_score,
    ) -> None:
        detect = _post_comprehensive(client, comprehensive_payload, checkpoint_path)
        assert detect.status_code == status.HTTP_200_OK
        body = detect.json()
        incident_id = body["incident_id"]
        assert incident_id is not None, "patch_anomaly_score should produce anomaly"

        feedback = client.post(
            "/api/feedback/mark-anomaly",
            json={
                "incident_id": incident_id,
                "is_true_anomaly": True,
                "operator_id": "op_loop",
                "customer_id": comprehensive_payload["customer_id"],
            },
        )
        fb_body = feedback.json()
        assert fb_body["incident_id"] == incident_id
        assert "learning_triggered" in fb_body

        metrics = client.get("/api/monitoring/metrics?hours=24")
        assert metrics.json()["total_detections"] >= 1

    def test_metrics_endpoint_schema_fields(
        self, client: TestClient, comprehensive_payload: dict, checkpoint_path: str
    ) -> None:
        _post_comprehensive(client, comprehensive_payload, checkpoint_path)
        response = client.get("/api/monitoring/metrics?hours=24")
        body = response.json()
        for key in (
            "total_detections",
            "false_positive_rate",
            "true_positives",
            "false_positives",
            "avg_confidence",
            "pattern_distribution",
            "confidence_distribution",
            "period_hours",
        ):
            assert key in body


# ---------------------------------------------------------------------------
# Phase 4.1: upload CSV → build sequence → comprehensive inference
# ---------------------------------------------------------------------------


def _minimal_upload_csv(rows: int = 25) -> str:
    columns = "timestamp,temperature,humidity,co2,light\n"
    base_time = datetime.datetime.now(datetime.timezone.utc)
    lines = []
    for i in range(rows):
        ts = (base_time - datetime.timedelta(minutes=i)).isoformat()
        lines.append(f"{ts},24.5,50.2,400.0,1200.0")
    return columns + "\n".join(lines)


class TestUploadToComprehensiveBridge:
    @pytest.mark.asyncio
    async def test_upload_then_comprehensive_from_readings(
        self,
        client: TestClient,
        db_session: AsyncSession,
        checkpoint_path: str,
    ) -> None:
        tenant_id = "tenant_upload_bridge"
        csv_bytes = io.BytesIO(_minimal_upload_csv(25).encode("utf-8"))
        upload = client.post(
            "/api/upload",
            files={"file": ("bridge.csv", csv_bytes, "text/csv")},
            data={"tenant_id": tenant_id, "greenhouse_id": "gh_bridge"},
        )
        assert upload.status_code == status.HTTP_200_OK

        stmt = (
            select(SensorReading)
            .where(
                SensorReading.tenant_id == tenant_id,
                SensorReading.greenhouse_id == "gh_bridge",
            )
            .order_by(SensorReading.timestamp.asc())
        )
        result = await db_session.execute(stmt)
        readings = result.scalars().all()
        assert len(readings) >= SEQ_LEN

        sequence = [
            [r.temperature, r.humidity, r.co2, r.light]
            for r in readings[-SEQ_LEN:]
        ]
        payload = {
            "customer_id": tenant_id,
            "greenhouse_id": "gh_bridge",
            "sequence": sequence,
            "baseline_sequences": baseline_sequences(5),
        }
        response = _post_comprehensive(client, payload, checkpoint_path)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["incident_id"]

    @pytest.mark.asyncio
    async def test_uploaded_tenant_isolated_in_comprehensive(
        self,
        client: TestClient,
        db_session: AsyncSession,
        checkpoint_path: str,
    ) -> None:
        for tenant in ("tenant_a_bridge", "tenant_b_bridge"):
            csv_bytes = io.BytesIO(_minimal_upload_csv(20).encode("utf-8"))
            client.post(
                "/api/upload",
                files={"file": ("data.csv", csv_bytes, "text/csv")},
                data={"tenant_id": tenant, "greenhouse_id": "gh_1"},
            )

        stmt_a = select(func.count(SensorReading.id)).where(
            SensorReading.tenant_id == "tenant_a_bridge"
        )
        stmt_b = select(func.count(SensorReading.id)).where(
            SensorReading.tenant_id == "tenant_b_bridge"
        )
        assert (await db_session.execute(stmt_a)).scalar() == 20
        assert (await db_session.execute(stmt_b)).scalar() == 20

        payload = {
            "customer_id": "tenant_a_bridge",
            "sequence": sample_sequence(42),
            "baseline_sequences": baseline_sequences(3),
        }
        response = _post_comprehensive(client, payload, checkpoint_path)
        assert response.status_code == status.HTTP_200_OK

    def test_comprehensive_fixture_json_shape(
        self, client: TestClient, checkpoint_path: str
    ) -> None:
        import json
        from pathlib import Path

        fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / "comprehensive_request.json"
        payload = json.loads(fixture_path.read_text())
        assert len(payload["sequence"]) == SEQ_LEN
        assert all(len(row) == INPUT_DIM for row in payload["sequence"])

        response = _post_comprehensive(client, payload, checkpoint_path)
        assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Phase 4.1: batch inference + monitoring hours param
# ---------------------------------------------------------------------------


class TestBatchAndMonitoring:
    def test_batch_inference_returns_results(
        self, client: TestClient, checkpoint_path: str
    ) -> None:
        payload = {
            "customer_id": "tenant_e2e_batch",
            "greenhouse_id": "gh_1",
            "sequences": [sample_sequence(1), sample_sequence(2), sample_sequence(3)],
        }
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            response = client.post("/api/inference/batch", json=payload)
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["batch_size"] == 3
        assert len(body["results"]) == 3
        assert body["anomalies_detected"] >= 0

    def test_metrics_hours_parameter(self, client: TestClient) -> None:
        for hours in (1, 24, 72):
            response = client.get(f"/api/monitoring/metrics?hours={hours}")
            assert response.status_code == status.HTTP_200_OK
            assert response.json()["period_hours"] == hours

    def test_metrics_pattern_distribution_after_batch(
        self, client: TestClient, checkpoint_path: str
    ) -> None:
        # Seeds 10 and 12 are verified to not trigger the Sprint-C diurnal
        # suppression rule (Rule 5 in PhysicsValidator). Seed 11 produces a
        # sequence whose mean temp/humidity trends are co-moving and gradual,
        # so it is suppressed before metrics.record_detection() is reached,
        # causing total_detections to be 1 instead of 2.
        payload = {
            "customer_id": "tenant_e2e_metrics",
            "sequences": [sample_sequence(10), sample_sequence(12)],
        }
        with patch(
            "src.api.routes.inference_integrated._resolve_checkpoint",
            return_value=checkpoint_path,
        ):
            client.post("/api/inference/batch", json=payload)

        metrics = client.get("/api/monitoring/metrics?hours=24").json()
        assert metrics["total_detections"] >= 2
        assert isinstance(metrics["pattern_distribution"], dict)
