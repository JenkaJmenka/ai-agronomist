"""Cold-run upload inference integration tests."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.config import LATENT_DIM
from src.db.models import Base, RawReading, SensorReading
from src.db.session import get_db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse_connext.csv"


@pytest.fixture(autouse=True)
def _disable_live_runtime():
    """Prevent LiveRuntime from starting during TestClient lifespan.

    The LiveRuntime spawns supervised background tasks (merger, connectors)
    that connect to Redis.  Without a running Redis, shutdown waits for the
    exponential-backoff retry loop to exhaust — causing a 60 s+ teardown
    hang followed by a timeout ERROR.  Patching ``build_from_config`` to
    raise makes the lifespan's try/except set ``live_runtime = None``.
    """
    with patch(
        "src.live.runtime.LiveRuntime",
        side_effect=RuntimeError("disabled in test"),
    ):
        yield


def _make_enriched_side_effect(score_fn):
    def side_effect(seqs):
        scores = score_fn(seqs)
        batch_size = len(scores)
        embeddings = np.zeros((batch_size, LATENT_DIM), dtype=np.float32)
        uncertainties = np.full(batch_size, 0.1, dtype=np.float32)
        return scores, embeddings, uncertainties

    return side_effect


@pytest_asyncio.fixture
async def test_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
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


@pytest.fixture
def patch_detector():
    with patch("src.models.inference.AnomalyDetector.predict_batch_enriched") as mock_enriched:
        mock_enriched.side_effect = _make_enriched_side_effect(
            lambda seqs: np.linspace(0.05, 0.12, len(seqs), dtype=np.float32)
        )
        yield mock_enriched


def test_inference_upload_cold_run_greenhouse_fixture(test_client, patch_detector):
    assert FIXTURE.is_file()
    csv_bytes = FIXTURE.read_bytes()
    response = test_client.post(
        "/api/inference/upload",
        files={"file": ("greenhouse_connext.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_1", "greenhouse_id": "gh_A"},
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["sequences_processed"] == 131
    assert data["threshold_used"] < 0.8
    assert data["threshold_method"] in ("pot", "percentile")
    assert "co2" in data["imputed_channels"]
    assert data["anomalies_detected"] > 0
    assert data["anomalies_detected"] < data["sequences_processed"] * 0.2
    assert len(data["anomaly_scores"]) == data["sequences_processed"]
    assert data["statistical_summary"] is not None
    stat_cols = data["statistical_summary"]["baseline_columns"]
    assert "outdoor_temp" in stat_cols
    # Connext CSV "Measured Greenhouse Temp" is consumed as canonical
    # "temperature" via _RAW_COLUMN_ALIASES in csv_adapter.py — it does NOT
    # appear as a separate stat column.  The canonical name must be present.
    assert "temperature" in stat_cols
    assert data["data_persisted"] is False
    assert data["rows_uploaded"] == 0


def test_inference_upload_response_schema_fields(test_client, patch_detector):
    csv_content = (
        "timestamp,temperature,humidity,co2,light\n"
        + "\n".join(
            [f"2026-06-09T11:{i//60:02d}:{i%60:02d}Z,{20+i*0.1},{60+i*0.2},400,500" for i in range(50)]
        )
    )
    response = test_client.post(
        "/api/inference/upload",
        files={"file": ("test.csv", csv_content.encode(), "text/csv")},
        data={"tenant_id": "tenant_1", "greenhouse_id": "default"},
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    for key in (
        "threshold_used",
        "threshold_method",
        "imputed_channels",
        "statistical_summary",
        "embedding_dim",
        "data_persisted",
        "rows_uploaded",
    ):
        assert key in data


def test_inference_upload_persist_data(test_client, patch_detector, test_database):
    csv_content = (
        "timestamp,temperature,humidity,co2,light\n"
        + "\n".join(
            [f"2026-06-09T11:{i//60:02d}:{i%60:02d}Z,{20+i*0.1},{60+i*0.2},400,500" for i in range(50)]
        )
    )
    response = test_client.post(
        "/api/inference/upload",
        files={"file": ("test.csv", csv_content.encode(), "text/csv")},
        data={
            "tenant_id": "tenant_persist",
            "greenhouse_id": "gh_persist",
            "persist_data": "true",
        },
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["data_persisted"] is True
    assert data["rows_uploaded"] == 50

    async def _assert_rows():
        sensor_count = await test_database.scalar(
            select(func.count())
            .select_from(SensorReading)
            .where(
                SensorReading.tenant_id == "tenant_persist",
                SensorReading.greenhouse_id == "gh_persist",
            )
        )
        raw_count = await test_database.scalar(
            select(func.count())
            .select_from(RawReading)
            .where(
                RawReading.tenant_id == "tenant_persist",
                RawReading.greenhouse_id == "gh_persist",
            )
        )
        assert sensor_count == 50
        assert raw_count == 50

    import asyncio

    asyncio.get_event_loop().run_until_complete(_assert_rows())

    response2 = test_client.post(
        "/api/inference/upload",
        files={"file": ("test.csv", csv_content.encode(), "text/csv")},
        data={
            "tenant_id": "tenant_persist",
            "greenhouse_id": "gh_persist",
            "persist_data": "true",
        },
    )
    assert response2.status_code == status.HTTP_200_OK
    data2 = response2.json()
    assert data2["data_persisted"] is True
    assert data2["sensor_rows_uploaded"] == 50
    assert data2["rows_uploaded"] == 50

    async def _assert_no_duplicates():
        sensor_count = await test_database.scalar(
            select(func.count())
            .select_from(SensorReading)
            .where(
                SensorReading.tenant_id == "tenant_persist",
                SensorReading.greenhouse_id == "gh_persist",
            )
        )
        assert sensor_count == 50

    asyncio.get_event_loop().run_until_complete(_assert_no_duplicates())
