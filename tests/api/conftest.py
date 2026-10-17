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

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import MODEL_CONFIG
from src.db.models import Base
from src.models.inference import AnomalyDetector
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


# A path that is never opened: _load_model is patched out below. It exists only
# so AnomalyDetector's required-checkpoint guard is satisfied for the many api
# tests that exercise pipeline behaviour through routes which still pass
# checkpoint_path=None for a tenant with no active model.
_TEST_STUB_CHECKPOINT = "models/__api_test_stub__.pt"

_REAL_DETECTOR_INIT = AnomalyDetector.__init__


def _init_with_stub_checkpoint(self, checkpoint_path=None):  # noqa: ANN001, ANN202
    _REAL_DETECTOR_INIT(self, checkpoint_path or _TEST_STUB_CHECKPOINT)


@pytest.fixture(autouse=True)
def patch_model_load():
    """Return a fresh random-weight LSTMVAE when AnomalyDetector loads a model.

    Production checkpoint files (models/checkpoints/*.pt, models/*_model.pt)
    are excluded from version control — they are training byproducts.  This
    fixture ensures API tests never depend on those files being present.

    It ALSO substitutes a stub path for a ``None`` checkpoint. Read this before
    trusting it: this autouse fixture is the reason a production outage was
    invisible to the whole suite. ``AnomalyDetector(None)`` used to resolve to
    ``models/baseline.pt`` — a file excluded from the image and present in no
    environment — and patching ``_load_model`` meant the path was never opened,
    so every api test passed while production returned 503 and the live feed
    scored nothing.

    ``AnomalyDetector`` now rejects a ``None`` checkpoint outright
    (``MissingCheckpointError``), which is the real behaviour. Routes that still
    pass ``None`` for a tenant with no active model therefore fail, by design —
    tracked as BACKLOG-13. The tests covered by this fixture are about pipeline
    behaviour, not model provisioning, so they get a stub rather than 30
    rewrites. The missing-model path has its own explicit coverage in
    ``tests/models/test_missing_checkpoint_contract.py``, which lives outside
    ``tests/api/`` precisely so this fixture does not apply to it.
    """
    with (
        patch(
            "src.models.inference.AnomalyDetector._load_model",
            return_value=LSTMVAE(**MODEL_CONFIG).eval(),
        ),
        patch(
            "src.models.inference.AnomalyDetector.__init__",
            _init_with_stub_checkpoint,
        ),
    ):
        yield


@pytest.fixture
def tenant_has_active_model():
    """Make ``ModelRegistry.get_active_model`` report a deployed model.

    Opt-in (not autouse): several upload tests use a 48-row CSV fixture, which
    is below the 100 clean rows cold-start training needs, so they cannot train
    a model from their own upload. They used to resolve
    ``AnomalyDetector(None)`` -> ``models/baseline.pt`` and only passed because
    ``patch_model_load`` above stopped anything reading that file.

    Those tests are about the upload pipeline, not model provisioning, so they
    get a tenant that already has a model — the realistic "returning tenant
    uploads more data" path. Tests that DO cover provisioning must not use this.
    """
    stub = {
        "model_id": "model_stub",
        "customer_id": "stub_tenant",
        "model_type": "stub",
        "checkpoint_path": _TEST_STUB_CHECKPOINT,
        "metrics": {},
        "created_at": None,
    }
    with patch(
        "src.models.model_registry.ModelRegistry.get_active_model",
        new=AsyncMock(return_value=stub),
    ):
        yield stub


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
