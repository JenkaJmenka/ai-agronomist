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
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.config import MODEL_CONFIG
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
