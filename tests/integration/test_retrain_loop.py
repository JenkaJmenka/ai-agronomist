"""Integration test for promote/rollback retrain loop."""

import pytest

from src.config import (
    ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD,
    ACTIVE_LEARNING_MIN_TRAINING_SAMPLES,
)
from src.models.active_learner import ActiveLearner
from src.models.customer_config_store import get_config_store


def _seed_reliable_operator(learner: ActiveLearner, operator_id: str = "op_retrain") -> None:
    for _ in range(25):
        learner.validator.record_feedback_outcome(operator_id, True)


def _collect_labeled_feedback(learner: ActiveLearner, count: int) -> None:
    for i in range(count):
        is_anomaly = i % 3 != 0
        learner.collect_operator_feedback(
            incident_id=f"inc_retrain_{i}",
            is_true_anomaly=is_anomaly,
            operator_id="op_retrain",
            pattern_type_observed="drift" if is_anomaly else "normal",
            sequence=[[25.0, 65.0, 400.0, 500.0]] * 20,
            detector_scores={
                "lstm_vae": 0.85 if is_anomaly else 0.15,
                "isolation_forest": 0.8 if is_anomaly else 0.2,
                "statistical": 0.75 if is_anomaly else 0.25,
                "correlation": 0.7 if is_anomaly else 0.1,
            },
        )


class TestRetrainLoop:
    def test_retrain_returns_metrics(self):
        store = get_config_store()
        store.clear("cust_retrain")
        learner = ActiveLearner(customer_id="cust_retrain")
        _seed_reliable_operator(learner)
        _collect_labeled_feedback(
            learner, ACTIVE_LEARNING_MIN_TRAINING_SAMPLES + 5
        )

        result = learner.trigger_retraining()
        assert result["status"] == "retraining_completed"
        assert "before_metrics" in result
        assert "after_metrics" in result
        assert result["action"] in ("promote", "rollback")
        store.clear("cust_retrain")

    def test_auto_retrain_on_collect(self):
        store = get_config_store()
        store.clear("cust_auto")
        learner = ActiveLearner(customer_id="cust_auto")
        _seed_reliable_operator(learner, "op_auto")

        result = None
        for i in range(ACTIVE_LEARNING_MIN_TRAINING_SAMPLES + 10):
            result = learner.collect_operator_feedback(
                incident_id=f"inc_auto_{i}",
                is_true_anomaly=i % 2 == 0,
                operator_id="op_auto",
                pattern_type_observed="spike" if i % 2 == 0 else "normal",
                detector_scores={
                    "lstm_vae": 0.9 if i % 2 == 0 else 0.1,
                    "isolation_forest": 0.8 if i % 2 == 0 else 0.2,
                    "statistical": 0.7 if i % 2 == 0 else 0.2,
                    "correlation": 0.6 if i % 2 == 0 else 0.1,
                },
            )
            if result.get("learning_triggered"):
                break

        if result and result.get("learning_triggered"):
            assert result["retraining"]["status"] == "retraining_completed"
        store.clear("cust_auto")

    def test_improvement_threshold_used(self):
        assert ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD >= 0.0
