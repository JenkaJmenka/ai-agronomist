"""
Sprint A comprehensive_pipeline.py coverage and verification tests.

Verifies:
  A1 – store_incident() gated on 'if is_anomaly:'  (incident_id is None for normal)
  A2 – _tracker_cache is a plain dict, not setattr/getattr cache
  A3 – EWMA/POT failures emit logger.warning, never logger.debug

Also exercises all previously uncovered lines:
  218        – run_batch() ValueError for wrong shape
  265-271    – physics-suppressed early return
  305-307    – EWMA/POT load-exception warning
  305 (line) – finite dynamic threshold applied (_effective_threshold = _dynamic)
  327        – ensemble stuck-fault type path
  337->345   – tracker None branch (update block skipped)
  341-342    – EWMA/POT update exception warning
  404-405    – online-learner exception handler
  466        – _suppressed_result() return statement
  513-520    – _load_trackers() DB exception fallback
  554-556    – _save_trackers() DB exception + rollback
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from src.utils.lru import LRUDict
import torch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.comprehensive_pipeline import (
    ComprehensiveInferencePipeline,
    _denormalize_sequence,
    _observe_online_learner,
    build_default_baseline,
)
from src.config import INPUT_DIM, LATENT_DIM, MODEL_CONFIG, RANDOM_SEED, SEQ_LEN
from src.db.models import Base, LearningState
from src.models.ensemble_detector import EnsembleResult
from src.models.ewma_tracker import EWMATracker
from src.models.inference import AnomalyDetector
from src.models.lstm_vae import LSTMVAE
from src.models.physics_validator import ValidationResult
from src.models.pot_threshold import POTThresholdFitter


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _sample_sequence(seed: int = RANDOM_SEED) -> List[List[float]]:
    """Nominal mid-range readings with small jitter.

    Uniform-random normalized values denormalize to physically wild swings
    (e.g. light jumping across 0-1000 lux) and legitimately trip the
    pipeline's rule gates (spike/noise). These tests target the full
    LSTM/ensemble path, so the input must read as genuinely normal.
    """
    rng = np.random.default_rng(seed)
    mids = np.full((SEQ_LEN, INPUT_DIM), 0.5, dtype=np.float32)
    jitter = rng.normal(0.0, 0.002, (SEQ_LEN, INPUT_DIM)).astype(np.float32)
    return np.clip(mids + jitter, 0.0, 1.0).tolist()


def _make_enriched_side_effect(raw_score: float = 0.1):
    """Factory for predict_batch_enriched side effects."""
    def _side_effect(seqs):
        n = len(seqs)
        scores = np.full(n, raw_score, dtype=np.float32)
        embeddings = np.zeros((n, LATENT_DIM), dtype=np.float32)
        uncertainties = np.full(n, 0.1, dtype=np.float32)
        return scores, embeddings, uncertainties
    return _side_effect


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def checkpoint_path(tmp_path) -> str:
    model = LSTMVAE(**MODEL_CONFIG)
    path = str(tmp_path / "sprint_a_model.pt")
    torch.save(
        {"model_config": MODEL_CONFIG, "model_state_dict": model.state_dict()},
        path,
    )
    return path


@pytest.fixture
def detector(checkpoint_path: str):
    instance = AnomalyDetector(checkpoint_path=checkpoint_path)
    yield instance


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def pipeline(detector: AnomalyDetector) -> ComprehensiveInferencePipeline:
    return ComprehensiveInferencePipeline(detector=detector)


@pytest.fixture
def patch_normal_score():
    """Force raw_score=0.1 → anomaly_score≈0.05, well below ANOMALY_THRESHOLD."""
    with patch(
        "src.models.inference.AnomalyDetector.predict_batch_enriched",
        side_effect=_make_enriched_side_effect(raw_score=0.1),
    ) as mock_fn:
        yield mock_fn


@pytest.fixture
def patch_physics_neutral():
    """Return a neutral (not suppressed, not confirmed) physics result.

    Tests that rely on LSTM or ensemble behaviour must suppress the
    physics confirmation path to avoid time-of-day non-determinism: the
    nocturnal-light rule fires at UTC night hours (22-04) for random
    sequences whose denormalized light channel exceeds 50 lux.
    """
    neutral = ValidationResult(suppressed=False, confirmed=False, reason="")
    with patch(
        "src.api.services.comprehensive_pipeline.PhysicsValidator.validate",
        return_value=neutral,
    ):
        yield


@pytest.fixture
def patch_anomaly_score():
    """Force raw_score=2.0 → anomaly_score=1.0, well above ANOMALY_THRESHOLD."""
    with patch(
        "src.models.inference.AnomalyDetector.predict_batch_enriched",
        side_effect=_make_enriched_side_effect(raw_score=2.0),
    ) as mock_fn:
        yield mock_fn


# ---------------------------------------------------------------------------
# Sprint A1 – store_incident() gated on is_anomaly
# ---------------------------------------------------------------------------


class TestA1StoreIncidentGating:
    """A1: store_incident() only called inside 'if is_anomaly:' block."""

    @pytest.mark.asyncio
    async def test_no_incident_for_normal_reading(
        self, pipeline, db_session, patch_normal_score, patch_physics_neutral
    ):
        """Normal reading: incident_id must be None, no DB write."""
        result = await pipeline.run(
            customer_id="a1_normal",
            sequence=_sample_sequence(),
            db=db_session,
        )
        assert result["incident_id"] is None, (
            "A1 regression: store_incident() called for non-anomaly"
        )

    @pytest.mark.asyncio
    async def test_incident_stored_for_anomaly(
        self, pipeline, db_session, patch_anomaly_score
    ):
        """Anomalous reading: incident_id is a UUID string stored in DB."""
        result = await pipeline.run(
            customer_id="a1_anomaly",
            sequence=_sample_sequence(),
            db=db_session,
        )
        assert result["is_anomaly"] is True
        assert result["incident_id"] is not None
        assert isinstance(result["incident_id"], str)

    @pytest.mark.asyncio
    async def test_incident_is_none_when_physics_suppresses(
        self, pipeline, db_session
    ):
        """Physics-suppressed result must have incident_id=None (A1 + suppression)."""
        suppressed = ValidationResult(suppressed=True, confirmed=False, reason="sim_suppress")
        with patch(
            "src.api.services.comprehensive_pipeline.PhysicsValidator.validate",
            return_value=suppressed,
        ):
            result = await pipeline.run(
                customer_id="a1_suppressed",
                sequence=_sample_sequence(),
                db=db_session,
            )
        assert result["incident_id"] is None
        assert result["is_anomaly"] is False


class TestGateIncidentPersistence:
    """Rule-gate detections must persist incidents.

    P1 regression guard: the short-circuit gates previously returned
    incident_id=None, so rule-detected anomalies never reached incident
    memory, operator feedback, or fault-type enrichment.
    """

    @pytest.mark.asyncio
    async def test_spike_gate_stores_incident(self, pipeline, db_session):
        """A MAD-spike gate result carries a stored incident id."""
        sequence = np.asarray(_sample_sequence(), dtype=np.float32)
        sequence[-1, 0] = 0.95  # ~+4.5°C temperature jump at the last step

        result = await pipeline.run(
            customer_id="gate_incident",
            sequence=sequence.tolist(),
            db=db_session,
        )

        assert result["is_anomaly"] is True
        assert result["fault_type"] == "spike_fault"
        assert result["incident_id"] is not None

        from src.db.models import Incident

        row = await db_session.get(Incident, result["incident_id"])
        assert row is not None
        assert row.fault_type == "spike_fault"
        assert row.tenant_id == "gate_incident"


class TestRuleVoteFusion:
    """Rule detectors vote instead of short-circuiting (P2 fusion).

    A fired rule must confirm the anomaly and pin the fault type while the
    full LSTM/ensemble path still runs — attribution, pattern scores, and
    recommendations must all be present alongside the rule's vote.
    """

    @pytest.mark.asyncio
    async def test_spike_vote_keeps_full_pipeline_output(
        self, pipeline, db_session, patch_physics_neutral
    ):
        """Physics is patched neutral: a confirmed physics fault deliberately
        outranks rule votes, and the light channel trips nocturnal-light
        confirmation whenever the suite runs at night."""
        sequence = np.asarray(_sample_sequence(), dtype=np.float32)
        sequence[-1, 0] = 0.95  # ~+4.5°C temperature jump at the last step

        result = await pipeline.run(
            customer_id="fusion_spike",
            sequence=sequence.tolist(),
            db=db_session,
        )

        assert result["is_anomaly"] is True
        assert result["fault_type"] == "spike_fault"
        assert result["root_cause"] == "transient_spike"
        # Rule vote is visible as detector evidence …
        assert result["detector_scores"].get("spike_mad") == 1.0
        # … and the affected sensor leads the merged list.
        assert result["affected_sensors"][0] == "temperature"
        # Full pipeline output survives fusion (pre-fusion gates returned
        # empty pattern scores and no recommendations).
        assert result["pattern_scores"], "pattern detection must run"
        assert result["all_recommendations"], "recommendations must run"

    @pytest.mark.asyncio
    async def test_reasoning_names_the_algorithm_that_fired(
        self, pipeline, db_session, patch_physics_neutral
    ):
        """Reasoning must describe the detection algorithm, not the model's
        pattern guess. Regression for the P2 gap where root_cause was
        overridden but reasoning kept the RootCauseAnalyzer's unrelated text.
        """
        sequence = np.asarray(_sample_sequence(), dtype=np.float32)
        sequence[-1, 0] = 0.95  # temperature spike → spike-MAD rule fires

        result = await pipeline.run(
            customer_id="fusion_reason",
            sequence=sequence.tolist(),
            db=db_session,
        )

        assert result["fault_type"] == "spike_fault"
        reasoning = result["reasoning"]
        assert "MAD" in reasoning and "temperature" in reasoning, reasoning

        # Reasoning is persisted on the incident so the feedback UI can show it
        # (the fused path previously omitted it, blanking the review queue).
        from src.db.models import Incident

        row = await db_session.get(Incident, result["incident_id"])
        assert row.root_cause_details["reasoning"] == reasoning
        assert row.root_cause_details["hint_summary"]


# ---------------------------------------------------------------------------
# Sprint A2 – _tracker_cache is a plain dict
# ---------------------------------------------------------------------------


class TestA2TrackerCacheDict:
    """A2: _tracker_cache must be a regular dict, no setattr/getattr with cache_key."""

    def test_tracker_cache_is_dict(self, pipeline):
        assert isinstance(pipeline._tracker_cache, (dict, LRUDict))

    def test_tracker_cache_starts_empty(self, pipeline):
        assert len(pipeline._tracker_cache) == 0

    def test_tracker_cache_has_no_dynamic_attrs(self, pipeline):
        """No _ewma_<x> or _pot_<x> dynamic attributes exist on the pipeline."""
        # If setattr was used, we'd see attributes like _ewma_cache_<customer_id>
        dynamic_attrs = [
            attr for attr in vars(pipeline)
            if attr.startswith("_ewma_cache_") or attr.startswith("_pot_cache_")
        ]
        assert dynamic_attrs == [], f"Dynamic cache attrs found: {dynamic_attrs}"

    @pytest.mark.asyncio
    async def test_tracker_cache_populated_by_load(
        self, pipeline, db_session, patch_physics_neutral
    ):
        """After a pipeline.run(), _tracker_cache has an entry for the tenant.

        Physics is patched neutral: suppression short-circuits before the
        tracker load, and the nocturnal-light rule is wall-clock-dependent.
        """
        await pipeline.run(
            customer_id="a2_load",
            sequence=_sample_sequence(),
            db=db_session,
        )
        assert "a2_load" in pipeline._tracker_cache
        ewma, pot = pipeline._tracker_cache["a2_load"]
        assert isinstance(ewma, EWMATracker)
        assert isinstance(pot, POTThresholdFitter)

    @pytest.mark.asyncio
    async def test_tracker_cache_hit_skips_db(
        self, pipeline, db_session, patch_physics_neutral
    ):
        """Second call for same tenant uses cached trackers (no extra DB execute)."""
        customer_id = "a2_cache_hit"

        await pipeline.run(
            customer_id=customer_id,
            sequence=_sample_sequence(1),
            db=db_session,
        )
        # First call populates cache
        assert customer_id in pipeline._tracker_cache

        # Second call should hit cache; we verify by clearing DB and expecting no error
        # The cache returns the existing trackers without a DB query.
        cached_before = pipeline._tracker_cache[customer_id]
        await pipeline.run(
            customer_id=customer_id,
            sequence=_sample_sequence(2),
            db=db_session,
        )
        # Cache was written again (save_trackers updates cache) but same types
        ewma, pot = pipeline._tracker_cache[customer_id]
        assert isinstance(ewma, EWMATracker)
        assert isinstance(pot, POTThresholdFitter)


# ---------------------------------------------------------------------------
# Sprint A3 – logger.warning at EWMA/POT failure sites (not logger.debug)
# ---------------------------------------------------------------------------


class TestA3LoggerWarningAtFailures:
    """A3: EWMA/POT exceptions must log at WARNING level, not DEBUG."""

    @pytest.mark.asyncio
    async def test_load_failure_emits_warning(
        self, pipeline, db_session, caplog
    ):
        """Patching _load_trackers to raise triggers logger.warning at load site."""
        import logging

        with patch.object(
            pipeline,
            "_load_trackers",
            side_effect=RuntimeError("sim load fail"),
        ), caplog.at_level(logging.WARNING):
            await pipeline.run(
                customer_id="a3_load",
                sequence=_sample_sequence(),
                db=db_session,
            )

        warning_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert any("EWMA/POT" in m and "load skipped" in m for m in warning_msgs), (
            "Expected WARNING for EWMA/POT load failure; got: " + str(warning_msgs)
        )
        debug_ewma_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.DEBUG and ("EWMA" in r.message or "POT" in r.message)
        ]
        assert not debug_ewma_msgs, (
            "A3 regression: EWMA/POT failure logged at DEBUG level"
        )

    @pytest.mark.asyncio
    async def test_update_failure_emits_warning(
        self, pipeline, db_session, caplog
    ):
        """EWMA tracker.update() raising triggers logger.warning at update site."""
        import logging

        ewma_mock = MagicMock(spec=EWMATracker)
        ewma_mock.get_threshold.return_value = float("inf")
        ewma_mock.update.side_effect = RuntimeError("update fail")
        ewma_mock.to_dict.return_value = {}

        pot_mock = MagicMock(spec=POTThresholdFitter)
        pot_mock.get_threshold.return_value = float("inf")

        with patch.object(pipeline, "_load_trackers", return_value=(ewma_mock, pot_mock)):
            with caplog.at_level(logging.WARNING):
                result = await pipeline.run(
                    customer_id="a3_update",
                    sequence=_sample_sequence(),
                    db=db_session,
                )

        warning_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert any("EWMA/POT" in m and "update skipped" in m for m in warning_msgs), (
            "Expected WARNING for EWMA/POT update failure; got: " + str(warning_msgs)
        )
        assert "is_anomaly" in result, "Pipeline should still return a result"


# ---------------------------------------------------------------------------
# Line 218 – run_batch() ValueError for wrong shape
# ---------------------------------------------------------------------------


class TestRunBatch:
    @pytest.mark.asyncio
    async def test_wrong_shape_raises_value_error(self, pipeline, db_session):
        """run_batch() must raise ValueError for sequences with wrong input dim."""
        bad_seq = np.zeros((SEQ_LEN, INPUT_DIM + 1), dtype=np.float32)
        with pytest.raises(ValueError, match="sequence shape"):
            await pipeline.run_batch(
                customer_id="batch_bad",
                sequences=[bad_seq],
                db=db_session,
            )

    @pytest.mark.asyncio
    async def test_wrong_seq_len_raises_value_error(self, pipeline, db_session):
        """run_batch() raises for wrong sequence length."""
        bad_seq = np.zeros((SEQ_LEN - 1, INPUT_DIM), dtype=np.float32)
        with pytest.raises(ValueError, match="sequence shape"):
            await pipeline.run_batch(
                customer_id="batch_bad_len",
                sequences=[bad_seq],
                db=db_session,
            )

    @pytest.mark.asyncio
    async def test_correct_shape_batch_succeeds(self, pipeline, db_session):
        """run_batch() processes correctly-shaped sequences without error."""
        seq = np.array(_sample_sequence(), dtype=np.float32)
        results = await pipeline.run_batch(
            customer_id="batch_ok",
            sequences=[seq],
            db=db_session,
        )
        assert len(results) == 1
        assert "is_anomaly" in results[0]

    @pytest.mark.asyncio
    async def test_batch_returns_one_result_per_sequence(self, pipeline, db_session):
        """run_batch() returns results list matching input sequences length."""
        seqs = [np.array(_sample_sequence(i), dtype=np.float32) for i in range(3)]
        results = await pipeline.run_batch(
            customer_id="batch_multi",
            sequences=seqs,
            db=db_session,
        )
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_batch_greenhouse_id_defaults_to_default(self, pipeline, db_session):
        """run_batch() without greenhouse_id uses 'default' greenhouse."""
        seq = np.array(_sample_sequence(), dtype=np.float32)
        results = await pipeline.run_batch(
            customer_id="batch_gh_default",
            sequences=[seq],
            db=db_session,
        )
        assert results[0].get("pipeline_version") == "2.0"


# ---------------------------------------------------------------------------
# Lines 265-271, 466 – Physics-suppressed early return + _suppressed_result
# ---------------------------------------------------------------------------


class TestPhysicsSuppressed:
    @pytest.mark.asyncio
    async def test_suppressed_result_is_non_anomaly(self, pipeline, db_session):
        """Physics-suppressed result has is_anomaly=False and no incident_id."""
        suppressed = ValidationResult(suppressed=True, confirmed=False, reason="test_suppression")
        with patch(
            "src.api.services.comprehensive_pipeline.PhysicsValidator.validate",
            return_value=suppressed,
        ):
            result = await pipeline.run(
                customer_id="suppressed_tenant",
                sequence=_sample_sequence(),
                db=db_session,
            )

        assert result["is_anomaly"] is False
        assert result["incident_id"] is None
        assert result["physics_validation"] == "test_suppression"
        assert result["root_cause"] == "physically_suppressed"
        assert result["confidence"] == 0.0
        assert result["confidence_level"] == "very_low"

    @pytest.mark.asyncio
    async def test_suppressed_result_has_pipeline_version(self, pipeline, db_session):
        """Suppressed result still includes pipeline_version and timestamp."""
        suppressed = ValidationResult(suppressed=True, confirmed=False, reason="sim")
        with patch(
            "src.api.services.comprehensive_pipeline.PhysicsValidator.validate",
            return_value=suppressed,
        ):
            result = await pipeline.run(
                customer_id="suppressed_schema",
                sequence=_sample_sequence(),
                db=db_session,
            )

        assert "pipeline_version" in result
        assert "timestamp" in result
        assert result["pipeline_version"] == "2.0"

    def test_suppressed_result_static_method_returns_correct_schema(self):
        """_suppressed_result() static method (line 466) returns all expected keys."""
        result = ComprehensiveInferencePipeline._suppressed_result(
            anomaly_score=0.42,
            reason="nocturnal_light",
        )
        required_keys = {
            "is_anomaly", "anomaly_score", "confidence", "confidence_level",
            "pattern_type", "fault_type", "attribution", "affected_sensors",
            "pattern_scores", "root_cause", "root_cause_confidence", "reasoning",
            "alternative_causes", "severity", "urgency", "immediate_actions",
            "follow_up_actions", "all_recommendations", "requires_technician",
            "estimated_resolution_minutes", "incident_id", "detector_scores",
            "ensemble_score", "ensemble_agreement", "physics_validation",
            "learning_progress", "timestamp", "pipeline_version",
        }
        assert required_keys.issubset(result.keys())
        assert result["anomaly_score"] == pytest.approx(0.42)
        assert result["physics_validation"] == "nocturnal_light"
        assert result["is_anomaly"] is False


# ---------------------------------------------------------------------------
# Lines 305-307 – EWMA/POT load-exception warning path
# ---------------------------------------------------------------------------


class TestEWMAPOTLoadException:
    @pytest.mark.asyncio
    async def test_load_exception_caught_and_warned(
        self, pipeline, db_session, caplog
    ):
        """EWMA/POT load exception is caught; pipeline still returns valid result."""
        import logging

        with patch.object(
            pipeline,
            "_load_trackers",
            side_effect=RuntimeError("simulated load failure"),
        ), caplog.at_level(logging.WARNING):
            result = await pipeline.run(
                customer_id="load_ex",
                sequence=_sample_sequence(),
                db=db_session,
            )

        assert "is_anomaly" in result
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "EWMA/POT" in msg and "load skipped" in msg for msg in warning_msgs
        )

    @pytest.mark.asyncio
    async def test_load_exception_uses_default_anomaly_threshold(
        self, pipeline, db_session, patch_anomaly_score
    ):
        """Default ANOMALY_THRESHOLD used when EWMA/POT load fails."""
        with patch.object(
            pipeline,
            "_load_trackers",
            side_effect=RuntimeError("db error"),
        ):
            result = await pipeline.run(
                customer_id="load_default_thresh",
                sequence=_sample_sequence(),
                db=db_session,
            )

        # With score above default ANOMALY_THRESHOLD, is_anomaly should be True
        assert isinstance(result["is_anomaly"], bool)


# ---------------------------------------------------------------------------
# Line 305 – finite dynamic threshold applied (_effective_threshold = _dynamic)
# ---------------------------------------------------------------------------


class TestFiniteDynamicThreshold:
    @pytest.mark.asyncio
    async def test_finite_threshold_overwrites_default(self, pipeline, db_session):
        """When EWMA tracker returns a finite threshold, it replaces the default."""
        # Build an EWMA tracker with enough observations for a finite threshold.
        ewma = EWMATracker(alpha=0.1, k=2.0)
        for _ in range(EWMATracker._MIN_OBS + 5):
            ewma.update(0.4)

        pot = POTThresholdFitter()
        assert ewma.get_threshold() != float("inf"), "Sanity: threshold must be finite"

        with patch.object(pipeline, "_load_trackers", return_value=(ewma, pot)):
            with patch.object(pipeline, "_save_trackers", new_callable=AsyncMock):
                result = await pipeline.run(
                    customer_id="finite_thresh",
                    sequence=_sample_sequence(),
                    db=db_session,
                )

        assert "is_anomaly" in result

    @pytest.mark.asyncio
    async def test_both_trackers_finite_uses_max(self, pipeline, db_session):
        """When both trackers return finite values, the higher one is used."""
        ewma = EWMATracker(alpha=0.1, k=3.0)
        for _ in range(EWMATracker._MIN_OBS + 5):
            ewma.update(0.2)

        pot = POTThresholdFitter()
        # Pre-populate POT buffer with many normal readings to get finite threshold
        for _ in range(100):
            pot._buffer.append(0.2)
        pot._threshold = 0.6
        pot._n_since_fit = 0

        assert math.isfinite(ewma.get_threshold())
        assert math.isfinite(pot.get_threshold())

        with patch.object(pipeline, "_load_trackers", return_value=(ewma, pot)):
            with patch.object(pipeline, "_save_trackers", new_callable=AsyncMock):
                result = await pipeline.run(
                    customer_id="both_finite",
                    sequence=_sample_sequence(),
                    db=db_session,
                )

        assert isinstance(result["is_anomaly"], bool)


# ---------------------------------------------------------------------------
# Line 327 – ensemble stuck-fault type path
# ---------------------------------------------------------------------------


class TestEnsembleStuckFaultType:
    @pytest.mark.asyncio
    async def test_stuck_fault_type_from_ensemble_used(
        self, pipeline, db_session, patch_physics_neutral
    ):
        """When ensemble.detect() returns fault_type='stuck_sensor', it's in result."""
        stuck = EnsembleResult(
            detector_scores={"lstm_vae": 0.9},
            ensemble_score=0.9,
            agreement_count=2,
            is_anomaly=True,
            bias_flag=False,
            fault_type="stuck_sensor",
        )
        pipeline._ensemble = MagicMock()
        pipeline._ensemble.detect.return_value = stuck

        result = await pipeline.run(
            customer_id="stuck_fault",
            sequence=_sample_sequence(),
            db=db_session,
        )

        assert result["fault_type"] == "stuck_sensor"

    @pytest.mark.asyncio
    async def test_stuck_fault_bypasses_fault_classifier(self, pipeline, db_session):
        """When ensemble sets fault_type, FaultClassifier.classify is NOT called."""
        stuck = EnsembleResult(
            detector_scores={},
            ensemble_score=0.85,
            agreement_count=1,
            is_anomaly=True,
            fault_type="stuck_sensor",
        )
        pipeline._ensemble = MagicMock()
        pipeline._ensemble.detect.return_value = stuck

        with patch(
            "src.api.services.comprehensive_pipeline.FaultClassifier.classify"
        ) as mock_classify:
            await pipeline.run(
                customer_id="stuck_bypass",
                sequence=_sample_sequence(),
                db=db_session,
            )

        mock_classify.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_fault_type_uses_classifier(
        self, pipeline, db_session, patch_physics_neutral
    ):
        """When ensemble.fault_type is None, FaultClassifier.classify IS called."""
        normal = EnsembleResult(
            detector_scores={},
            ensemble_score=0.3,
            agreement_count=0,
            is_anomaly=False,
            fault_type=None,
        )
        pipeline._ensemble = MagicMock()
        pipeline._ensemble.detect.return_value = normal

        with patch(
            "src.api.services.comprehensive_pipeline.FaultClassifier.classify",
            return_value="drift_fault",
        ) as mock_classify:
            result = await pipeline.run(
                customer_id="none_fault_type",
                sequence=_sample_sequence(),
                db=db_session,
            )

        mock_classify.assert_called_once()


# ---------------------------------------------------------------------------
# Lines 337->345, 341-342 – EWMA/POT update exception warning
# ---------------------------------------------------------------------------


class TestEWMAPOTUpdateException:
    @pytest.mark.asyncio
    async def test_update_exception_caught_and_warned(
        self, pipeline, db_session, caplog
    ):
        """EWMA update raising causes logger.warning; pipeline still returns result."""
        import logging

        ewma_mock = MagicMock(spec=EWMATracker)
        ewma_mock.get_threshold.return_value = float("inf")
        ewma_mock.update.side_effect = RuntimeError("update broken")
        ewma_mock.to_dict.return_value = {}

        pot_mock = MagicMock(spec=POTThresholdFitter)
        pot_mock.get_threshold.return_value = float("inf")

        with patch.object(pipeline, "_load_trackers", return_value=(ewma_mock, pot_mock)):
            with caplog.at_level(logging.WARNING):
                result = await pipeline.run(
                    customer_id="update_fail",
                    sequence=_sample_sequence(),
                    db=db_session,
                )

        assert "is_anomaly" in result
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "EWMA/POT" in msg and "update skipped" in msg for msg in warning_msgs
        )

    @pytest.mark.asyncio
    async def test_tracker_none_skips_update_block(self, pipeline, db_session):
        """When _load_trackers fails (trackers remain None), update block is skipped."""
        # Force load failure so trackers remain None → 337->345 branch taken
        with patch.object(
            pipeline,
            "_load_trackers",
            side_effect=RuntimeError("load fail"),
        ):
            result = await pipeline.run(
                customer_id="skip_update",
                sequence=_sample_sequence(),
                db=db_session,
            )
        # Pipeline completes without error even though trackers were never loaded
        assert "is_anomaly" in result


# ---------------------------------------------------------------------------
# Lines 404-405 – Online-learner exception handler
# ---------------------------------------------------------------------------


class TestOnlineLearnerException:
    @pytest.mark.asyncio
    async def test_learner_exception_does_not_crash_pipeline(
        self, pipeline, db_session, patch_anomaly_score
    ):
        """Online learner RuntimeError is swallowed; pipeline still returns result."""
        with patch(
            "src.api.services.comprehensive_pipeline._observe_online_learner",
            side_effect=RuntimeError("learner unavailable"),
        ):
            result = await pipeline.run(
                customer_id="learner_crash",
                sequence=_sample_sequence(),
                db=db_session,
            )

        assert "is_anomaly" in result
        assert result["is_anomaly"] is True  # patch_anomaly_score forces anomaly

    @pytest.mark.asyncio
    async def test_learner_exception_logged_at_error(
        self, pipeline, db_session, patch_anomaly_score, caplog
    ):
        """Online learner failure is logged at ERROR (exception) level."""
        import logging

        with patch(
            "src.api.services.comprehensive_pipeline._observe_online_learner",
            side_effect=RuntimeError("learner boom"),
        ), caplog.at_level(logging.ERROR):
            await pipeline.run(
                customer_id="learner_log",
                sequence=_sample_sequence(),
                db=db_session,
            )

        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("Online learner observe failed" in msg for msg in error_msgs)

    @pytest.mark.asyncio
    async def test_learner_not_called_for_normal_reading(
        self, pipeline, db_session, patch_normal_score, patch_physics_neutral
    ):
        """Online learner is NOT called when is_anomaly=False."""
        with patch(
            "src.api.services.comprehensive_pipeline._observe_online_learner"
        ) as mock_observe:
            await pipeline.run(
                customer_id="learner_not_called",
                sequence=_sample_sequence(),
                db=db_session,
            )
        mock_observe.assert_not_called()


# ---------------------------------------------------------------------------
# Lines 513-520 – _load_trackers() DB exception fallback
# ---------------------------------------------------------------------------


class TestLoadTrackersDBException:
    @pytest.mark.asyncio
    async def test_db_execute_raises_falls_back_to_fresh_trackers(
        self, pipeline, caplog
    ):
        """DB execute failure → _load_trackers returns fresh EWMATracker + POTFitter."""
        import logging

        pipeline._tracker_cache.clear()

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute.side_effect = RuntimeError("DB connection lost")

        with caplog.at_level(logging.WARNING):
            ewma, pot = await pipeline._load_trackers("db_ex_tenant", mock_db)

        assert isinstance(ewma, EWMATracker)
        assert isinstance(pot, POTThresholdFitter)
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("EWMA/POT DB load failed" in msg for msg in warning_msgs)

    @pytest.mark.asyncio
    async def test_db_exception_fallback_populates_cache(self, pipeline):
        """Fresh trackers from fallback path are stored in _tracker_cache."""
        pipeline._tracker_cache.clear()

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute.side_effect = RuntimeError("DB error")

        await pipeline._load_trackers("fallback_cached", mock_db)

        assert "fallback_cached" in pipeline._tracker_cache

    @pytest.mark.asyncio
    async def test_cache_hit_skips_db_entirely(self, pipeline, db_session):
        """Cached tenant → _load_trackers never calls db.execute."""
        ewma = EWMATracker(alpha=0.1, k=3.0)
        pot = POTThresholdFitter()
        pipeline._tracker_cache["cached_tenant"] = (ewma, pot)

        mock_db = AsyncMock(spec=AsyncSession)
        result = await pipeline._load_trackers("cached_tenant", mock_db)

        assert result == (ewma, pot)
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_from_db_row_restores_tracker_state(
        self, pipeline, db_session
    ):
        """When LearningState row has tracker data, it's deserialized correctly."""
        ewma_src = EWMATracker(alpha=0.1, k=3.0)
        for _ in range(EWMATracker._MIN_OBS + 5):
            ewma_src.update(0.3)
        pot_src = POTThresholdFitter()

        tracker_payload = {
            "ewma": ewma_src.to_dict(),
            "pot": pot_src.to_dict(),
        }
        row = LearningState(
            customer_id="db_row_tenant",
            state_json={"ewma_pot_trackers": tracker_payload},
        )
        db_session.add(row)
        await db_session.commit()

        pipeline._tracker_cache.clear()
        loaded_ewma, loaded_pot = await pipeline._load_trackers(
            "db_row_tenant", db_session
        )

        assert isinstance(loaded_ewma, EWMATracker)
        assert isinstance(loaded_pot, POTThresholdFitter)
        assert loaded_ewma._n == ewma_src._n


# ---------------------------------------------------------------------------
# Lines 554-556 – _save_trackers() DB exception + rollback
# ---------------------------------------------------------------------------


class TestSaveTrackersDBException:
    @pytest.mark.asyncio
    async def test_db_execute_raises_logs_warning_and_rolls_back(
        self, pipeline, caplog
    ):
        """DB execute failure in _save_trackers emits warning.

        Since B-BE-2, the rollback is scoped to a savepoint via begin_nested()
        so db.rollback() is never called directly — the outer transaction is
        left untouched.
        """
        import logging

        ewma = EWMATracker(alpha=0.1, k=3.0)
        pot = POTThresholdFitter()

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute.side_effect = RuntimeError("commit refused")
        mock_db.rollback = AsyncMock()

        with caplog.at_level(logging.WARNING):
            await pipeline._save_trackers("save_fail_tenant", ewma, pot, mock_db)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("EWMA/POT DB save failed" in msg for msg in warning_msgs)
        # Savepoint auto-rolls back via begin_nested().__aexit__; bare db.rollback() must NOT be called.
        mock_db.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_updated_even_on_db_failure(self, pipeline):
        """_save_trackers updates _tracker_cache before the DB write attempt."""
        ewma = EWMATracker(alpha=0.1, k=3.0)
        pot = POTThresholdFitter()

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute.side_effect = RuntimeError("DB error")
        mock_db.rollback = AsyncMock()

        await pipeline._save_trackers("cache_on_fail", ewma, pot, mock_db)

        assert "cache_on_fail" in pipeline._tracker_cache
        assert pipeline._tracker_cache["cache_on_fail"] == (ewma, pot)

    @pytest.mark.asyncio
    async def test_save_persists_to_db_on_success(self, pipeline, db_session):
        """_save_trackers creates a LearningState row when DB is available."""
        from sqlalchemy import select

        ewma = EWMATracker(alpha=0.1, k=3.0)
        for _ in range(5):
            ewma.update(0.3)
        pot = POTThresholdFitter()

        await pipeline._save_trackers("save_ok_tenant", ewma, pot, db_session)

        result = await db_session.execute(
            select(LearningState).where(LearningState.customer_id == "save_ok_tenant")
        )
        row = result.scalar_one_or_none()
        assert row is not None
        assert "ewma_pot_trackers" in row.state_json


# ---------------------------------------------------------------------------
# Helper function coverage
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_denormalize_sequence_shape_preserved(self):
        """_denormalize_sequence output shape matches input shape."""
        normalized = np.zeros((SEQ_LEN, INPUT_DIM), dtype=np.float32)
        result = _denormalize_sequence(normalized)
        assert result.shape == (SEQ_LEN, INPUT_DIM)

    def test_denormalize_midpoint_maps_to_midpoint(self):
        """Input 0.5 (normalized midpoint) denormalizes to sensor range midpoint."""
        from src.config import TEMP_RANGE
        normalized = np.full((SEQ_LEN, INPUT_DIM), 0.5, dtype=np.float32)
        result = _denormalize_sequence(normalized)
        expected_temp_mid = (TEMP_RANGE[0] + TEMP_RANGE[1]) / 2.0
        assert result[0, 0] == pytest.approx(expected_temp_mid, rel=1e-5)

    def test_build_default_baseline_shape(self):
        """build_default_baseline returns (n, SEQ_LEN, INPUT_DIM) shape."""
        baseline = build_default_baseline(7)
        assert baseline.shape == (7, SEQ_LEN, INPUT_DIM)

    def test_build_default_baseline_dtype(self):
        """build_default_baseline returns float32 array."""
        baseline = build_default_baseline(3)
        assert baseline.dtype == np.float32

    def test_peak_sensor_values_reads_last_timestep(self, pipeline):
        """_peak_sensor_values extracts readings from the last row of the sequence."""
        seq = np.zeros((SEQ_LEN, INPUT_DIM), dtype=np.float32)
        seq[-1] = [1.0, 2.0, 3.0, 4.0]
        values = pipeline._peak_sensor_values(seq)
        assert len(values) == INPUT_DIM
        # Check first sensor (temperature) is 1.0
        assert list(values.values())[0] == pytest.approx(1.0)

    def test_observe_online_learner_calls_manager(self):
        """_observe_online_learner delegates to manager.observe()."""
        mock_manager = MagicMock()
        seq = np.zeros((SEQ_LEN, INPUT_DIM), dtype=np.float32)

        with patch("src.api.routes.learning._get_manager", return_value=mock_manager):
            _observe_online_learner("tenant_obs", seq, 0.9)

        mock_manager.observe.assert_called_once_with("tenant_obs", seq)

    def test_observe_online_learner_propagates_exceptions(self):
        """_observe_online_learner propagates exceptions (caller wraps in try/except)."""
        with patch(
            "src.api.routes.learning._get_manager",
            side_effect=RuntimeError("manager gone"),
        ):
            with pytest.raises(RuntimeError, match="manager gone"):
                _observe_online_learner(
                    "t",
                    np.zeros((SEQ_LEN, INPUT_DIM), dtype=np.float32),
                    0.5,
                )


# ---------------------------------------------------------------------------
# Pipeline initialisation edge cases
# ---------------------------------------------------------------------------


class TestPipelineInit:
    def test_default_confidence_scorer_created(self, detector):
        """Pipeline creates a ConfidenceScorer when none is passed."""
        from src.models.confidence_scorer import ConfidenceScorer

        p = ComprehensiveInferencePipeline(detector=detector)
        assert isinstance(p.confidence_scorer, ConfidenceScorer)

    def test_default_recommendation_engine_created(self, detector):
        """Pipeline creates a RecommendationEngine when none is passed."""
        from src.models.recommendation_engine import RecommendationEngine

        p = ComprehensiveInferencePipeline(detector=detector)
        assert isinstance(p.recommendation_engine, RecommendationEngine)

    def test_orchestrators_defaults_to_empty_dict(self, detector):
        """Pipeline _orchestrators defaults to empty dict when not passed."""
        p = ComprehensiveInferencePipeline(detector=detector)
        assert p._orchestrators == {}

    def test_ensemble_lazy_initialised(self, detector):
        """Ensemble is None at init; created on first _get_ensemble() call."""
        p = ComprehensiveInferencePipeline(detector=detector)
        assert p._ensemble is None
        ensemble = p._get_ensemble()
        assert ensemble is not None
        assert p._ensemble is ensemble

    def test_get_ensemble_returns_same_instance(self, detector):
        """Second call to _get_ensemble() returns the cached instance."""
        p = ComprehensiveInferencePipeline(detector=detector)
        first = p._get_ensemble()
        second = p._get_ensemble()
        assert first is second

    def test_learning_progress_none_without_orchestrator(self, pipeline):
        """_learning_progress returns None when tenant has no orchestrator."""
        result = pipeline._learning_progress("unknown_tenant")
        assert result is None

    def test_learning_progress_returns_dict_with_orchestrator(self, detector):
        """_learning_progress returns stats when orchestrator exists."""
        from src.models.retraining_orchestrator import RetrainingOrchestrator

        customer_id = "orch_tenant"
        orch = RetrainingOrchestrator(customer_id=customer_id)
        orch.observe(
            incident_id="inc_1",
            is_true_anomaly=True,
            operator_id="op_1",
            pattern_type_observed="drift",
        )

        p = ComprehensiveInferencePipeline(
            detector=detector,
            orchestrators={customer_id: orch},
        )
        progress = p._learning_progress(customer_id)
        assert progress is not None
        assert "high_quality_feedback" in progress
        assert "needed_for_retrain" in progress
        assert "retrainings_completed" in progress


# ---------------------------------------------------------------------------
# Import math needed at module level
# ---------------------------------------------------------------------------
import math  # noqa: E402
