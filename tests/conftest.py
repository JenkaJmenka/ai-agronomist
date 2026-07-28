"""Shared pytest fixtures for model and pipeline testing."""

import logging
import os

# Limit OpenMP threads to avoid pytest shutdown deadlock from leaked torch threads.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

# Ensure test DB is used globally before any app imports
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_global.db"

# Disable LiveRuntime mock SCADA sources and live-to-DB persistence in tests.
# The supervised mock connector tasks attempt to publish to Redis, which is
# unavailable in test environments. When the TestClient shuts down, the
# merger's CancelledError handler tries to flush remaining buckets to Redis,
# fails with ConnectionError, and the supervisor restarts the task instead
# of terminating it — causing asyncio.gather() in LiveRuntime.stop() to hang
# forever, which blocks TestClient.__exit__(). Tests that exercise the live
# pipeline use monkeypatch to re-enable these per-test.
os.environ.setdefault("MOCK_SCADA_ENABLED", "false")
os.environ.setdefault("LIVE_PERSIST_ENABLED", "false")

import pytest
import torch
from src.models.lstm_vae import LSTMVAE
from src.config import MODEL_CONFIG


@pytest.fixture(autouse=True)
def _restore_root_logger_handlers():
    """Snapshot the root logger's handler list and level before each test,
    then restore them afterwards.

    This prevents any import-time or in-test logging reconfiguration
    (e.g. ``setup_logging()`` calling ``dictConfig``) from leaking across
    tests and silently removing pytest's ``LogCaptureHandler``.

    The fixture restores the *list contents* only — it does not close
    handlers it did not create, so pytest's own caplog handler management
    is unaffected.
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers = list(original_handlers)
    root.setLevel(original_level)


@pytest.fixture
def device() -> str:
    """Returns the primary target device for model testing.
    
    Using CPU ensures consistent results across different CI environments.
    """
    return "cpu"


@pytest.fixture
def model(device: str) -> LSTMVAE:
    """Returns a newly initialized instance of LSTMVAE mapped to the test device.
    
    This fixture ensures clean, isolated state for every test case.
    """
    test_model = LSTMVAE(**MODEL_CONFIG)
    return test_model.to(device)


@pytest.fixture
def sample_input() -> torch.Tensor:
    """Returns a random synthetic tensor representing a batch of greenhouse readings.

    Shape: (batch_size=8, seq_len, input_dim).
    """
    batch_size = 8
    seq_len = MODEL_CONFIG["seq_len"]
    input_dim = MODEL_CONFIG["input_dim"]
    return torch.randn(batch_size, seq_len, input_dim)


@pytest.fixture
def baseline_checkpoint(tmp_path: "pathlib.Path") -> str:
    """Create a minimal LSTM-VAE checkpoint in tmp_path for tests that need a
    baseline model.

    The model is freshly initialised (random weights) and saved through the
    standard ``save_checkpoint`` API so the file contains all metadata fields
    (model_state_dict, epoch, loss, accuracy, timestamp, model_config) that
    downstream code expects.

    Returns the path to the saved ``.pt`` file.
    """
    import pathlib  # noqa: F811 — guarded above for type hint

    mdl = LSTMVAE(**MODEL_CONFIG)
    ckpt_path = str(tmp_path / "baseline_test.pt")
    mdl.save_checkpoint(ckpt_path, epoch=0, loss=0.0, accuracy=0.0)
    return ckpt_path
