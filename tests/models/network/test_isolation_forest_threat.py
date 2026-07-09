"""Tests for IsolationForestThreatDetector using document Table 16 dataset."""

import numpy as np
import pytest

from src.models.network.isolation_forest_threat_detector import (
    IsolationForestThreatDetector,
)


class TestIsolationForestThreatDetector:
    """Document Table 16: Multi-dimensional behaviour anomaly."""

    def _make_baseline(self, n: int = 500, seed: int = 42) -> np.ndarray:
        """Generate normal-traffic baseline (19 features)."""
        rng = np.random.RandomState(seed)
        baseline = rng.normal(0, 1, (n, 19))
        baseline[:, 0] = 2.0 + rng.normal(0, 0.2, n)    # RTT ~2ms
        baseline[:, 1] = rng.exponential(0.1, n)          # Jitter ~0ms
        baseline[:, 2] = 0.99 + rng.normal(0, 0.01, n)   # CV_IAT ~1.0
        return baseline

    def test_normal_traffic(self):
        baseline = self._make_baseline()
        detector = IsolationForestThreatDetector(
            n_estimators=100, contamination=0.05, threshold=70.0,
        )
        detector.fit(baseline)

        normal_vector = baseline[0].copy()
        score, is_anomaly = detector.detect(normal_vector)
        assert score < 70.0
        assert is_anomaly is False

    def test_rogue_connection(self):
        baseline = self._make_baseline()
        detector = IsolationForestThreatDetector(
            n_estimators=100, contamination=0.05, threshold=70.0,
        )
        detector.fit(baseline)

        rogue = np.zeros(19)
        rogue[0] = 157.0    # RTT
        rogue[1] = 118.0    # Jitter
        rogue[2] = 4.95     # CV_IAT
        rogue[3:] = np.random.RandomState(99).normal(50, 20, 16)

        score, is_anomaly = detector.detect(rogue)
        assert score > 50.0, f"Threat score {score} should be high for rogue vector"

    def test_fit_and_predict(self):
        baseline = self._make_baseline(n=200)
        detector = IsolationForestThreatDetector(
            n_estimators=50, contamination=0.05, threshold=60.0,
        )
        detector.fit(baseline)

        normal = baseline[10].copy()
        score_normal, _ = detector.detect(normal)

        anomalous = np.full(19, 100.0)
        score_anom, _ = detector.detect(anomalous)

        assert score_anom > score_normal

    def test_not_fitted_raises(self):
        detector = IsolationForestThreatDetector()
        with pytest.raises(RuntimeError):
            detector.detect(np.zeros(19))

    def test_1d_input(self):
        baseline = self._make_baseline(n=100)
        detector = IsolationForestThreatDetector(n_estimators=50, contamination=0.05, threshold=70.0)
        detector.fit(baseline)
        score, _ = detector.detect(baseline[0])
        assert isinstance(score, float)

    def test_2d_input(self):
        baseline = self._make_baseline(n=100)
        detector = IsolationForestThreatDetector(n_estimators=50, contamination=0.05, threshold=70.0)
        detector.fit(baseline)
        score, _ = detector.detect(baseline[0:1])
        assert isinstance(score, float)
