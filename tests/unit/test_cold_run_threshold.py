"""Tests for file-calibrated cold-run thresholds."""

import numpy as np

from src.models.cold_run_threshold import _calibrate_pot, calibrate_threshold


def test_percentile_threshold_flags_top_fraction():
    scores = np.linspace(0.01, 0.10, 1000)
    threshold, method = calibrate_threshold(scores, method="percentile")
    assert method == "percentile"
    assert threshold >= 0.099
    assert int((scores > threshold).sum()) <= 2


def test_pot_never_below_percentile_floor():
    scores = np.linspace(0.01, 0.10, 1000)
    pot_threshold, _ = _calibrate_pot(scores, float(np.percentile(scores, 99.9)))
    assert pot_threshold >= float(np.percentile(scores, 99.9))


def test_pot_falls_back_when_flat():
    scores = np.full(50, 0.05)
    threshold, method = calibrate_threshold(scores, method="pot")
    assert method == "percentile"
    assert threshold >= 0.05
