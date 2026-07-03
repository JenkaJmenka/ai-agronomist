"""
API tests for Gap 1 — Vector Embeddings in inference responses.

Covers:
1. /api/inference returns embeddings of LATENT_DIM with uncertainties.
2. embedding_dim metadata equals LATENT_DIM from src/config.py.
3. BACKWARD COMPATIBILITY: every pre-existing InferenceResponse field is
   still present and correctly typed.
4. /api/inference/upload (CSV route) returns the same enriched fields.

The detector is patched (predict_batch_enriched) because real VAE scoring
is stochastic even in eval mode — no exact-score assertions against the
real model.
"""

import datetime
import io
from unittest.mock import patch

import numpy as np
import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.config import LATENT_DIM
from src.db.models import Base, SensorReading
from src.db.session import get_db

BATCH_SIZE = 12
RAW_SCORE = 0.3  # route divides by ANOMALY_SCALING_FACTOR=2.0 -> 0.15
MOCK_UNCERTAINTY = 0.1


def make_enriched_return(batch_size: int):
    """Build a deterministic (scores, embeddings, uncertainties) tuple where
    embedding row i is filled with i * 0.1 so passthrough can be verified."""
    scores = np.full(batch_size, RAW_SCORE, dtype=np.float32)
    embeddings = np.tile(
        np.arange(batch_size, dtype=np.float32).reshape(-1, 1) * 0.1,
        (1, LATENT_DIM),
    )
    uncertainties = np.full(batch_size, MOCK_UNCERTAINTY, dtype=np.float32)
    return scores, embeddings, uncertainties


@pytest.fixture
def test_sequences():
    rng = np.random.default_rng(42)
    return rng.random((BATCH_SIZE, 20, 4)).tolist()


@pytest_asyncio.fixture
async def test_database():
    """Isolated async in-memory SQLite database with a few seeded readings."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    TestingSessionLocal = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    async with TestingSessionLocal() as session:
        base_time = datetime.datetime.now(datetime.timezone.utc)
        for i in range(BATCH_SIZE):
            session.add(
                SensorReading(
                    tenant_id="tenant_1",
                    greenhouse_id="default",
                    timestamp=base_time - datetime.timedelta(minutes=i),
                    temperature=24.5,
                    humidity=50.3,
                    co2=400.0,
                    light=1200.0,
                    anomaly_score=None,
                )
            )
        await session.commit()

    async with TestingSessionLocal() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def test_client(test_database):
    async def override_get_db():
        yield test_database

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def patch_detector():
    """Patch the enriched prediction with deterministic embedding output."""
    with patch(
        "src.models.inference.AnomalyDetector.predict_batch_enriched"
    ) as mock_enriched:
        mock_enriched.side_effect = lambda seqs: make_enriched_return(len(seqs))
        yield mock_enriched


def post_inference(client, sequences):
    return client.post(
        "/api/inference",
        json={
            "sequences": sequences,
            "tenant_id": "tenant_1",
            "greenhouse_id": "default",
        },
    )


class TestInferenceEmbeddingFields:
    def test_response_contains_embeddings_of_latent_dim(
        self, test_client, test_sequences
    ):
        response = post_inference(test_client, test_sequences)
        assert response.status_code == status.HTTP_200_OK
        embeddings = response.json()["embeddings"]
        assert len(embeddings) == BATCH_SIZE
        assert all(len(vector) == LATENT_DIM for vector in embeddings)

    def test_embedding_dim_equals_latent_dim_config(
        self, test_client, test_sequences
    ):
        response = post_inference(test_client, test_sequences)
        assert response.json()["embedding_dim"] == LATENT_DIM

    def test_uncertainties_present_for_each_sequence(
        self, test_client, test_sequences
    ):
        uncertainties = post_inference(test_client, test_sequences).json()[
            "uncertainties"
        ]
        assert len(uncertainties) == BATCH_SIZE
        assert all(isinstance(value, float) for value in uncertainties)
        assert all(value == pytest.approx(MOCK_UNCERTAINTY) for value in uncertainties)

    def test_embedding_values_pass_through_unmodified(
        self, test_client, test_sequences
    ):
        embeddings = post_inference(test_client, test_sequences).json()["embeddings"]
        for row_index, vector in enumerate(embeddings):
            assert vector == pytest.approx([row_index * 0.1] * LATENT_DIM)

    def test_embedding_values_are_json_floats(self, test_client, test_sequences):
        embeddings = post_inference(test_client, test_sequences).json()["embeddings"]
        assert all(
            isinstance(value, float) for vector in embeddings for value in vector
        )


class TestBackwardCompatibility:
    """All pre-Gap-1 InferenceResponse fields must remain present and typed."""

    def test_all_preexisting_fields_present(self, test_client, test_sequences):
        payload = post_inference(test_client, test_sequences).json()
        for field_name in (
            "tenant_id",
            "greenhouse_id",
            "sequences_processed",
            "anomalies_detected",
            "anomaly_scores",
            "high_anomalies",
        ):
            assert field_name in payload, f"missing pre-existing field {field_name}"

    def test_preexisting_field_types_and_values(self, test_client, test_sequences):
        payload = post_inference(test_client, test_sequences).json()
        assert payload["tenant_id"] == "tenant_1"
        assert payload["greenhouse_id"] == "default"
        assert isinstance(payload["sequences_processed"], int)
        assert payload["sequences_processed"] == BATCH_SIZE
        assert isinstance(payload["anomalies_detected"], int)
        assert isinstance(payload["anomaly_scores"], list)
        assert len(payload["anomaly_scores"]) == BATCH_SIZE
        assert all(isinstance(score, float) for score in payload["anomaly_scores"])
        assert isinstance(payload["high_anomalies"], list)

    def test_scores_unchanged_by_embedding_enrichment(
        self, test_client, test_sequences
    ):
        scores = post_inference(test_client, test_sequences).json()["anomaly_scores"]
        # 0.3 raw / ANOMALY_SCALING_FACTOR (2.0) = 0.15 — same contract as before
        assert all(score == pytest.approx(0.15) for score in scores)

    def test_high_anomaly_schema_unchanged(
        self, patch_detector, test_client, test_sequences
    ):
        def high_first(seqs):
            scores, embeddings, uncertainties = make_enriched_return(len(seqs))
            scores[0] = 1.9  # -> 0.95 after scaling, above 0.8 threshold
            return scores, embeddings, uncertainties

        patch_detector.side_effect = high_first
        payload = post_inference(test_client, test_sequences).json()
        assert payload["anomalies_detected"] == 1
        entry = payload["high_anomalies"][0]
        assert set(entry) == {"sequence_index", "score", "timestamp"}
        assert entry["sequence_index"] == 0
        assert entry["score"] == pytest.approx(0.95)

    def test_empty_sequences_still_rejected_with_400(self, test_client):
        response = post_inference(test_client, [])
        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestCsvInferenceEmbeddings:
    @staticmethod
    def _csv_bytes() -> io.BytesIO:
        csv_content = "timestamp,temperature,humidity,co2,light\n" + "\n".join(
            f"2026-06-09T11:00:{i % 60:02d}Z,25.0,50.0,400.0,1200.0"
            for i in range(50)
        )
        return io.BytesIO(csv_content.encode())

    def test_upload_route_returns_enriched_fields_when_opted_in(self, test_client):
        response = test_client.post(
            "/api/inference/upload",
            files={"file": ("seq.csv", self._csv_bytes(), "text/csv")},
            data={
                "tenant_id": "tenant_1",
                "greenhouse_id": "house_alpha",
                "include_embeddings": "true",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        # 50 rows with seq_len 20 -> 31 sliding-window sequences
        assert payload["sequences_processed"] == 31
        assert payload["embedding_dim"] == LATENT_DIM
        assert len(payload["embeddings"]) == 31
        assert all(len(vector) == LATENT_DIM for vector in payload["embeddings"])
        assert len(payload["uncertainties"]) == 31
        # Pre-existing CSV-route fields stay intact
        assert payload["tenant_id"] == "tenant_1"
        assert payload["greenhouse_id"] == "house_alpha"
        assert len(payload["anomaly_scores"]) == 31
        assert "threshold_used" in payload
        assert payload["threshold_method"] in ("pot", "percentile")

    def test_upload_route_omits_embeddings_by_default(self, test_client):
        # Embeddings are opt-in on the upload route: retaining them for the
        # whole file is O(rows) memory and they were unused by any consumer.
        response = test_client.post(
            "/api/inference/upload",
            files={"file": ("seq.csv", self._csv_bytes(), "text/csv")},
            data={"tenant_id": "tenant_1", "greenhouse_id": "house_alpha"},
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["sequences_processed"] == 31
        assert payload["embeddings"] == []
        assert payload["uncertainties"] == []
        assert len(payload["anomaly_scores"]) == 31
