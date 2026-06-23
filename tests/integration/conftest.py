"""Integration test fixtures — re-exports pipeline fixtures for this directory."""

from unittest.mock import patch

import pytest

from tests.integration.conftest_pipeline import (  # noqa: F401
    baseline_sequences,
    checkpoint_path,
    client,
    comprehensive_payload,
    db_session,
    detector,
    patch_anomaly_score,
    pipeline_client,
    pipeline_db_session,
    sample_sequence,
)


@pytest.fixture(autouse=True)
def patch_noise_quiet():
    """Silence the NoiseDetector for all integration tests.

    Synthetic sequences used here contain high-frequency content that always
    exceeds the tight physical-unit SENSOR_NOISE_TARGETS thresholds.  Noise
    detection correctness is covered in tests/models/test_noise_detector.py.
    """
    with patch(
        "src.api.services.comprehensive_pipeline.NoiseDetector.detect",
        return_value=False,
    ):
        yield
