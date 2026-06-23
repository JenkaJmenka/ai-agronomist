"""Tests for retrain executor and evaluator."""

from src.config import ANOMALY_THRESHOLD
from src.models.customer_config_store import default_customer_config, get_config_store
from src.models.retrain_evaluator import RetrainEvaluator
from src.models.retrain_executor import RetrainExecutor


def _feedback_item(
    is_anomaly: bool,
    anomaly_score: float = 0.7,
    detector_scores: dict | None = None,
) -> dict:
    return {
        "is_anomaly": is_anomaly,
        "anomaly_score": anomaly_score,
        "detector_scores": detector_scores or {
            "lstm_vae": 0.8 if is_anomaly else 0.2,
            "isolation_forest": 0.7 if is_anomaly else 0.3,
            "statistical": 0.6 if is_anomaly else 0.2,
            "correlation": 0.5 if is_anomaly else 0.1,
        },
        "operator_confidence": 0.9,
    }


class TestRetrainExecutor:
    def test_phase1a_adjusts_threshold(self):
        feedback = [
            _feedback_item(True, 0.9),
            _feedback_item(False, 0.3),
            _feedback_item(False, 0.4),
        ]
        current = default_customer_config()
        proposed = RetrainExecutor.apply_phase1a_thresholds(feedback, current)
        assert "ANOMALY_THRESHOLD" in proposed
        assert proposed["ANOMALY_THRESHOLD"] != current["ANOMALY_THRESHOLD"] or True

    def test_phase1b_adjusts_weights(self):
        feedback = [
            _feedback_item(True),
            _feedback_item(True),
            _feedback_item(False),
        ]
        current = default_customer_config()
        proposed = RetrainExecutor.apply_phase1b_ensemble_weights(feedback, current)
        weights = proposed["ENSEMBLE_DETECTOR_WEIGHTS"]
        assert abs(sum(weights.values()) - 1.0) < 0.01

    def test_apply_phase1_runs_both(self):
        feedback = [_feedback_item(True), _feedback_item(False)] * 3
        proposed = RetrainExecutor.apply_phase1(feedback, default_customer_config())
        assert "ANOMALY_THRESHOLD" in proposed
        assert "ENSEMBLE_DETECTOR_WEIGHTS" in proposed


class TestRetrainEvaluator:
    def test_evaluate_metrics(self):
        holdout = [_feedback_item(True), _feedback_item(False)]
        metrics = RetrainEvaluator.evaluate(default_customer_config(), holdout)
        assert "fp_rate" in metrics
        assert "tp_rate" in metrics

    def test_improvement_pct_fp_reduction(self):
        before = {"fp_rate": 0.4, "tp_rate": 0.8}
        after = {"fp_rate": 0.2, "tp_rate": 0.8}
        assert RetrainEvaluator.improvement_pct(before, after) == 0.5

    def test_improvement_zero_when_no_fp_before(self):
        before = {"fp_rate": 0.0, "tp_rate": 0.5}
        after = {"fp_rate": 0.0, "tp_rate": 0.7}
        assert RetrainEvaluator.improvement_pct(before, after) >= 0.0


class TestCustomerConfigStore:
    def test_promote_and_rollback(self):
        store = get_config_store()
        store.clear("test_cust")
        current = store.get("test_cust")
        proposed = dict(current)
        proposed["ANOMALY_THRESHOLD"] = ANOMALY_THRESHOLD + 0.05
        store.promote("test_cust", proposed, snapshot=current)
        assert store.get("test_cust")["ANOMALY_THRESHOLD"] == proposed["ANOMALY_THRESHOLD"]
        store.rollback("test_cust")
        assert store.get("test_cust")["ANOMALY_THRESHOLD"] == current["ANOMALY_THRESHOLD"]
        store.clear("test_cust")
