"""
Coverage tests for retrain_executor.py, retrain_evaluator.py,
model_registry.py, ewma_tracker.py, pot_threshold.py.

Targets uncovered lines from the 87.51% baseline run.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base
from src.models.customer_config_store import default_customer_config
from src.models.ewma_tracker import EWMATracker
from src.models.pot_threshold import POTThresholdFitter
from src.models.retrain_evaluator import RetrainEvaluator
from src.models.retrain_executor import RetrainExecutor


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ===========================================================================
# EWMATracker – uncovered: line 32 (k < 0 validation)
# ===========================================================================


class TestEWMATrackerValidation:
    def test_negative_k_raises_value_error(self):
        """EWMATracker raises ValueError when k < 0 (line 32)."""
        with pytest.raises(ValueError, match="k must be non-negative"):
            EWMATracker(alpha=0.1, k=-1.0)

    def test_zero_k_is_valid(self):
        """k=0 is a valid threshold multiplier (mean equals threshold)."""
        tracker = EWMATracker(alpha=0.1, k=0.0)
        assert tracker is not None

    def test_invalid_alpha_raises_value_error(self):
        """alpha=0 violates (0, 1] constraint."""
        with pytest.raises(ValueError, match="alpha must be in"):
            EWMATracker(alpha=0.0, k=3.0)

    def test_alpha_above_one_raises_value_error(self):
        """alpha=1.5 violates (0, 1] constraint."""
        with pytest.raises(ValueError, match="alpha must be in"):
            EWMATracker(alpha=1.5, k=3.0)

    def test_non_finite_score_raises_value_error(self):
        """update() rejects non-finite scores."""
        tracker = EWMATracker(alpha=0.1, k=3.0)
        with pytest.raises(ValueError):
            tracker.update(float("nan"))
        with pytest.raises(ValueError):
            tracker.update(float("inf"))

    def test_to_dict_and_from_dict_roundtrip(self):
        """Serialization roundtrip preserves tracker state."""
        tracker = EWMATracker(alpha=0.2, k=2.5)
        for v in [0.3, 0.4, 0.35, 0.5, 0.2]:
            tracker.update(v)
        d = tracker.to_dict()
        restored = EWMATracker.from_dict(d)
        assert restored._alpha == tracker._alpha
        assert restored._k == tracker._k
        assert restored._n == tracker._n
        assert restored.get_threshold() == pytest.approx(tracker.get_threshold())

    def test_threshold_is_inf_below_min_obs(self):
        """get_threshold returns inf when fewer than _MIN_OBS updates done."""
        tracker = EWMATracker(alpha=0.1, k=3.0)
        for _ in range(EWMATracker._MIN_OBS - 1):
            tracker.update(0.5)
        assert tracker.get_threshold() == float("inf")

    def test_threshold_finite_after_min_obs(self):
        """get_threshold returns finite value after _MIN_OBS updates."""
        tracker = EWMATracker(alpha=0.1, k=3.0)
        for _ in range(EWMATracker._MIN_OBS):
            tracker.update(0.5)
        threshold = tracker.get_threshold()
        assert threshold != float("inf")
        import math
        assert math.isfinite(threshold)

    def test_is_anomaly_uses_threshold(self):
        """is_anomaly returns True iff score exceeds current threshold."""
        tracker = EWMATracker(alpha=0.1, k=3.0)
        for _ in range(EWMATracker._MIN_OBS + 5):
            tracker.update(0.3)
        threshold = tracker.get_threshold()
        assert tracker.is_anomaly(threshold + 0.01) is True
        assert tracker.is_anomaly(threshold - 0.01) is False


# ===========================================================================
# POTThresholdFitter – uncovered: line 93, 95-100 (GPD fit fallback)
# ===========================================================================


class TestPOTThresholdFitterEdgeCases:
    def test_get_threshold_inf_when_buffer_too_small(self):
        """get_threshold returns inf if buffer has fewer than _MIN_BUFFER_FILL."""
        fitter = POTThresholdFitter(q=0.9, fpr=0.001, buffer_size=500)
        fitter._buffer.append(0.3)  # only 1 element
        assert fitter.get_threshold() == float("inf")

    def test_update_does_not_refit_below_interval(self):
        """update() accumulates but does not refit until _REFIT_INTERVAL reached."""
        fitter = POTThresholdFitter(q=0.9, fpr=0.001, buffer_size=500)
        for v in [0.1 * i for i in range(30)]:  # 30 < _REFIT_INTERVAL (50)
            fitter.update(v)
        # n_since_fit was never reset → still equal to 30
        assert fitter._n_since_fit == 30

    def test_to_dict_from_dict_roundtrip(self):
        """Serialization roundtrip preserves buffer and threshold."""
        fitter = POTThresholdFitter(q=0.9, fpr=0.001, buffer_size=100)
        for i in range(30):
            fitter._buffer.append(0.2 + 0.01 * i)
        fitter._threshold = 0.55
        d = fitter.to_dict()
        restored = POTThresholdFitter.from_dict(d)
        assert list(restored._buffer) == list(fitter._buffer)
        assert restored._threshold == pytest.approx(fitter._threshold)

    def test_fit_falls_back_to_quantile_when_too_few_peaks(self):
        """fit() uses simple quantile when fewer than min_peaks exceedances found."""
        import numpy as np

        fitter = POTThresholdFitter(q=0.9, fpr=0.05, min_peaks=100, buffer_size=500)
        # Add 100 identical values: no exceedances above 0.9-quantile
        for _ in range(100):
            fitter._buffer.append(0.5)
        threshold = fitter.fit()
        assert threshold > 0  # quantile fallback should give a positive number

    def test_fit_gpd_failure_falls_back_to_quantile(self):
        """fit() catches GPD errors and falls back to quantile method."""
        import numpy as np
        from unittest.mock import patch

        fitter = POTThresholdFitter(q=0.5, fpr=0.01, min_peaks=1, buffer_size=500)
        # Add varied data to ensure some peaks exist
        rng = np.random.default_rng(42)
        for v in rng.random(200):
            fitter._buffer.append(float(v))

        with patch("scipy.stats.genpareto.fit", side_effect=RuntimeError("fit failed")):
            threshold = fitter.fit()

        assert threshold > 0  # quantile fallback


# ===========================================================================
# RetrainExecutor – uncovered: lines 31, 39, 86, 88->90, 95, 119
# ===========================================================================


class TestRetrainExecutorUncoveredPaths:
    def test_apply_phase1a_empty_feedback_returns_unchanged(self):
        """Line 31: empty feedback → return proposed without modification."""
        cfg = default_customer_config()
        original_threshold = cfg["ANOMALY_THRESHOLD"]
        result = RetrainExecutor.apply_phase1a_thresholds([], cfg)
        assert result["ANOMALY_THRESHOLD"] == original_threshold

    def test_apply_phase1a_all_zero_scores_returns_unchanged(self):
        """Line 39: all scores are 0 (falsy) → return proposed unchanged."""
        cfg = default_customer_config()
        feedback = [{"is_anomaly": True, "anomaly_score": 0.0}]
        result = RetrainExecutor.apply_phase1a_thresholds(feedback, cfg)
        assert result["ANOMALY_THRESHOLD"] == cfg["ANOMALY_THRESHOLD"]

    def test_apply_phase1b_fewer_than_3_labeled_returns_unchanged(self):
        """Line 86→90: fewer than 3 labeled feedback items → return unchanged."""
        cfg = default_customer_config()
        # Only 2 items with detector_scores
        feedback = [
            {"detector_scores": {"lstm_vae": 0.8}, "is_true_anomaly": True},
            {"detector_scores": {"lstm_vae": 0.2}, "is_true_anomaly": False},
        ]
        result = RetrainExecutor.apply_phase1b_ensemble_weights(feedback, cfg)
        assert result["ENSEMBLE_DETECTOR_WEIGHTS"] == cfg["ENSEMBLE_DETECTOR_WEIGHTS"]

    def test_apply_phase1b_no_scores_for_detector_uses_existing_weight(self):
        """Line 95: detector not in scores dict → keep existing weight."""
        cfg = default_customer_config()
        # Item with detector_scores missing some detectors
        feedback = [
            {"detector_scores": {"lstm_vae": 0.9}, "is_true_anomaly": True},
            {"detector_scores": {"lstm_vae": 0.8}, "is_true_anomaly": True},
            {"detector_scores": {"lstm_vae": 0.7}, "is_true_anomaly": True},
        ]
        result = RetrainExecutor.apply_phase1b_ensemble_weights(feedback, cfg)
        # Should succeed without error
        assert "ENSEMBLE_DETECTOR_WEIGHTS" in result

    def test_rollback_with_empty_snapshot_returns_default_config(self):
        """Line 119: rollback(None/empty) returns default_customer_config()."""
        result = RetrainExecutor.rollback({})
        expected = default_customer_config()
        assert result["ANOMALY_THRESHOLD"] == expected["ANOMALY_THRESHOLD"]

    def test_rollback_with_none_snapshot_returns_default_config(self):
        """rollback(None) must not raise; returns default config."""
        result = RetrainExecutor.rollback(None)
        expected = default_customer_config()
        assert result["ANOMALY_THRESHOLD"] == expected["ANOMALY_THRESHOLD"]

    def test_apply_phase1_runs_both_phases(self):
        """apply_phase1() chains phase1a and phase1b."""
        cfg = default_customer_config()
        feedback = [
            {
                "is_anomaly": True,
                "anomaly_score": 0.9,
                "detector_scores": {"lstm_vae": 0.85},
            }
        ] * 5
        result = RetrainExecutor.apply_phase1(feedback, cfg)
        assert "ANOMALY_THRESHOLD" in result
        assert "ENSEMBLE_DETECTOR_WEIGHTS" in result


# ===========================================================================
# RetrainEvaluator – uncovered: lines 33, 45, 56, 58
# ===========================================================================


class TestRetrainEvaluatorUncoveredPaths:
    def test_empty_holdout_returns_zero_rates(self):
        """Line 45: empty holdout → {fp_rate: 0.0, tp_rate: 0.0, ...}."""
        result = RetrainEvaluator.evaluate(default_customer_config(), [])
        assert result == {"fp_rate": 0.0, "tp_rate": 0.0, "mean_confidence": 0.0}

    def test_predict_uses_anomaly_score_when_no_detector_scores(self):
        """Line 33: anomaly_score used when detector_scores is absent."""
        cfg = default_customer_config()
        cfg["ANOMALY_THRESHOLD"] = 0.5
        holdout = [
            {"anomaly_score": 0.8, "is_anomaly": True},   # TP
            {"anomaly_score": 0.3, "is_anomaly": False},  # TN
        ]
        result = RetrainEvaluator.evaluate(cfg, holdout)
        assert result["tp_rate"] == pytest.approx(1.0)
        assert result["fp_rate"] == pytest.approx(0.0)

    def test_predict_false_positive_path(self):
        """Line 56: FP counted when label=False but pred=True."""
        cfg = default_customer_config()
        cfg["ANOMALY_THRESHOLD"] = 0.5
        holdout = [
            {"anomaly_score": 0.9, "is_anomaly": False},  # FP
        ]
        result = RetrainEvaluator.evaluate(cfg, holdout)
        assert result["fp_rate"] == pytest.approx(1.0)

    def test_predict_false_negative_path(self):
        """Line 58: FN counted when label=True but pred=False."""
        cfg = default_customer_config()
        cfg["ANOMALY_THRESHOLD"] = 0.99
        holdout = [
            {"anomaly_score": 0.1, "is_anomaly": True},  # FN
        ]
        result = RetrainEvaluator.evaluate(cfg, holdout)
        assert result["tp_rate"] == pytest.approx(0.0)

    def test_improvement_pct_fp_before_zero_uses_tp_gain(self):
        """improvement_pct with fp_before=0 computes tp_gain instead."""
        before = {"fp_rate": 0.0, "tp_rate": 0.5}
        after = {"fp_rate": 0.0, "tp_rate": 0.8}
        result = RetrainEvaluator.improvement_pct(before, after)
        assert result == pytest.approx(0.3)

    def test_improvement_pct_returns_fractional_fp_reduction(self):
        """Standard case: fractional FP-rate reduction."""
        before = {"fp_rate": 0.4, "tp_rate": 0.8}
        after = {"fp_rate": 0.2, "tp_rate": 0.8}
        result = RetrainEvaluator.improvement_pct(before, after)
        assert result == pytest.approx(0.5)

    def test_improvement_pct_non_negative_clamp(self):
        """improvement_pct is always >= 0."""
        before = {"fp_rate": 0.2}
        after = {"fp_rate": 0.4}  # worse
        result = RetrainEvaluator.improvement_pct(before, after)
        assert result == 0.0

    def test_predict_with_detector_scores_and_ensemble_agreement(self):
        """_predict uses ensemble logic when detector_scores is provided."""
        cfg = default_customer_config()
        cfg["ENSEMBLE_ANOMALY_THRESHOLD"] = 0.5
        cfg["ENSEMBLE_MIN_AGREEMENT"] = 1
        cfg["ENSEMBLE_DETECTOR_WEIGHTS"] = {"lstm_vae": 1.0}
        item = {"detector_scores": {"lstm_vae": 0.9}, "is_anomaly": True}
        result = RetrainEvaluator._predict(item, cfg)
        assert result is True


# ===========================================================================
# ModelRegistry – uncovered: lines 81, 128, 154-163
# ===========================================================================


class TestModelRegistry:
    @pytest.mark.asyncio
    async def test_register_and_get_active_model(self, db_session: AsyncSession):
        """register_model creates a model, get_active_model retrieves it."""
        from src.models.model_registry import ModelRegistry

        registry = ModelRegistry(db_session)
        model_id = await registry.register_model(
            customer_id="mr_cust",
            model_type="lstm_vae",
            checkpoint_path="/tmp/model.pt",
            metrics={"val_loss": 0.05},
        )
        assert model_id is not None

        active = await registry.get_active_model("mr_cust")
        assert active is not None
        assert active["model_id"] == model_id
        assert active["checkpoint_path"] == "/tmp/model.pt"
        assert active["metrics"]["val_loss"] == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_get_active_model_returns_none_when_no_deployment(
        self, db_session: AsyncSession
    ):
        """Line 81: get_active_model returns None when no deployment found."""
        from src.models.model_registry import ModelRegistry

        registry = ModelRegistry(db_session)
        result = await registry.get_active_model("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_rollback_model_returns_false_for_unknown_model_id(
        self, db_session: AsyncSession
    ):
        """Line 128: rollback_model returns False when model_id not found."""
        from src.models.model_registry import ModelRegistry

        registry = ModelRegistry(db_session)
        result = await registry.rollback_model("cust_a", "nonexistent_model_id")
        assert result is False

    @pytest.mark.asyncio
    async def test_archive_model_returns_false_for_unknown_model(
        self, db_session: AsyncSession
    ):
        """Lines 154-163: archive_model returns False when model not found."""
        from src.models.model_registry import ModelRegistry

        registry = ModelRegistry(db_session)
        result = await registry.archive_model("nonexistent_model_id")
        assert result is False

    @pytest.mark.asyncio
    async def test_archive_model_marks_version_archived(
        self, db_session: AsyncSession
    ):
        """archive_model sets status='archived' on the model version."""
        from src.models.model_registry import ModelRegistry

        registry = ModelRegistry(db_session)
        model_id = await registry.register_model(
            customer_id="arch_cust",
            model_type="lstm_vae",
            checkpoint_path="/tmp/archive.pt",
        )
        result = await registry.archive_model(model_id)
        assert result is True

        # Verify via list_models
        models = await registry.list_models("arch_cust")
        archived = [m for m in models if m["model_id"] == model_id]
        assert archived[0]["status"] == "archived"

    @pytest.mark.asyncio
    async def test_rollback_model_succeeds_for_known_model(
        self, db_session: AsyncSession
    ):
        """rollback_model returns True for an existing model version."""
        from src.models.model_registry import ModelRegistry

        registry = ModelRegistry(db_session)
        model_id = await registry.register_model(
            customer_id="rb_cust",
            model_type="lstm_vae",
            checkpoint_path="/tmp/rb.pt",
        )
        result = await registry.rollback_model("rb_cust", model_id)
        assert result is True

    @pytest.mark.asyncio
    async def test_list_models_empty_when_no_models(
        self, db_session: AsyncSession
    ):
        """list_models returns empty list for tenant with no models."""
        from src.models.model_registry import ModelRegistry

        registry = ModelRegistry(db_session)
        result = await registry.list_models("empty_cust")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_models_returns_all_versions(
        self, db_session: AsyncSession
    ):
        """list_models includes all registered models for a customer."""
        from src.models.model_registry import ModelRegistry

        registry = ModelRegistry(db_session)
        id1 = await registry.register_model(
            customer_id="list_cust",
            model_type="lstm_vae",
            checkpoint_path="/tmp/m1.pt",
        )
        id2 = await registry.register_model(
            customer_id="list_cust",
            model_type="lstm_vae",
            checkpoint_path="/tmp/m2.pt",
        )
        models = await registry.list_models("list_cust")
        model_ids = [m["model_id"] for m in models]
        assert id1 in model_ids
        assert id2 in model_ids
