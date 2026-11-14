"""Tests for file-calibrated cold-run thresholds."""

import os

import numpy as np
import pytest

from src.models import cold_run_threshold
from src.models.cold_run_threshold import _calibrate_pot, calibrate_threshold


def test_percentile_threshold_flags_top_fraction(robust_gate_disabled):
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


@pytest.mark.skipif(
    "COLD_RUN_ROBUST_GATE_ENABLED" in os.environ,
    reason="the gate is explicitly configured for this run",
)
def test_robust_gate_is_off_by_default():
    """Enabling the gate is a per-deployment decision, not the default."""
    assert cold_run_threshold.COLD_RUN_ROBUST_GATE_ENABLED is False


# ---------------------------------------------------------------------------
# Absolute anomalousness gate (COLD_RUN_ROBUST_GATE_ENABLED)
# ---------------------------------------------------------------------------


@pytest.fixture
def robust_gate_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cold_run_threshold, "COLD_RUN_ROBUST_GATE_ENABLED", True
    )


@pytest.fixture
def robust_gate_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the relative-threshold behaviour regardless of deployment config."""
    monkeypatch.setattr(
        cold_run_threshold, "COLD_RUN_ROBUST_GATE_ENABLED", False
    )


def _clean_scores(size: int) -> np.ndarray:
    """Scores from a file with no genuine anomalies."""
    return np.random.default_rng(0).normal(0.05, 0.005, size)


def _scores_with_faults(size: int, faults: int) -> np.ndarray:
    """Clean scores with a known number of genuinely anomalous rows."""
    rng = np.random.default_rng(1)
    return np.concatenate(
        [
            rng.normal(0.05, 0.005, size - faults),
            rng.uniform(0.4, 0.9, faults),
        ]
    )


class TestPercentileScalesWithFileSize:
    """The defect the gate exists to correct."""

    @pytest.mark.parametrize("size", [10_000, 100_000])
    def test_clean_file_is_flagged_in_proportion_to_its_length(
        self, size, robust_gate_disabled
    ):
        scores = _clean_scores(size)
        threshold, _ = calibrate_threshold(scores)
        flagged = int((scores > threshold).sum())
        assert flagged == pytest.approx(size * 0.001, rel=0.1)


class TestRobustGate:
    def test_clean_file_yields_no_anomalies(self, robust_gate_enabled):
        scores = _clean_scores(100_000)
        threshold, method = calibrate_threshold(scores)
        assert method == "percentile+robust_gate"
        assert int((scores > threshold).sum()) == 0

    def test_count_tracks_faults_not_file_size(self, robust_gate_enabled):
        scores = _scores_with_faults(100_000, faults=40)
        threshold, _ = calibrate_threshold(scores)
        assert int((scores > threshold).sum()) == 40

    def test_gate_never_lowers_the_threshold(self, robust_gate_enabled):
        """A bimodal file whose percentile already sits above the gate."""
        scores = np.array([0.30] * 29 + [0.95])
        gated, _ = calibrate_threshold(scores)
        assert gated >= float(np.percentile(scores, 99.9))

    def test_degenerate_scale_falls_back_to_percentile(self, robust_gate_enabled):
        """Zero MAD gives no usable scale — report the relative method."""
        scores = np.full(50, 0.05)
        threshold, method = calibrate_threshold(scores)
        assert method == "percentile"
        assert threshold == pytest.approx(0.05)

    def test_empty_scores_are_unaffected(self, robust_gate_enabled):
        threshold, method = calibrate_threshold(np.array([]))
        assert method == "percentile"
        assert threshold == 1.0
