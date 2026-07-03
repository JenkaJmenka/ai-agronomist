import pytest_asyncio
"""
Integration and unit tests for the ML Model Inference API endpoint.

This file covers:
1. Valid inference execution returning anomaly scores.
2. Checking score output list quantity matches input batch size.
3. Checking score value ranges fit inside expected bounds [0.0, 1.0].
4. Missing tenant_id parameter error handling.
5. Invalid sequence matrix dimensions validation.
6. NaN/None inputs error recovery and response codes.
7. Verification that computed scores successfully save/persist to the DB.
8. Exception handling when the model checkpoint is missing or load failures occur (503).
9. High anomaly alerts listing and scoring criteria.
"""

import pytest
import numpy as np
import datetime
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import status
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from src.api.main import app
from src.config import LATENT_DIM
from src.db.session import get_db
from src.db.models import Base, SensorReading


def make_enriched_side_effect(score_fn):
    """
    Wraps a raw-score generator into the (scores, embeddings, uncertainties)
    tuple contract of AnomalyDetector.predict_batch_enriched.
    """
    def side_effect(seqs):
        scores = score_fn(seqs)
        batch_size = len(scores)
        embeddings = np.zeros((batch_size, LATENT_DIM), dtype=np.float32)
        uncertainties = np.full(batch_size, 0.1, dtype=np.float32)
        return scores, embeddings, uncertainties
    return side_effect


@pytest.fixture
async def test_sequences():
    """Generates a batch of 150 valid sequences with size (seq_len=20, input_dim=4)."""
    return np.random.rand(150, 20, 4).tolist()


@pytest_asyncio.fixture
async def test_database():
    """Provides an isolated async SQLite in-memory database seeded with tenant records."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    TestingSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Seed 150 database records for tenant_1 to allow updates in tests
    async with TestingSessionLocal() as session:
        base_time = datetime.datetime.now(datetime.timezone.utc)
        for i in range(150):
            reading = SensorReading(
                tenant_id="tenant_1",
                greenhouse_id="default",
                timestamp=base_time - datetime.timedelta(minutes=i),
                temperature=24.5,
                humidity=50.3,
                co2=400.0,
                light=1200.0,
                anomaly_score=None
            )
            session.add(reading)
        await session.commit()

    async with TestingSessionLocal() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def test_client(test_database):
    """Overrides standard database dependency with test database session."""
    async def override_get_db():
        yield test_database

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def patch_detector():
    """Autouse fixture that patches the AnomalyDetector with a predictable mock."""
    with patch("src.models.inference.AnomalyDetector.predict_batch_enriched") as mock_enriched:
        # We return 0.3 raw score because the inference route divides by 2.0 -> final score 0.15
        mock_enriched.side_effect = make_enriched_side_effect(
            lambda seqs: np.array([0.3] * len(seqs), dtype=np.float32)
        )
        yield mock_enriched


def test_inference_returns_scores(test_client, test_sequences):
    """POST /api/inference with valid sequences should return 200 and anomaly scores."""
    response = test_client.post(
        "/api/inference",
        json={"sequences": test_sequences, "tenant_id": "tenant_1", "greenhouse_id": "default"}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "anomaly_scores" in data
    assert "anomalies_detected" in data
    assert "high_anomalies" in data


def test_inference_score_shape(test_client, test_sequences):
    """Verifies that processing 150 sequences generates precisely 150 continuous scores."""
    response = test_client.post(
        "/api/inference",
        json={"sequences": test_sequences, "tenant_id": "tenant_1", "greenhouse_id": "default"}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert len(data["anomaly_scores"]) == 150


def test_inference_score_range(patch_detector, test_client, test_sequences):
    """Verifies that all returned anomaly score coordinates remain well-bounded between 0 and 1."""
    # Let's override the mock behavior to yield escalating boundaries from 0.0 to 2.0 (divided by 2 gives 0 to 1)
    patch_detector.side_effect = make_enriched_side_effect(
        lambda seqs: np.linspace(0.0, 2.0, len(seqs), dtype=np.float32)
    )

    response = test_client.post(
        "/api/inference",
        json={"sequences": test_sequences, "tenant_id": "tenant_1", "greenhouse_id": "default"}
    )
    assert response.status_code == status.HTTP_200_OK
    scores = response.json()["anomaly_scores"]
    assert min(scores) == pytest.approx(0.0)
    assert max(scores) == pytest.approx(1.0)
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_inference_missing_tenant_id(test_client, test_sequences):
    """POST /api/inference with missing tenant_id parameter should trigger validation 422 block."""
    response = test_client.post(
        "/api/inference",
        json={"sequences": test_sequences}  # missing tenant_id
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "tenant_id" in str(response.json()).lower() or "detail" in response.json()


def test_inference_invalid_sequence_shape(test_client):
    """POST with wrong dimensions (like shape 10x5 instead of 20x4) should fail validation with 422."""
    invalid_matrix = np.random.rand(10, 5).tolist() # invalid dims
    response = test_client.post(
        "/api/inference",
        json={"sequences": [invalid_matrix], "tenant_id": "tenant_1"}
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "sequence" in str(response.json()).lower() or "detail" in response.json()


def test_inference_nan_in_input(test_client, test_sequences):
    """Inputs containing NaN or None features should return error status code 400 or 422."""
    # Insert None/NaN in sequence details
    bad_sequences = test_sequences.copy()
    bad_sequences[0][0][0] = None  # invalid value

    response = test_client.post(
        "/api/inference",
        json={"sequences": bad_sequences, "tenant_id": "tenant_1"}
    )
    assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.asyncio
async def test_inference_saves_to_database(patch_detector, test_client, test_database, test_sequences):
    """Scores are persisted to the rows matching the supplied timestamps when persist_scores=True."""
    # Predict exactly 1.44 for all items (divided by 2.0 gives 0.72)
    patch_detector.side_effect = make_enriched_side_effect(
        lambda seqs: np.array([1.44] * len(seqs), dtype=np.float32)
    )

    result = await test_database.execute(
        select(SensorReading)
        .where(SensorReading.tenant_id == "tenant_1")
        .order_by(SensorReading.timestamp)
    )
    seeded_timestamps = [r.timestamp.isoformat() for r in result.scalars().all()]

    response = test_client.post(
        "/api/inference",
        json={
            "sequences": test_sequences,
            "tenant_id": "tenant_1",
            "greenhouse_id": "default",
            "timestamps": seeded_timestamps,
            "persist_scores": True,
        },
    )
    assert response.status_code == status.HTTP_200_OK

    # Query the database to retrieve rows and assert the presence of updated anomaly scores
    result = await test_database.execute(select(SensorReading).where(SensorReading.tenant_id == "tenant_1"))
    readings = result.scalars().all()
    assert len(readings) == 150
    assert all(r.anomaly_score == pytest.approx(0.72) for r in readings)


def test_inference_persist_without_timestamps_returns_400(test_client, test_sequences):
    """persist_scores=True without timestamps must be rejected (blind fallback was removed)."""
    response = test_client.post(
        "/api/inference",
        json={
            "sequences": test_sequences,
            "tenant_id": "tenant_1",
            "greenhouse_id": "default",
            "persist_scores": True,
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "timestamps required" in response.text


def test_inference_persist_with_mismatched_timestamps_returns_400(test_client, test_sequences):
    """persist_scores=True with a timestamp count != sequence count must be rejected."""
    response = test_client.post(
        "/api/inference",
        json={
            "sequences": test_sequences,
            "tenant_id": "tenant_1",
            "greenhouse_id": "default",
            "timestamps": ["2026-07-03T10:00:00+00:00"],
            "persist_scores": True,
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "timestamps required" in response.text


def test_inference_without_persist_does_not_require_timestamps(patch_detector, test_client, test_sequences):
    """Pure inference (persist_scores omitted/false) keeps working without timestamps."""
    response = test_client.post(
        "/api/inference",
        json={"sequences": test_sequences, "tenant_id": "tenant_1", "greenhouse_id": "default"},
    )
    assert response.status_code == status.HTTP_200_OK


def test_inference_model_not_found(test_client, test_sequences):
    """If AnomalyDetector throws Exception on init, endpoint will return 503 model not loaded error."""
    with patch("src.api.routes.inference.get_cached_detector") as mock_class:
        mock_class.side_effect = Exception("Model weights not found")
        response = test_client.post(
            "/api/inference",
            json={"sequences": test_sequences, "tenant_id": "tenant_1"}
        )
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "failed to initialize" in str(response.json()).lower()


def test_inference_response_includes_high_anomalies(patch_detector, test_client, test_sequences):
    """Scores verified above threshold value of 0.8 should trigger high anomaly listings in outputs."""
    # Force first sequence score at 1.9 (high anomaly, gives 0.95), rest safe at 0.24 (gives 0.12)
    def mock_predict(seqs):
        out = np.array([0.24] * len(seqs), dtype=np.float32)
        out[0] = 1.9
        return out

    patch_detector.side_effect = make_enriched_side_effect(mock_predict)

    response = test_client.post(
        "/api/inference",
        json={"sequences": test_sequences, "tenant_id": "tenant_1", "greenhouse_id": "default"}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["anomalies_detected"] == 1
    assert len(data["high_anomalies"]) == 1
    assert data["high_anomalies"][0]["sequence_index"] == 0
    assert data["high_anomalies"][0]["score"] == pytest.approx(0.95)


def test_inference_upload_csv(test_client, patch_detector):
    """POST /api/inference/upload with valid CSV should process and return anomaly details."""
    csv_content = (
        "timestamp,temperature,humidity,co2,light\n"
        + "\n".join([f"2026-06-09T11:00:{i%60:02d}Z,25.0,50.0,400.0,1200.0" for i in range(50)])
    )
    import io
    csv_bytes = io.BytesIO(csv_content.encode("utf-8"))
    
    response = test_client.post(
        "/api/inference/upload",
        files={"file": ("test_inference.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_1", "greenhouse_id": "house_alpha"}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["tenant_id"] == "tenant_1"
    assert data["greenhouse_id"] == "house_alpha"
    # 50 rows, seq_len 20 -> 50 - 20 + 1 = 31 sequences processed
    assert data["sequences_processed"] == 31
    assert len(data["anomaly_scores"]) == 31
    assert "threshold_used" in data
    assert data["threshold_used"] < 0.8
    assert data["threshold_method"] in ("pot", "percentile")
    assert isinstance(data["imputed_channels"], list)
    assert data["statistical_summary"] is not None
