"""Stuck-fault detector tests for imputed channels and smooth data."""

import numpy as np

from src.models.stuck_fault_detector import StuckFaultDetector


def test_smooth_varying_sequence_not_stuck():
    rng = np.random.default_rng(0)
    seq = np.stack(
        [
            0.5 + rng.normal(0, 0.02, 20),
            0.6 + rng.normal(0, 0.02, 20),
            np.full(20, 0.5),
            0.4 + rng.normal(0, 0.02, 20),
        ],
        axis=1,
    )
    detector = StuckFaultDetector()
    result = detector.detect(seq, skip_channels={"co2"})
    assert result["temperature"] is False
    assert result["humidity"] is False
    assert result["co2"] is False


def test_imputed_co2_flat_is_skipped():
    seq = np.stack(
        [
            np.linspace(0.4, 0.6, 20),
            np.linspace(0.5, 0.7, 20),
            np.full(20, 0.5),
            np.linspace(0.3, 0.5, 20),
        ],
        axis=1,
    )
    detector = StuckFaultDetector()
    without_skip = detector.detect(seq)
    with_skip = detector.detect(seq, skip_channels={"co2"})
    assert without_skip["co2"] is True
    assert with_skip["co2"] is False
