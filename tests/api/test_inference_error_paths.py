"""
Error-path and edge-case tests for /api/inference and /api/inference/upload.

Targets route branches not covered by the happy-path suites: timestamp
back-persistence, commit-failure rollback, detector init failure, CSV
validation failures, chunk inference failure, short/empty CSV inputs, and
high-anomaly extraction on the CSV route.

The detector is patched (predict_batch_enriched) because real VAE scoring
is stochastic even in eval mode — no exact-score assertions against the
real model.
"""

import datetime
import io
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.models import Base, SensorReading
from src.db.session import get_db

BATCH_SIZE = 4
RAW_SCORE = 0.3  # route divides by ANOMALY_SCALING_FACTOR=2.0 -> 0.15
HIGH_RAW_SCORE = 1.9  # -> 0.95 after scaling, above the 0.8 threshold
SEED_BASE_TIME = datetime.datetime(2026, 6, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)
ROUTE_PATCH_PREFIX = "src.api.routes.inference"


def make_enriched_return(batch_size: int, raw_score: float = RAW_SCORE):
    scores = np.full(batch_size, raw_score, dtype=np.float32)
    embeddings = np.zeros((batch_size, 8), dtype=np.float32)
    uncertainties = np.full(batch_size, 0.1, dtype=np.float32)
    return scores, embeddings, uncertainties


def make_csv_bytes(row_count: int) -> io.BytesIO:
    """Valid sensor CSV with `row_count` data rows, one per minute."""
    header = "timestamp,temperature,humidity,co2,light"
    rows = "\n".join(
        f"2026-06-06T11:{i // 60:02d}:{i % 60:02d}Z,25.0,50.0,400.0,1200.0"
        for i in range(row_count)
    )
    return io.BytesIO(f"{header}\n{rows}".encode())


def seeded_timestamps() -> list:
    return [SEED_BASE_TIME - datetime.timedelta(minutes=i) for i in range(BATCH_SIZE)]


@pytest.fixture
def test_sequences():
    rng = np.random.default_rng(42)
    return rng.random((BATCH_SIZE, 20, 4)).tolist()


@pytest_asyncio.fixture
async def test_database():
    """Isolated async in-memory SQLite DB seeded at fixed, known timestamps."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    TestingSessionLocal = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    async with TestingSessionLocal() as session:
        for moment in seeded_timestamps():
            session.add(
                SensorReading(
                    tenant_id="tenant_1",
                    greenhouse_id="default",
                    timestamp=moment,
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
    with patch(
        "src.models.inference.AnomalyDetector.predict_batch_enriched"
    ) as mock_enriched:
        mock_enriched.side_effect = lambda seqs: make_enriched_return(len(seqs))
        yield mock_enriched


def post_inference(client, sequences, timestamps=None):
    payload = {
        "sequences": sequences,
        "tenant_id": "tenant_1",
        "greenhouse_id": "default",
    }
    if timestamps is not None:
        payload["timestamps"] = timestamps
    return client.post("/api/inference", json=payload)


def post_csv(client, file_bytes, filename="seq.csv"):
    return client.post(
        "/api/inference/upload",
        files={"file": (filename, file_bytes, "text/csv")},
        data={"tenant_id": "tenant_1", "greenhouse_id": "default"},
    )


class TestTimestampPersistenceBranch:
    def test_matching_timestamps_accepted_and_scored(
        self, test_client, test_sequences
    ):
        timestamps = [moment.isoformat() for moment in seeded_timestamps()]
        response = post_inference(test_client, test_sequences, timestamps)
        assert response.status_code == status.HTTP_200_OK
        scores = response.json()["anomaly_scores"]
        assert all(score == pytest.approx(0.15) for score in scores)

    def test_one_malformed_timestamp_is_skipped_not_fatal(
        self, test_client, test_sequences
    ):
        timestamps = [moment.isoformat() for moment in seeded_timestamps()]
        timestamps[-1] = "not-a-timestamp"
        response = post_inference(test_client, test_sequences, timestamps)
        assert response.status_code == status.HTTP_200_OK

    def test_all_malformed_timestamps_skip_persistence_gracefully(
        self, test_client, test_sequences
    ):
        timestamps = ["garbage"] * BATCH_SIZE
        response = post_inference(test_client, test_sequences, timestamps)
        assert response.status_code == status.HTTP_200_OK

    def test_commit_failure_returns_500_and_rolls_back(
        self, test_client, test_database, test_sequences
    ):
        rollback_mock = AsyncMock()
        with patch.object(
            test_database, "commit", AsyncMock(side_effect=RuntimeError("disk full"))
        ), patch.object(test_database, "rollback", rollback_mock):
            response = post_inference(test_client, test_sequences)
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "persist" in response.json()["message"]
        rollback_mock.assert_awaited_once()


class TestInferenceFailureModes:
    def test_inference_error_returns_400(
        self, patch_detector, test_client, test_sequences
    ):
        patch_detector.side_effect = RuntimeError("tensor shape mismatch")
        response = post_inference(test_client, test_sequences)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Inference failed" in response.json()["message"]

    def test_active_model_checkpoint_branch_is_used(
        self, test_client, test_sequences
    ):
        registry_instance = MagicMock()
        registry_instance.get_active_model = AsyncMock(
            return_value={"checkpoint_path": None, "model_id": "custom_v2"}
        )
        with patch(
            f"{ROUTE_PATCH_PREFIX}.ModelRegistry", return_value=registry_instance
        ):
            response = post_inference(test_client, test_sequences)
        assert response.status_code == status.HTTP_200_OK
        registry_instance.get_active_model.assert_awaited_once_with("tenant_1")


class TestCsvUploadErrorPaths:
    def test_non_csv_extension_rejected_with_400(self, test_client):
        response = post_csv(test_client, make_csv_bytes(25), filename="seq.txt")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "CSV" in response.json()["message"]

    def test_tempfile_write_failure_returns_500(self, test_client):
        with patch(
            f"{ROUTE_PATCH_PREFIX}.tempfile.NamedTemporaryFile",
            side_effect=OSError("disk full"),
        ):
            response = post_csv(test_client, make_csv_bytes(25))
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_detector_init_failure_returns_503(self, test_client):
        with patch(
            f"{ROUTE_PATCH_PREFIX}.get_cached_detector",
            side_effect=RuntimeError("corrupt checkpoint"),
        ):
            response = post_csv(test_client, make_csv_bytes(25))
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    def test_adapter_connect_failure_returns_422(self, test_client):
        with patch(
            f"{ROUTE_PATCH_PREFIX}.CSVAdapter.connect",
            side_effect=RuntimeError("unreadable file"),
        ):
            response = post_csv(test_client, make_csv_bytes(25))
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "Invalid CSV format" in response.json()["message"]

    def test_missing_columns_fail_validation_with_422(self, test_client):
        bad_csv = io.BytesIO(b"foo,bar\n1,2\n3,4\n")
        response = post_csv(test_client, bad_csv)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "validation failed" in response.json()["message"].lower()

    def test_chunk_inference_failure_returns_400(
        self, patch_detector, test_client
    ):
        patch_detector.side_effect = RuntimeError("NaN in batch")
        response = post_csv(test_client, make_csv_bytes(25))
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "chunk" in response.json()["message"]

    def test_csv_shorter_than_window_yields_zero_sequences(self, test_client):
        response = post_csv(test_client, make_csv_bytes(10))
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["sequences_processed"] == 0
        assert payload["embeddings"] == []
        assert payload["uncertainties"] == []

    def test_header_only_csv_returns_400_no_valid_data(self, test_client):
        header_only = io.BytesIO(b"timestamp,temperature,humidity,co2,light\n")
        response = post_csv(test_client, header_only)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "no valid data" in response.json()["message"].lower()

    def test_high_anomalies_reported_with_mapped_timestamps(
        self, patch_detector, test_client
    ):
        def _side_effect(seqs):
            n = len(seqs)
            scores = np.full(n, 0.2, dtype=np.float32)
            if n > 0:
                scores[-1] = HIGH_RAW_SCORE
            embeddings = np.zeros((n, 8), dtype=np.float32)
            uncertainties = np.full(n, 0.1, dtype=np.float32)
            return scores, embeddings, uncertainties

        patch_detector.side_effect = _side_effect
        response = post_csv(test_client, make_csv_bytes(25))
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        # 25 rows, seq_len 20 -> 6 sliding-window sequences, 1 high anomaly
        assert payload["sequences_processed"] == 6
        assert payload["anomalies_detected"] == 1
        assert len(payload["high_anomalies"]) == 1
        entry = payload["high_anomalies"][0]
        assert entry["sequence_index"] == 5
        assert entry["score"] == pytest.approx(0.95)
        assert entry["timestamp"].startswith("2026-06-06T11:")
