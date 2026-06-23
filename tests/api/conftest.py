"""API test configuration — shared fixtures for all tests/api/ tests.

The ``patch_noise_quiet`` autouse fixture patches ``NoiseDetector.detect``
to always return ``False`` so random synthetic test sequences (uniform [0,1])
do not trigger the precision-degradation pre-check.  Tests that specifically
exercise noise detection live in ``tests/models/test_noise_detector.py`` and
run against the real implementation without this patch.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


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
