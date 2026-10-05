"""API test configuration — shared fixtures for all tests/api/ tests.

The ``patch_noise_quiet`` autouse fixture patches ``NoiseDetector.detect``
to always return ``False`` so random synthetic test sequences (uniform [0,1])
do not trigger the precision-degradation pre-check.  Tests that specifically
exercise noise detection live in ``tests/models/test_noise_detector.py`` and
run against the real implementation without this patch.

The ``patch_model_load`` autouse fixture patches
``AnomalyDetector._load_model`` so tests never depend on checkpoint files
existing in the working tree.  The returned model has random weights — API
tests that exercise scoring always mock ``predict_batch_enriched`` anyway.
Tests that genuinely need a real model use the ``baseline_checkpoint``
fixture in ``tests/conftest.py``.

The ``db_session`` fixture yields a fresh in-memory SQLite session with the
full schema created.  It lives here rather than in a test module so sibling
modules that need it (e.g. the synthetic-marker contract files) get it through
pytest's own discovery instead of importing a fixture across test modules —
that import coupled collection order and broke silently on a rename.  Modules
with their own ``db_session`` definition keep it: a module-local fixture takes
precedence over the conftest one.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import MODEL_CONFIG
from src.db.models import Base
from src.models.lstm_vae import LSTMVAE


@pytest.fixture(autouse=True)
def patch_noise_quiet():
    """Silence the NoiseDetector for all api-layer tests.

    Synthetic random sequences used in pipeline integration tests contain
    high-frequency content that would always exceed the tight physical-unit
    thresholds (SENSOR_NOISE_TARGETS).  Patching here keeps the focus of api
    tests on pipeline correctness, not sensor precision degradation.
    """
    with patch(
        "src.api.services.comprehensive_pipeline.NoiseDetector.detect",
        return_value=False,
    ):
        yield


@pytest.fixture(autouse=True)
def patch_model_load():
    """Return a fresh random-weight LSTMVAE when AnomalyDetector loads a model.

    Production checkpoint files (models/checkpoints/*.pt, models/*_model.pt)
    are excluded from version control — they are training byproducts.  This
    fixture ensures API tests never depend on those files being present.
    """
    with patch(
        "src.models.inference.AnomalyDetector._load_model",
        return_value=LSTMVAE(**MODEL_CONFIG).eval(),
    ):
        yield


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Yield an AsyncSession bound to a per-test in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
