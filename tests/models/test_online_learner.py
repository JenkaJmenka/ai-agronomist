"""Unit tests for OnlineLearner, OnlineLearningManager and DriftDetector (Gap 3)."""

from __future__ import annotations

import numpy as np
import pytest
import torch

from src.config import LATENT_DIM, MODEL_CONFIG
from src.models.drift_detector import DriftDetector
from src.models.lstm_vae import LSTMVAE
from src.models.online_learner import OnlineLearner, OnlineLearningManager

pytestmark = pytest.mark.unit

SEQ_LEN = MODEL_CONFIG["seq_len"]
INPUT_DIM = MODEL_CONFIG["input_dim"]

# Fast settings: update on every 5th observation over tiny replay batches.
FAST_LEARNER_KWARGS = {
    "update_interval": 5,
    "batch_size": 4,
    "buffer_capacity": 50,
}


def make_sequence(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random((SEQ_LEN, INPUT_DIM)).astype(np.float32)


@pytest.fixture
def learner(model: LSTMVAE) -> OnlineLearner:
    """Fast online learner around the shared conftest model fixture."""
    return OnlineLearner(model=model, tenant_id="tenant_a", **FAST_LEARNER_KWARGS)


def params_snapshot(model: torch.nn.Module) -> dict:
    return {name: p.detach().clone() for name, p in model.named_parameters()}


def models_equal(snapshot: dict, model: torch.nn.Module) -> bool:
    return all(
        torch.equal(snapshot[name], p.detach())
        for name, p in model.named_parameters()
    )


# ---------------------------------------------------------------------------
# OnlineLearner — observe / update cadence
# ---------------------------------------------------------------------------


class TestOnlineLearnerObserve:
    def test_observe_returns_positive_float_loss(self, learner: OnlineLearner) -> None:
        loss = learner.observe(make_sequence())
        assert isinstance(loss, float)
        assert loss > 0.0

    def test_observe_tracks_steps_buffer_and_losses(
        self, learner: OnlineLearner
    ) -> None:
        for i in range(3):
            learner.observe(make_sequence(i))
        assert learner.step_count == 3
        assert len(learner.buffer) == 3
        assert len(learner.losses) == 3

    def test_update_fires_every_update_interval(self, learner: OnlineLearner) -> None:
        for i in range(4):
            learner.observe(make_sequence(i))
        assert learner.update_count == 0
        learner.observe(make_sequence(4))
        assert learner.update_count == 1
        for i in range(5, 10):
            learner.observe(make_sequence(i))
        assert learner.update_count == 2

    def test_update_skipped_when_buffer_below_batch_size(
        self, model: LSTMVAE
    ) -> None:
        learner = OnlineLearner(
            model=model,
            tenant_id="tenant_a",
            update_interval=2,
            batch_size=10,
        )
        learner.observe(make_sequence(0))
        learner.observe(make_sequence(1))
        assert learner.update_count == 0

    def test_model_returned_to_eval_after_update(self, learner: OnlineLearner) -> None:
        for i in range(5):
            learner.observe(make_sequence(i))
        assert learner.update_count == 1
        assert learner.model.training is False

    def test_update_changes_model_weights(self, model: LSTMVAE) -> None:
        learner = OnlineLearner(
            model=model,
            tenant_id="tenant_a",
            learning_rate=1e-2,
            **FAST_LEARNER_KWARGS,
        )
        before = params_snapshot(learner.model)
        for i in range(5):
            learner.observe(make_sequence(i))
        assert learner.update_count == 1
        assert not models_equal(before, learner.model)

    def test_update_records_loss_history(self, learner: OnlineLearner) -> None:
        for i in range(10):
            learner.observe(make_sequence(i))
        assert len(learner.update_history["loss"]) == learner.update_count
        assert all(np.isfinite(v) for v in learner.update_history["loss"])

    def test_ewc_disabled_when_requested(self, model: LSTMVAE) -> None:
        learner = OnlineLearner(
            model=model, tenant_id="tenant_a", use_ewc=False, **FAST_LEARNER_KWARGS
        )
        assert learner.ewc is None
        # Updates still run without the EWC penalty term.
        for i in range(5):
            learner.observe(make_sequence(i))
        assert learner.update_count == 1

    def test_ewc_enabled_by_default(self, learner: OnlineLearner) -> None:
        assert learner.ewc is not None

    def test_periodic_update_logging_after_many_updates(
        self, model: LSTMVAE, caplog
    ) -> None:
        import logging

        learner = OnlineLearner(
            model=model,
            tenant_id="tenant_a",
            update_interval=1,
            batch_size=2,
            use_ewc=False,
        )
        with caplog.at_level(logging.INFO, logger="src.models.online_learner"):
            for i in range(12):
                learner.observe(make_sequence(i))
        assert learner.update_count >= 10
        assert any("Online update #10" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# OnlineLearner — predict / statistics / retraining heuristics
# ---------------------------------------------------------------------------


class TestOnlineLearnerPredict:
    def test_predict_returns_expected_keys_and_shapes(
        self, learner: OnlineLearner
    ) -> None:
        result = learner.predict(make_sequence())
        assert set(result.keys()) == {
            "anomaly_score",
            "embedding",
            "uncertainty",
            "trend",
            "updates_performed",
        }
        assert isinstance(result["anomaly_score"], float)
        assert result["anomaly_score"] > 0.0
        assert len(result["embedding"]) == LATENT_DIM
        assert result["uncertainty"] > 0.0
        assert result["updates_performed"] == 0

    def test_predict_trend_none_with_short_history(
        self, learner: OnlineLearner
    ) -> None:
        learner.observe(make_sequence())
        assert learner.predict(make_sequence())["trend"] is None

    def test_predict_trend_increasing_when_recent_losses_higher(
        self, learner: OnlineLearner
    ) -> None:
        learner.losses = [0.1] * 60 + [5.0] * 50
        assert learner.predict(make_sequence())["trend"] == "increasing"

    def test_predict_trend_stable_when_losses_flat(
        self, learner: OnlineLearner
    ) -> None:
        learner.losses = [1.0] * 120
        assert learner.predict(make_sequence())["trend"] == "stable"

    def test_get_statistics_before_any_observation(
        self, learner: OnlineLearner
    ) -> None:
        stats = learner.get_statistics()
        assert stats["tenant_id"] == "tenant_a"
        assert stats["sequences_observed"] == 0
        assert stats["updates_performed"] == 0
        assert stats["buffer_status"]["size"] == 0
        assert stats["loss_statistics"]["mean"] is None

    def test_get_statistics_after_observations(self, learner: OnlineLearner) -> None:
        for i in range(3):
            learner.observe(make_sequence(i))
        stats = learner.get_statistics()
        assert stats["sequences_observed"] == 3
        assert stats["buffer_status"]["size"] == 3
        assert stats["loss_statistics"]["min"] > 0.0
        assert stats["loss_statistics"]["max"] >= stats["loss_statistics"]["min"]

    def test_needs_retraining_false_with_short_history(
        self, learner: OnlineLearner
    ) -> None:
        learner.losses = [1.0] * 10
        assert learner.needs_retraining() is False

    def test_needs_retraining_true_after_loss_shift(
        self, learner: OnlineLearner
    ) -> None:
        learner.losses = [1.0] * 60 + [10.0] * 50
        assert learner.needs_retraining() is True

    def test_needs_retraining_false_when_stable(self, learner: OnlineLearner) -> None:
        learner.losses = [1.0] * 120
        assert learner.needs_retraining() is False

    def test_checkpoint_roundtrip_restores_weights(
        self, learner: OnlineLearner, tmp_path
    ) -> None:
        path = str(tmp_path / "online_model.pt")
        learner.save_checkpoint(path)
        saved = params_snapshot(learner.model)

        with torch.no_grad():
            for param in learner.model.parameters():
                param.add_(1.0)
        assert not models_equal(saved, learner.model)

        learner.load_checkpoint(path)
        assert models_equal(saved, learner.model)


# ---------------------------------------------------------------------------
# OnlineLearningManager — per-tenant isolation
# ---------------------------------------------------------------------------


class TestOnlineLearningManager:
    @pytest.fixture
    def manager(self, model: LSTMVAE) -> OnlineLearningManager:
        return OnlineLearningManager(base_model=model)

    def test_create_learner_registers_tenant(
        self, manager: OnlineLearningManager
    ) -> None:
        learner = manager.create_learner("tenant_a")
        assert manager.learners["tenant_a"] is learner
        assert learner.tenant_id == "tenant_a"

    def test_create_learner_deep_copies_base_model(
        self, manager: OnlineLearningManager
    ) -> None:
        learner = manager.create_learner("tenant_a")
        assert learner.model is not manager.base_model
        base_before = params_snapshot(manager.base_model)
        with torch.no_grad():
            for param in learner.model.parameters():
                param.add_(1.0)
        assert models_equal(base_before, manager.base_model)

    def test_tenants_do_not_share_weights(
        self, manager: OnlineLearningManager
    ) -> None:
        learner_a = manager.create_learner("tenant_a")
        learner_b = manager.create_learner("tenant_b")
        b_before = params_snapshot(learner_b.model)
        with torch.no_grad():
            for param in learner_a.model.parameters():
                param.add_(1.0)
        assert models_equal(b_before, learner_b.model)

    def test_observe_auto_creates_learner(
        self, manager: OnlineLearningManager
    ) -> None:
        assert "tenant_new" not in manager.learners
        loss = manager.observe("tenant_new", make_sequence())
        assert "tenant_new" in manager.learners
        assert loss > 0.0

    def test_predict_unknown_tenant_uses_base_model(
        self, manager: OnlineLearningManager
    ) -> None:
        result = manager.predict("ghost_tenant", make_sequence())
        assert result["learner_active"] is False
        assert "ghost_tenant" not in manager.learners
        assert result["anomaly_score"] > 0.0
        assert len(result["embedding"]) == LATENT_DIM
        assert result["uncertainty"] > 0.0

    def test_predict_known_tenant_uses_learner(
        self, manager: OnlineLearningManager
    ) -> None:
        manager.observe("tenant_a", make_sequence())
        result = manager.predict("tenant_a", make_sequence())
        assert result["learner_active"] is True
        assert result["updates_performed"] == 0
        assert len(result["embedding"]) == LATENT_DIM

    def test_get_tenant_statistics_no_learner_marker(
        self, manager: OnlineLearningManager
    ) -> None:
        assert manager.get_tenant_statistics("ghost") == {"status": "no_learner"}

    def test_get_tenant_statistics_for_existing_learner(
        self, manager: OnlineLearningManager
    ) -> None:
        manager.observe("tenant_a", make_sequence())
        stats = manager.get_tenant_statistics("tenant_a")
        assert stats["tenant_id"] == "tenant_a"
        assert stats["sequences_observed"] == 1


# ---------------------------------------------------------------------------
# DriftDetector
# ---------------------------------------------------------------------------


class TestDriftDetector:
    def test_insufficient_data_returns_warning(self) -> None:
        detector = DriftDetector(window_size=5, threshold=0.3)
        result = detector.update(1.0)
        assert result["drift_detected"] is False
        assert result["drift_score"] == 0.0
        assert "Insufficient data" in result["warning"]

    def test_warning_persists_until_history_exceeds_window(self) -> None:
        detector = DriftDetector(window_size=5, threshold=0.3)
        results = [detector.update(1.0) for _ in range(6)]
        assert all("warning" in r for r in results[:5])
        assert "warning" not in results[5]

    def test_stable_losses_do_not_trigger_drift(self) -> None:
        detector = DriftDetector(window_size=5, threshold=0.3)
        result = {}
        for _ in range(15):
            result = detector.update(1.0)
        assert result["drift_detected"] is False
        assert result["action"] == "continue_learning"
        assert result["drift_score"] == pytest.approx(0.0, abs=1e-3)
        assert result["recent_avg"] == pytest.approx(result["historical_avg"])

    def test_shifted_losses_trigger_retrain_action(self) -> None:
        detector = DriftDetector(window_size=5, threshold=0.3)
        for _ in range(5):
            detector.update(1.0)
        result = {}
        for _ in range(5):
            result = detector.update(5.0)
        assert result["drift_detected"] is True
        assert result["action"] == "retrain_model"
        assert result["drift_score"] > 0.3
        assert result["recent_avg"] > result["historical_avg"]
        assert result["recent_avg"] == pytest.approx(5.0)
        assert result["historical_avg"] == pytest.approx(1.0)

    def test_history_capped_at_twice_window_size(self) -> None:
        detector = DriftDetector(window_size=4, threshold=0.3)
        for i in range(20):
            detector.update(float(i))
        assert len(detector.losses) == 8

    def test_result_contains_all_documented_fields(self) -> None:
        detector = DriftDetector(window_size=3, threshold=0.3)
        result = {}
        for _ in range(7):
            result = detector.update(2.0)
        assert set(result.keys()) == {
            "drift_detected",
            "drift_score",
            "recent_avg",
            "historical_avg",
            "action",
        }
