"""Tests for Gap 4 ensemble anomaly detector."""

import time

import numpy as np
import pytest
import torch

from src.config import INPUT_DIM, MODEL_CONFIG, RANDOM_SEED, SEQ_LEN
from src.models.ensemble_detector import (
    CorrelationDetector,
    EnsembleAnomalyDetector,
    IsolationForestDetector,
    LstmVaeDetector,
    StatisticalDetector,
)
from src.models.inference import AnomalyDetector
from src.models.lstm_vae import LSTMVAE


@pytest.fixture
def checkpoint_path(tmp_path) -> str:
    model = LSTMVAE(**MODEL_CONFIG)
    path = str(tmp_path / "ensemble_model.pt")
    torch.save(
        {"model_config": MODEL_CONFIG, "model_state_dict": model.state_dict()},
        path,
    )
    return path


@pytest.fixture
def detector(checkpoint_path) -> AnomalyDetector:
    instance = AnomalyDetector(checkpoint_path=checkpoint_path)
    yield instance


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(RANDOM_SEED)


@pytest.fixture
def baseline(rng) -> np.ndarray:
    mids = np.array([25.0, 65.0, 400.0, 500.0], dtype=np.float32)
    template = np.tile(mids, (SEQ_LEN, 1))
    return np.stack([template + rng.normal(0, 0.5, template.shape) for _ in range(10)])


@pytest.fixture
def normal_sequence(rng) -> np.ndarray:
    mids = np.array([25.0, 65.0, 400.0, 500.0], dtype=np.float32)
    return np.tile(mids, (SEQ_LEN, 1)) + rng.normal(0, 0.3, (SEQ_LEN, INPUT_DIM))


@pytest.fixture
def anomaly_sequence() -> np.ndarray:
    seq = np.tile(np.array([25.0, 65.0, 400.0, 500.0]), (SEQ_LEN, 1)).astype(
        np.float32
    )
    seq[-5:, 0] = 45.0
    seq[-5:, 2] = 900.0
    return seq


class TestLstmVaeDetector:
    def test_score_in_unit_interval(self, detector, normal_sequence, baseline):
        d = LstmVaeDetector(detector)
        score = d.score(normal_sequence, baseline)
        assert 0.0 <= score <= 1.0

    def test_is_anomaly_at_half(self, detector):
        d = LstmVaeDetector(detector)
        assert d.is_anomaly(0.5) is True
        assert d.is_anomaly(0.49) is False


class TestIsolationForestDetector:
    def test_fit_and_score(self, normal_sequence, baseline):
        d = IsolationForestDetector()
        score = d.score(normal_sequence, baseline)
        assert 0.0 <= score <= 1.0

    def test_anomaly_scores_valid(self, normal_sequence, anomaly_sequence, baseline):
        d = IsolationForestDetector()
        normal = d.score(normal_sequence, baseline)
        anomaly = d.score(anomaly_sequence, baseline)
        assert 0.0 <= normal <= 1.0
        assert 0.0 <= anomaly <= 1.0


class TestStatisticalDetector:
    def test_score_range(self, normal_sequence, baseline):
        d = StatisticalDetector()
        score = d.score(normal_sequence, baseline)
        assert 0.0 <= score <= 1.0

    def test_anomaly_higher_than_normal(
        self, normal_sequence, anomaly_sequence, baseline
    ):
        d = StatisticalDetector()
        assert d.score(anomaly_sequence, baseline) >= d.score(
            normal_sequence, baseline
        )


class TestCorrelationDetector:
    def test_range_violation_detected(self, anomaly_sequence, baseline):
        d = CorrelationDetector()
        score = d.score(anomaly_sequence, baseline)
        assert score > 0.0

    def test_normal_low_score(self, normal_sequence, baseline):
        d = CorrelationDetector()
        assert d.score(normal_sequence, baseline) < 0.5


class TestEnsembleAnomalyDetector:
    def test_returns_all_detector_scores(
        self, detector, normal_sequence, baseline
    ):
        ensemble = EnsembleAnomalyDetector(LstmVaeDetector(detector))
        result = ensemble.detect(normal_sequence, baseline)
        assert set(result.detector_scores.keys()) == {
            "lstm_vae",
            "isolation_forest",
            "statistical",
            "correlation",
        }

    def test_agreement_count_bounds(self, detector, normal_sequence, baseline):
        result = EnsembleAnomalyDetector(LstmVaeDetector(detector)).detect(
            normal_sequence, baseline
        )
        assert 0 <= result.agreement_count <= 4

    def test_ensemble_score_weighted(
        self, detector, normal_sequence, baseline
    ):
        result = EnsembleAnomalyDetector(LstmVaeDetector(detector)).detect(
            normal_sequence, baseline
        )
        assert 0.0 <= result.ensemble_score <= 1.0

    def test_anomaly_flag_on_extreme_sequence(
        self, detector, anomaly_sequence, baseline
    ):
        result = EnsembleAnomalyDetector(LstmVaeDetector(detector)).detect(
            anomaly_sequence, baseline
        )
        assert result.agreement_count >= 1

    def test_baseline_cache_refit(self, detector, normal_sequence, baseline, rng):
        ensemble = EnsembleAnomalyDetector(LstmVaeDetector(detector))
        ensemble.detect(normal_sequence, baseline)
        new_baseline = baseline + rng.normal(0, 2.0, baseline.shape)
        result = ensemble.detect(normal_sequence, new_baseline.astype(np.float32))
        assert result.detector_scores

    def test_latency_smoke(self, detector, normal_sequence, baseline):
        ensemble = EnsembleAnomalyDetector(LstmVaeDetector(detector))
        ensemble.fit_baseline(baseline)
        start = time.perf_counter()
        for _ in range(5):
            ensemble.detect(normal_sequence, baseline)
        elapsed_ms = (time.perf_counter() - start) / 5 * 1000
        assert elapsed_ms < 500


class TestEnsembleEdgeCases:
    def test_single_baseline_sequence(self, detector, normal_sequence, rng):
        baseline = np.stack([normal_sequence.copy()])
        result = EnsembleAnomalyDetector(LstmVaeDetector(detector)).detect(
            normal_sequence, baseline
        )
        assert result.detector_scores

    def test_equal_weights_sum(self, detector, normal_sequence, baseline):
        weights = {
            "lstm_vae": 0.25,
            "isolation_forest": 0.25,
            "statistical": 0.25,
            "correlation": 0.25,
        }
        ensemble = EnsembleAnomalyDetector(
            LstmVaeDetector(detector), weights=weights
        )
        result = ensemble.detect(normal_sequence, baseline)
        manual = sum(
            weights[k] * result.detector_scores[k] for k in weights
        )
        assert abs(result.ensemble_score - manual) < 0.01

    def test_min_agreement_blocks_weak_vote(
        self, detector, normal_sequence, baseline
    ):
        ensemble = EnsembleAnomalyDetector(
            LstmVaeDetector(detector),
            anomaly_threshold=0.01,
            min_agreement=4,
        )
        result = ensemble.detect(normal_sequence, baseline)
        if result.agreement_count < 4:
            assert result.is_anomaly is False

    def test_detector_names_stable(self, detector):
        d = LstmVaeDetector(detector)
        assert d.name == "lstm_vae"

    def test_isolation_forest_is_anomaly_threshold(self):
        d = IsolationForestDetector(threshold=0.6)
        assert d.is_anomaly(0.7) is True
        assert d.is_anomaly(0.4) is False

    def test_correlation_is_anomaly_threshold(self):
        d = CorrelationDetector(threshold=0.3)
        assert d.is_anomaly(0.35) is True

    def test_statistical_is_anomaly(self):
        d = StatisticalDetector()
        assert d.is_anomaly(0.6) is True

    def test_ensemble_result_frozen(self, detector, normal_sequence, baseline):
        result = EnsembleAnomalyDetector(LstmVaeDetector(detector)).detect(
            normal_sequence, baseline
        )
        with pytest.raises(Exception):
            result.ensemble_score = 0.99  # type: ignore[misc]
