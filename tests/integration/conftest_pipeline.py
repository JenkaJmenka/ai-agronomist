"""Shared fixtures for Phase 4.1 comprehensive pipeline integration tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, List
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
import pytest_asyncio
import torch
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.api.services.comprehensive_pipeline import build_default_baseline
from src.api.services.detector_cache import ResolvedDetector
from src.api.services.orchestrator_registry import clear_orchestrator_cache
from src.config import INPUT_DIM, LATENT_DIM, MODEL_CONFIG, RANDOM_SEED, SEQ_LEN
from src.db.models import Base
from src.db.session import get_db
from src.models.inference import AnomalyDetector
from src.models.lstm_vae import LSTMVAE
from src.monitoring.anomaly_metrics import reset_anomaly_metrics


def sample_sequence(seed: int = RANDOM_SEED) -> List[List[float]]:
    """Nominal mid-range readings with small jitter.

    Uniform-random normalized values denormalize to physically wild swings
    (e.g. light jumping across 0-1000 lux) and legitimately trip the
    pipeline's rule gates (spike/noise). These tests target the full
    LSTM/ensemble path, so the input must read as genuinely normal.
    """
    rng = np.random.default_rng(seed)
    mids = np.full((SEQ_LEN, INPUT_DIM), 0.5, dtype=np.float32)
    jitter = rng.normal(0.0, 0.002, (SEQ_LEN, INPUT_DIM)).astype(np.float32)
    return np.clip(mids + jitter, 0.0, 1.0).tolist()


def baseline_sequences(count: int = 10) -> List[List[List[float]]]:
    base = build_default_baseline(count)
    return base.tolist()


@pytest.fixture
def checkpoint_path(tmp_path) -> str:
    model = LSTMVAE(**MODEL_CONFIG)
    path = str(tmp_path / "pipeline_model.pt")
    torch.save(
        {"model_config": MODEL_CONFIG, "model_state_dict": model.state_dict()},
        path,
    )
    return path


@contextmanager
def patch_active_model(checkpoint_path: str) -> Generator[None, None, None]:
    """Bypass ModelRegistry/artifact-store resolution for tests that only
    care about pipeline behaviour, not resolution itself — loads the real
    checkpoint at *checkpoint_path* as if it were the tenant's active model.
    Resolution logic itself (registry lookup, db:// artifact resolution, the
    no-active-model error) is covered directly against a real ModelRegistry
    in TestResolveActiveModel (test_phase4_integration.py).
    """
    resolved = ResolvedDetector(
        detector=AnomalyDetector(checkpoint_path=checkpoint_path),
        checkpoint_path=checkpoint_path,
    )
    with patch(
        "src.api.routes.inference_integrated.resolve_tenant_detector",
        new=AsyncMock(return_value=resolved),
    ):
        yield


@pytest.fixture
def detector(checkpoint_path: str) -> Generator[AnomalyDetector, None, None]:
    instance = AnomalyDetector(checkpoint_path=checkpoint_path)
    yield instance


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def client(db_session: AsyncSession) -> Generator[TestClient, None, None]:
    async def override_get_db():
        yield db_session

    clear_orchestrator_cache()
    reset_anomaly_metrics()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    clear_orchestrator_cache()
    reset_anomaly_metrics()


@pytest.fixture
def comprehensive_payload() -> dict:
    return {
        "customer_id": "tenant_phase4",
        "greenhouse_id": "gh_1",
        "sequence": sample_sequence(),
        "baseline_sequences": baseline_sequences(),
    }


pipeline_db_session = db_session
pipeline_client = client


@pytest.fixture
def patch_anomaly_score():
    """Patch predict_batch_enriched to return a score above ANOMALY_THRESHOLD.

    Uses raw_score=2.0 → anomaly_score=clip(2.0/2.0)=1.0 which exceeds the
    ANOMALY_THRESHOLD of 0.8.  This guarantees is_anomaly=True so that
    incident_id is always stored — allowing tests that exercise the
    detect→feedback loop to obtain a valid incident_id.
    """

    def _enriched_side_effect(seqs: list) -> tuple:
        n = len(seqs)
        scores = np.full(n, 2.0, dtype=np.float32)
        embeddings = np.zeros((n, LATENT_DIM), dtype=np.float32)
        uncertainties = np.full(n, 0.1, dtype=np.float32)
        return scores, embeddings, uncertainties

    with patch(
        "src.models.inference.AnomalyDetector.predict_batch_enriched",
        side_effect=_enriched_side_effect,
    ) as mock_fn:
        yield mock_fn
