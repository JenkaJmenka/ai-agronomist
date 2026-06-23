"""
Tests for the Phase 3.1 Active Learning subsystem.

Covers: FeedbackCollector, FeedbackValidator, RetrainingDecisionMaker,
ActiveLearner, and RetrainingOrchestrator — including input validation,
edge cases, EMA updates, retrain criteria, and promotion logic.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.config import (
    ACTIVE_LEARNING_FEEDBACK_CAPACITY,
    ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD,
    ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES,
    ACTIVE_LEARNING_MIN_PATTERN_TYPES,
    ACTIVE_LEARNING_MIN_QUALITY_LABELS,
    ACTIVE_LEARNING_MIN_RETRAIN_INTERVAL_HOURS,
    ACTIVE_LEARNING_MIN_TRAINING_SAMPLES,
    ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD,
    ACTIVE_LEARNING_OPERATOR_ALPHA,
    ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT,
)
from src.models.feedback_collector import FeedbackCollector
from src.models.feedback_validator import FeedbackValidator
from src.models.active_learner import ActiveLearner, RetrainingDecisionMaker
from src.models.retraining_orchestrator import RetrainingOrchestrator


# =========================================================================
# FeedbackCollector
# =========================================================================
class TestFeedbackCollectorCollect:
    """Test FeedbackCollector.collect and count."""

    def test_collect_basic(self) -> None:
        """Should store feedback and increment count."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_001",
            sequence=None,
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        assert collector.count() == 1

    def test_collect_all_fields(self) -> None:
        """Should store all optional fields."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_002",
            sequence=[1, 2, 3],
            is_true_anomaly=False,
            operator_id="op_bob",
            pattern_type_observed="drift",
            actual_root_cause="sensor_failure",
            severity=5,
            operator_confidence=0.9,
            notes="Looks like noise",
        )
        assert collector.count() == 1
        item = collector.feedback[0]
        assert item["incident_id"] == "inc_002"
        assert item["is_anomaly"] is False
        assert item["pattern_type"] == "drift"
        assert item["root_cause"] == "sensor_failure"
        assert item["severity"] == 5
        assert item["operator_confidence"] == 0.9
        assert item["notes"] == "Looks like noise"

    def test_collect_timestamp_is_utc(self) -> None:
        """Timestamps should use timezone-aware UTC."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_003",
            sequence=None,
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        ts = collector.feedback[0]["timestamp"]
        assert ts.tzinfo is not None

    def test_collect_multiple(self) -> None:
        """Should accumulate multiple feedback items."""
        collector = FeedbackCollector()
        for i in range(5):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
            )
        assert collector.count() == 5


class TestFeedbackCollectorCapacity:
    """Test deque maxlen eviction behavior."""

    def test_capacity_cap(self) -> None:
        """Should not exceed capacity; oldest evicted first."""
        collector = FeedbackCollector(capacity=3)
        for i in range(5):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
            )
        assert collector.count() == 3
        assert collector.feedback[0]["incident_id"] == "inc_2"

    def test_default_capacity(self) -> None:
        """Default capacity should match config."""
        collector = FeedbackCollector()
        assert collector.capacity == ACTIVE_LEARNING_FEEDBACK_CAPACITY

    def test_invalid_capacity_zero(self) -> None:
        """Should reject capacity <= 0."""
        with pytest.raises(ValueError, match="positive integer"):
            FeedbackCollector(capacity=0)

    def test_invalid_capacity_negative(self) -> None:
        """Should reject negative capacity."""
        with pytest.raises(ValueError, match="positive integer"):
            FeedbackCollector(capacity=-1)


class TestFeedbackCollectorGetRecent:
    """Test FeedbackCollector.get_recent."""

    def test_get_recent_all(self) -> None:
        """All recent entries within window."""
        collector = FeedbackCollector()
        for i in range(3):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
            )
        recent = collector.get_recent(days=1)
        assert len(recent) == 3

    def test_get_recent_empty(self) -> None:
        """Empty collector returns empty list."""
        collector = FeedbackCollector()
        assert collector.get_recent(days=30) == []

    def test_get_recent_zero_days(self) -> None:
        """Zero-day window may exclude items due to sub-ms timing."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_001",
            sequence=None,
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        # days=0 means cutoff = now(), which can exclude items created
        # microseconds earlier. Verify it returns 0 or 1 (timing-safe).
        recent = collector.get_recent(days=0)
        assert len(recent) in (0, 1)
        # days=1 should always include recent items
        recent_1d = collector.get_recent(days=1)
        assert len(recent_1d) == 1


class TestFeedbackCollectorValidation:
    """Test input validation on collect."""

    def test_empty_incident_id(self) -> None:
        """Should reject empty incident_id."""
        collector = FeedbackCollector()
        with pytest.raises(ValueError, match="incident_id"):
            collector.collect(
                incident_id="",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
            )

    def test_whitespace_incident_id(self) -> None:
        """Should reject whitespace-only incident_id."""
        collector = FeedbackCollector()
        with pytest.raises(ValueError, match="incident_id"):
            collector.collect(
                incident_id="   ",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
            )

    def test_empty_operator_id(self) -> None:
        """Should reject empty operator_id."""
        collector = FeedbackCollector()
        with pytest.raises(ValueError, match="operator_id"):
            collector.collect(
                incident_id="inc_001",
                sequence=None,
                is_true_anomaly=True,
                operator_id="",
            )

    def test_operator_confidence_too_high(self) -> None:
        """Should reject operator_confidence > 1."""
        collector = FeedbackCollector()
        with pytest.raises(ValueError, match="operator_confidence"):
            collector.collect(
                incident_id="inc_001",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                operator_confidence=1.5,
            )

    def test_operator_confidence_negative(self) -> None:
        """Should reject operator_confidence < 0."""
        collector = FeedbackCollector()
        with pytest.raises(ValueError, match="operator_confidence"):
            collector.collect(
                incident_id="inc_001",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                operator_confidence=-0.1,
            )

    def test_severity_too_low(self) -> None:
        """Should reject severity < 1."""
        collector = FeedbackCollector()
        with pytest.raises(ValueError, match="severity"):
            collector.collect(
                incident_id="inc_001",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                severity=0,
            )

    def test_severity_too_high(self) -> None:
        """Should reject severity > 10."""
        collector = FeedbackCollector()
        with pytest.raises(ValueError, match="severity"):
            collector.collect(
                incident_id="inc_001",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                severity=11,
            )

    def test_severity_none_allowed(self) -> None:
        """Should allow severity=None (optional)."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_001",
            sequence=None,
            is_true_anomaly=True,
            operator_id="op_alice",
            severity=None,
        )
        assert collector.count() == 1


# =========================================================================
# FeedbackValidator
# =========================================================================
class TestFeedbackValidatorEMA:
    """Test EMA reliability updates."""

    def test_unknown_operator_returns_default(self) -> None:
        """Unknown operator should return default reliability."""
        validator = FeedbackValidator()
        r = validator.get_operator_reliability("unknown_op")
        assert r == ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT

    def test_single_correct_outcome(self) -> None:
        """One correct outcome should increase from default (dampened)."""
        validator = FeedbackValidator()
        validator.record_feedback_outcome("op_alice", True)
        r = validator.get_operator_reliability("op_alice")
        raw = (
            ACTIVE_LEARNING_OPERATOR_ALPHA * 1.0
            + (1.0 - ACTIVE_LEARNING_OPERATOR_ALPHA)
            * ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT
        )
        weight = 1 / ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES
        expected = (
            weight * raw
            + (1.0 - weight) * ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT
        )
        assert abs(r - expected) < 1e-9

    def test_single_incorrect_outcome(self) -> None:
        """One incorrect outcome should decrease from default (dampened)."""
        validator = FeedbackValidator()
        validator.record_feedback_outcome("op_alice", False)
        r = validator.get_operator_reliability("op_alice")
        raw = (
            ACTIVE_LEARNING_OPERATOR_ALPHA * 0.0
            + (1.0 - ACTIVE_LEARNING_OPERATOR_ALPHA)
            * ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT
        )
        weight = 1 / ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES
        expected = (
            weight * raw
            + (1.0 - weight) * ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT
        )
        assert abs(r - expected) < 1e-9

    def test_reliability_increases_with_correct_feedback(self) -> None:
        """Repeated correct outcomes should push reliability toward 1."""
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
        r = validator.get_operator_reliability("op_alice")
        assert r > 0.9

    def test_reliability_decreases_with_incorrect_feedback(self) -> None:
        """Repeated incorrect outcomes should push reliability toward 0."""
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_bob", False)
        r = validator.get_operator_reliability("op_bob")
        assert r < 0.1

    def test_ema_alpha_from_config(self) -> None:
        """EMA should use the config alpha value (dampened until min samples)."""
        validator = FeedbackValidator()
        validator.record_feedback_outcome("op_alice", True)
        validator.record_feedback_outcome("op_alice", False)
        # After True then False with default=0.5:
        # step1: 0.2*1 + 0.8*0.5 = 0.6
        # step2: 0.2*0 + 0.8*0.6 = 0.48
        raw = 0.48
        weight = 2 / ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES
        expected = (
            weight * raw
            + (1.0 - weight) * ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT
        )
        r = validator.get_operator_reliability("op_alice")
        assert abs(r - expected) < 1e-9

    def test_dampening_removed_at_min_samples(self) -> None:
        """At MIN_OPERATOR_SAMPLES outcomes, raw EMA is returned unchanged."""
        validator = FeedbackValidator()
        for _ in range(ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES):
            validator.record_feedback_outcome("op_alice", True)
        raw = validator._operator_accuracy["op_alice"]
        assert validator.get_operator_reliability("op_alice") == raw

    def test_empty_operator_id_raises(self) -> None:
        """Should reject empty operator_id."""
        validator = FeedbackValidator()
        with pytest.raises(ValueError, match="operator_id"):
            validator.record_feedback_outcome("", True)


class TestFeedbackValidatorBatch:
    """Test validate_batch."""

    def test_validate_batch_basic(self) -> None:
        """Should classify feedback by operator reliability."""
        validator = FeedbackValidator()
        # Make op_alice reliable
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)

        feedback_list = [
            {"operator_id": "op_alice"},
            {"operator_id": "op_unknown"},
        ]
        result = validator.validate_batch(feedback_list)
        assert result["total"] == 2
        assert result["high_quality"] == 1
        assert result["low_quality"] == 1
        assert "op_alice" in result["operator_stats"]
        assert "op_unknown" in result["operator_stats"]

    def test_validate_batch_empty(self) -> None:
        """Empty batch should return zeros."""
        validator = FeedbackValidator()
        result = validator.validate_batch([])
        assert result["total"] == 0
        assert result["high_quality"] == 0
        assert result["low_quality"] == 0

    def test_validate_batch_all_high_quality(self) -> None:
        """All operators reliable → all high quality."""
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
            validator.record_feedback_outcome("op_bob", True)

        feedback_list = [
            {"operator_id": "op_alice"},
            {"operator_id": "op_bob"},
        ]
        result = validator.validate_batch(feedback_list)
        assert result["high_quality"] == 2
        assert result["low_quality"] == 0

    def test_operator_ids_property(self) -> None:
        """Should track all operators that have outcomes."""
        validator = FeedbackValidator()
        validator.record_feedback_outcome("op_alice", True)
        validator.record_feedback_outcome("op_bob", False)
        ids = validator.operator_ids
        assert set(ids) == {"op_alice", "op_bob"}


# =========================================================================
# RetrainingDecisionMaker
# =========================================================================
class TestRetrainingDecisionMaker:
    """Test should_retrain and what_to_retrain."""

    def _build_ready_state(self):
        """Helper: build collector+validator that meet retrain criteria."""
        collector = FeedbackCollector()
        validator = FeedbackValidator()

        # Make operator reliable
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)

        # Add enough feedback with 2+ pattern types
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS):
            pattern = "drift" if i % 2 == 0 else "spike"
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed=pattern,
            )
        return collector, validator

    def test_should_retrain_false_insufficient_labels(self) -> None:
        """Should return False with < MIN_QUALITY_LABELS."""
        collector = FeedbackCollector()
        validator = FeedbackValidator()

        # Make operator reliable but only 5 labels
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
        for i in range(5):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift",
            )

        dm = RetrainingDecisionMaker()
        assert dm.should_retrain(collector, validator) is False

    def test_should_retrain_false_insufficient_pattern_types(self) -> None:
        """Should return False with < MIN_PATTERN_TYPES."""
        collector = FeedbackCollector()
        validator = FeedbackValidator()

        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift",  # Only one type
            )

        dm = RetrainingDecisionMaker()
        assert dm.should_retrain(collector, validator) is False

    def test_should_retrain_false_low_operator_accuracy(self) -> None:
        """Should return False when operator is unreliable."""
        collector = FeedbackCollector()
        validator = FeedbackValidator()

        # Unreliable operator
        for _ in range(20):
            validator.record_feedback_outcome("op_bob", False)
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_bob",
                pattern_type_observed="drift" if i % 2 == 0 else "spike",
            )

        dm = RetrainingDecisionMaker()
        assert dm.should_retrain(collector, validator) is False

    def test_should_retrain_true_all_criteria_met(self) -> None:
        """Should return True when all criteria are met."""
        collector, validator = self._build_ready_state()
        dm = RetrainingDecisionMaker()
        assert dm.should_retrain(collector, validator) is True

    def test_what_to_retrain_returns_phase_1(self) -> None:
        """Should return Phase 1 low-risk plan."""
        dm = RetrainingDecisionMaker()
        plan = dm.what_to_retrain()
        assert plan["phase"] == 1
        assert plan["risk_level"] == "low"
        assert "ensemble_weights" in plan["components"]
        assert "pattern_thresholds" in plan["components"]


# =========================================================================
# ActiveLearner
# =========================================================================
class TestActiveLearnerConstruction:
    """Test ActiveLearner construction."""

    def test_valid_construction(self) -> None:
        """Should construct with a valid customer_id."""
        learner = ActiveLearner(customer_id="cust_01")
        assert learner.customer_id == "cust_01"

    def test_empty_customer_id_raises(self) -> None:
        """Should reject empty customer_id."""
        with pytest.raises(ValueError, match="customer_id"):
            ActiveLearner(customer_id="")

    def test_whitespace_customer_id_raises(self) -> None:
        """Should reject whitespace-only customer_id."""
        with pytest.raises(ValueError, match="customer_id"):
            ActiveLearner(customer_id="   ")


class TestActiveLearnerCollect:
    """Test collect_operator_feedback."""

    def test_collect_returns_correct_dict(self) -> None:
        """Should return status, learning_triggered, counts."""
        learner = ActiveLearner(customer_id="cust_01")
        result = learner.collect_operator_feedback(
            incident_id="inc_001",
            is_true_anomaly=True,
            operator_id="op_alice",
            pattern_type_observed="drift",
        )
        assert result["status"] == "feedback_recorded"
        assert isinstance(result["learning_triggered"], bool)
        assert result["feedback_count"] == 1
        assert isinstance(result["high_quality_count"], int)

    def test_learning_triggered_false_below_threshold(self) -> None:
        """Should not trigger learning with few labels."""
        learner = ActiveLearner(customer_id="cust_01")
        result = learner.collect_operator_feedback(
            incident_id="inc_001",
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        assert result["learning_triggered"] is False

    def test_learning_triggered_true_above_threshold(self) -> None:
        """Should trigger learning and auto-retrain when criteria met."""
        learner = ActiveLearner(customer_id="cust_01")

        # Make operator reliable
        for _ in range(20):
            learner.validator.record_feedback_outcome("op_alice", True)

        # Add enough diverse feedback
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS - 1):
            learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift" if i % 2 == 0 else "spike",
            )

        # The final one should trigger learning AND auto-retrain
        result = learner.collect_operator_feedback(
            incident_id=f"inc_{ACTIVE_LEARNING_MIN_QUALITY_LABELS}",
            is_true_anomaly=True,
            operator_id="op_alice",
            pattern_type_observed="spike",
        )
        assert result["learning_triggered"] is True
        # Auto-retraining should have been called
        assert "retraining" in result
        assert result["retraining"]["status"] == "retraining_completed"
        # Cooldown should now be set
        assert learner._last_retrain is not None

    def test_collect_propagates_validation_errors(self) -> None:
        """Should raise ValueError on invalid inputs."""
        learner = ActiveLearner(customer_id="cust_01")
        with pytest.raises(ValueError, match="incident_id"):
            learner.collect_operator_feedback(
                incident_id="",
                is_true_anomaly=True,
                operator_id="op_alice",
            )


class TestActiveLearnerRetraining:
    """Test trigger_retraining."""

    def test_trigger_retraining_insufficient_data(self) -> None:
        """Should return insufficient_data with few samples."""
        learner = ActiveLearner(customer_id="cust_01")
        result = learner.trigger_retraining()
        assert result["status"] == "insufficient_data"

    def test_trigger_retraining_success(self) -> None:
        """Should succeed with enough high-quality data."""
        learner = ActiveLearner(customer_id="cust_01")

        # Make operator reliable
        for _ in range(20):
            learner.validator.record_feedback_outcome("op_alice", True)

        # Add enough feedback
        for i in range(ACTIVE_LEARNING_MIN_TRAINING_SAMPLES + 5):
            learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift",
            )

        result = learner.trigger_retraining()
        assert result["status"] == "retraining_completed"
        assert result["phase"] == 1
        assert result["samples_used"] >= ACTIVE_LEARNING_MIN_TRAINING_SAMPLES

    def test_retraining_updates_last_retrain(self) -> None:
        """Should update last_retrain timestamp."""
        learner = ActiveLearner(customer_id="cust_01")

        for _ in range(20):
            learner.validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_TRAINING_SAMPLES + 5):
            learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift",
            )

        learner.trigger_retraining()
        assert learner._last_retrain is not None


class TestActiveLearnerStatistics:
    """Test get_statistics."""

    def test_statistics_fields(self) -> None:
        """Should return all expected fields."""
        learner = ActiveLearner(customer_id="cust_01")
        stats = learner.get_statistics()
        assert stats["customer_id"] == "cust_01"
        assert stats["total_feedback"] == 0
        assert stats["high_quality_feedback"] == 0
        assert stats["operator_count"] == 0
        assert stats["retrainings_completed"] == 0
        assert stats["last_retrain"] is None
        assert isinstance(stats["operator_reliability"], dict)

    def test_statistics_after_feedback(self) -> None:
        """Should reflect collected feedback."""
        learner = ActiveLearner(customer_id="cust_01")
        for i in range(5):
            learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
            )
        stats = learner.get_statistics()
        assert stats["total_feedback"] == 5
        assert stats["operator_count"] == 1

    def test_statistics_multiple_operators(self) -> None:
        """Should track distinct operators."""
        learner = ActiveLearner(customer_id="cust_01")
        learner.collect_operator_feedback(
            incident_id="inc_001",
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        learner.collect_operator_feedback(
            incident_id="inc_002",
            is_true_anomaly=False,
            operator_id="op_bob",
        )
        stats = learner.get_statistics()
        assert stats["operator_count"] == 2


# =========================================================================
# RetrainingOrchestrator
# =========================================================================
class TestRetrainingOrchestratorObserve:
    """Test RetrainingOrchestrator.observe."""

    def test_observe_accumulates_feedback(self) -> None:
        """Should accumulate feedback through learner."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        for i in range(5):
            orch.observe(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
            )
        stats = orch.learner.get_statistics()
        assert stats["total_feedback"] == 5

    def test_observe_returns_expected_keys(self) -> None:
        """Should return status, learning_triggered, counts."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        result = orch.observe(
            incident_id="inc_001",
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        assert "status" in result
        assert "learning_triggered" in result
        assert "feedback_count" in result
        assert "high_quality_count" in result

    def test_observe_triggers_retraining(self) -> None:
        """Should include retraining result when triggered."""
        orch = RetrainingOrchestrator(customer_id="cust_01")

        # Make operator reliable
        for _ in range(20):
            orch.learner.validator.record_feedback_outcome("op_alice", True)

        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS - 1):
            orch.observe(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift" if i % 2 == 0 else "spike",
            )

        result = orch.observe(
            incident_id=f"inc_{ACTIVE_LEARNING_MIN_QUALITY_LABELS}",
            is_true_anomaly=True,
            operator_id="op_alice",
            pattern_type_observed="spike",
        )
        assert result["learning_triggered"] is True
        assert "retraining" in result


class TestRetrainingOrchestratorProgress:
    """Test get_progress."""

    def test_progress_percent_zero(self) -> None:
        """No feedback → 0% progress."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        progress = orch.get_progress()
        assert progress["progress_percent"] == 0.0
        assert progress["customer_id"] == "cust_01"

    def test_progress_percent_calculation(self) -> None:
        """Should calculate correct percent."""
        orch = RetrainingOrchestrator(customer_id="cust_01")

        # Make operator reliable
        for _ in range(20):
            orch.learner.validator.record_feedback_outcome("op_alice", True)

        target = ACTIVE_LEARNING_MIN_QUALITY_LABELS
        halfway = target // 2
        for i in range(halfway):
            orch.observe(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift",
            )

        progress = orch.get_progress()
        expected_pct = (halfway / target) * 100.0
        assert abs(progress["progress_percent"] - expected_pct) < 0.1
        assert progress["samples_needed"] == target - halfway

    def test_progress_percent_capped_at_100(self) -> None:
        """Should never exceed 100%."""
        orch = RetrainingOrchestrator(customer_id="cust_01")

        for _ in range(20):
            orch.learner.validator.record_feedback_outcome("op_alice", True)

        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS + 10):
            orch.observe(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift" if i % 2 == 0 else "spike",
            )

        progress = orch.get_progress()
        assert progress["progress_percent"] <= 100.0


class TestRetrainingOrchestratorValidateAndPromote:
    """Test validate_and_promote."""

    def test_promote_at_threshold(self) -> None:
        """Should promote when improvement == threshold."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        result = orch.validate_and_promote(
            ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD
        )
        assert result["action"] == "promote"

    def test_promote_above_threshold(self) -> None:
        """Should promote when improvement > threshold."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        result = orch.validate_and_promote(0.10)
        assert result["action"] == "promote"
        assert result["improvement_pct"] == 0.10

    def test_rollback_below_threshold(self) -> None:
        """Should rollback when improvement < threshold."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        result = orch.validate_and_promote(0.01)
        assert result["action"] == "rollback"

    def test_rollback_zero_improvement(self) -> None:
        """Should rollback at 0% improvement."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        result = orch.validate_and_promote(0.0)
        assert result["action"] == "rollback"

    def test_negative_improvement_raises(self) -> None:
        """Should reject negative improvement."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        with pytest.raises(ValueError, match="model_improvement_pct"):
            orch.validate_and_promote(-0.1)

    def test_non_numeric_improvement_raises(self) -> None:
        """Should reject non-numeric improvement."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        with pytest.raises(ValueError, match="model_improvement_pct"):
            orch.validate_and_promote("high")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bad_value",
        [float("inf"), float("-inf"), float("nan")],
    )
    def test_non_finite_improvement_raises(self, bad_value: float) -> None:
        """Should reject inf/nan improvement values."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        with pytest.raises(ValueError, match="finite"):
            orch.validate_and_promote(bad_value)


class TestRetrainingOrchestratorRetainPlan:
    """Test get_retrain_plan."""

    def test_retrain_plan(self) -> None:
        """Should return Phase 1 plan."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        plan = orch.get_retrain_plan()
        assert plan["phase"] == 1
        assert plan["risk_level"] == "low"


# =========================================================================
# FeedbackCollector — additional edge cases
# =========================================================================
class TestFeedbackCollectorAdditionalEdgeCases:
    """Extra boundary / edge-case coverage for FeedbackCollector."""

    def test_invalid_capacity_float_raises(self) -> None:
        """Should reject float capacity (not an int)."""
        with pytest.raises(ValueError, match="positive integer"):
            FeedbackCollector(capacity=1.5)  # type: ignore[arg-type]

    def test_invalid_capacity_string_raises(self) -> None:
        """Should reject string capacity."""
        with pytest.raises(ValueError, match="positive integer"):
            FeedbackCollector(capacity="100")  # type: ignore[arg-type]

    def test_collect_confidence_boundary_zero_accepted(self) -> None:
        """operator_confidence = 0.0 is a valid boundary value."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_001",
            sequence=None,
            is_true_anomaly=False,
            operator_id="op_alice",
            operator_confidence=0.0,
        )
        assert collector.count() == 1

    def test_collect_confidence_boundary_one_accepted(self) -> None:
        """operator_confidence = 1.0 is a valid boundary value."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_001",
            sequence=None,
            is_true_anomaly=False,
            operator_id="op_alice",
            operator_confidence=1.0,
        )
        assert collector.count() == 1

    def test_collect_severity_boundary_one_accepted(self) -> None:
        """severity = 1 is the valid lower boundary."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_001",
            sequence=None,
            is_true_anomaly=True,
            operator_id="op_alice",
            severity=1,
        )
        assert collector.count() == 1

    def test_collect_severity_boundary_ten_accepted(self) -> None:
        """severity = 10 is the valid upper boundary."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_001",
            sequence=None,
            is_true_anomaly=True,
            operator_id="op_alice",
            severity=10,
        )
        assert collector.count() == 1

    def test_collect_whitespace_operator_id_raises(self) -> None:
        """Should reject whitespace-only operator_id."""
        collector = FeedbackCollector()
        with pytest.raises(ValueError, match="operator_id"):
            collector.collect(
                incident_id="inc_001",
                sequence=None,
                is_true_anomaly=True,
                operator_id="   ",
            )

    def test_feedback_property_returns_tuple(self) -> None:
        """feedback property should return an immutable tuple snapshot."""
        collector = FeedbackCollector()
        assert isinstance(collector.feedback, tuple)

    def test_capacity_overflow_retains_most_recent(self) -> None:
        """After overflow, exactly the most-recent N items should be kept."""
        collector = FeedbackCollector(capacity=3)
        for i in range(10):
            collector.collect(
                incident_id=f"inc_{i:02d}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
            )
        assert collector.count() == 3
        ids = [item["incident_id"] for item in collector.feedback]
        assert ids == ["inc_07", "inc_08", "inc_09"]

    def test_get_recent_excludes_old_entries(self) -> None:
        """Entries older than the window should be excluded from get_recent."""
        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_fresh",
            sequence=None,
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        # Artificially back-date the single entry to 60 days ago
        collector.feedback[0]["timestamp"] = datetime.now(timezone.utc) - timedelta(
            days=60
        )
        # 30-day window should exclude it
        assert collector.get_recent(days=30) == []
        # 90-day window should include it
        assert len(collector.get_recent(days=90)) == 1


# =========================================================================
# FeedbackValidator — additional coverage: get_operator_feedback_count
# and duplicate-operator branch in validate_batch
# =========================================================================
class TestFeedbackValidatorFeedbackCount:
    """Tests for get_operator_feedback_count (line 106 in feedback_validator.py)."""

    def test_count_zero_for_unknown_operator(self) -> None:
        """Unknown operator should return 0 feedback count."""
        validator = FeedbackValidator()
        assert validator.get_operator_feedback_count("op_unknown") == 0

    def test_count_increments_with_each_outcome(self) -> None:
        """Count should increment with every recorded outcome."""
        validator = FeedbackValidator()
        validator.record_feedback_outcome("op_alice", True)
        assert validator.get_operator_feedback_count("op_alice") == 1
        validator.record_feedback_outcome("op_alice", False)
        assert validator.get_operator_feedback_count("op_alice") == 2
        validator.record_feedback_outcome("op_alice", True)
        assert validator.get_operator_feedback_count("op_alice") == 3

    def test_count_independent_per_operator(self) -> None:
        """Counts must be tracked independently per operator."""
        validator = FeedbackValidator()
        for _ in range(5):
            validator.record_feedback_outcome("op_alice", True)
        validator.record_feedback_outcome("op_bob", False)
        assert validator.get_operator_feedback_count("op_alice") == 5
        assert validator.get_operator_feedback_count("op_bob") == 1


class TestFeedbackValidatorDuplicateOperatorInBatch:
    """Test validate_batch branch where operator already has a stats entry."""

    def test_duplicate_operator_in_batch_single_stats_entry(self) -> None:
        """Same operator appearing twice yields only one operator_stats entry."""
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
        feedback_list = [
            {"operator_id": "op_alice"},
            {"operator_id": "op_alice"},  # duplicate — hits the else branch
        ]
        result = validator.validate_batch(feedback_list)
        assert result["total"] == 2
        assert result["high_quality"] == 2  # both counted
        assert result["low_quality"] == 0
        assert len(result["operator_stats"]) == 1  # deduplicated

    def test_duplicate_low_quality_operator_in_batch(self) -> None:
        """Duplicate low-quality operator counts both items as low_quality."""
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_bob", False)
        feedback_list = [
            {"operator_id": "op_bob"},
            {"operator_id": "op_bob"},
        ]
        result = validator.validate_batch(feedback_list)
        assert result["total"] == 2
        assert result["high_quality"] == 0
        assert result["low_quality"] == 2
        assert len(result["operator_stats"]) == 1

    def test_batch_operator_stats_includes_count(self) -> None:
        """operator_stats should include the recorded outcome count."""
        validator = FeedbackValidator()
        validator.record_feedback_outcome("op_alice", True)
        validator.record_feedback_outcome("op_alice", True)
        result = validator.validate_batch([{"operator_id": "op_alice"}])
        assert result["operator_stats"]["op_alice"]["count"] == 2

    def test_batch_mixed_reliable_unreliable(self) -> None:
        """Batch with both reliable and unreliable operators classified correctly."""
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_reliable", True)
            validator.record_feedback_outcome("op_unreliable", False)
        feedback_list = [
            {"operator_id": "op_reliable"},
            {"operator_id": "op_unreliable"},
            {"operator_id": "op_reliable"},  # duplicate to hit else branch
        ]
        result = validator.validate_batch(feedback_list)
        assert result["total"] == 3
        assert result["high_quality"] == 2
        assert result["low_quality"] == 1

    def test_validate_batch_feedback_without_operator_id_key(self) -> None:
        """Items missing 'operator_id' key use empty string default → low quality."""
        validator = FeedbackValidator()
        result = validator.validate_batch([{}])  # no operator_id key
        assert result["total"] == 1
        assert result["low_quality"] == 1


# =========================================================================
# RetrainingDecisionMaker — additional boundary / structure tests
# =========================================================================
class TestRetrainingDecisionMakerBoundary:
    """Boundary conditions and structure checks for RetrainingDecisionMaker."""

    def test_exactly_min_quality_labels_triggers_retrain(self) -> None:
        """Exactly MIN_QUALITY_LABELS should satisfy the label count gate."""
        collector = FeedbackCollector()
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift" if i % 2 == 0 else "spike",
            )
        dm = RetrainingDecisionMaker()
        assert dm.should_retrain(collector, validator) is True

    def test_one_below_min_quality_labels_does_not_retrain(self) -> None:
        """One fewer than MIN_QUALITY_LABELS must not trigger retrain."""
        collector = FeedbackCollector()
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS - 1):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift" if i % 2 == 0 else "spike",
            )
        dm = RetrainingDecisionMaker()
        assert dm.should_retrain(collector, validator) is False

    def test_exactly_two_pattern_types_satisfies_diversity_gate(self) -> None:
        """Exactly 2 distinct pattern types must satisfy MIN_PATTERN_TYPES."""
        collector = FeedbackCollector()
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="typeA" if i % 2 == 0 else "typeB",
            )
        dm = RetrainingDecisionMaker()
        assert dm.should_retrain(collector, validator) is True

    def test_none_pattern_type_excluded_from_diversity_count(self) -> None:
        """Entries with pattern_type=None must not count toward pattern diversity."""
        collector = FeedbackCollector()
        validator = FeedbackValidator()
        for _ in range(20):
            validator.record_feedback_outcome("op_alice", True)
        # All entries have pattern_type=None → 0 distinct types < MIN_PATTERN_TYPES
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS):
            collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed=None,
            )
        dm = RetrainingDecisionMaker()
        assert dm.should_retrain(collector, validator) is False

    def test_what_to_retrain_full_structure(self) -> None:
        """what_to_retrain should include all required keys with correct types."""
        dm = RetrainingDecisionMaker()
        plan = dm.what_to_retrain()
        assert isinstance(plan["components"], list)
        assert len(plan["components"]) == 2
        assert "ensemble_weights" in plan["components"]
        assert "pattern_thresholds" in plan["components"]
        assert isinstance(plan.get("description"), str)
        assert len(plan["description"]) > 0


# =========================================================================
# ActiveLearner — cooldown expiry branch + extra coverage
# =========================================================================
class TestActiveLearnerCooldown:
    """Test the 4-hour cooldown gate in _should_retrain_now."""

    def _make_retrain_ready_learner(self) -> ActiveLearner:
        """Build a learner whose feedback meets all retrain criteria."""
        learner = ActiveLearner(customer_id="cust_01")
        for _ in range(20):
            learner.validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS):
            learner.feedback_collector.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift" if i % 2 == 0 else "spike",
            )
        return learner

    def test_cooldown_prevents_immediate_second_retrain(self) -> None:
        """Cooldown active (last_retrain=now) should block _should_retrain_now."""
        learner = self._make_retrain_ready_learner()
        # Simulate a very recent retrain
        learner._last_retrain = datetime.now(timezone.utc)
        assert learner._should_retrain_now() is False

    def test_cooldown_expired_allows_retrain(self) -> None:
        """Expired cooldown (last_retrain > interval hours ago) must not block."""
        learner = self._make_retrain_ready_learner()
        # Set last_retrain to well past the cooldown window
        past = datetime.now(timezone.utc) - timedelta(
            hours=ACTIVE_LEARNING_MIN_RETRAIN_INTERVAL_HOURS + 1
        )
        learner._last_retrain = past  # type: ignore[assignment]
        assert learner._should_retrain_now() is True  # branch 255->258 covered

    def test_no_prior_retrain_skips_cooldown_check(self) -> None:
        """When last_retrain is None the cooldown branch should be skipped."""
        learner = self._make_retrain_ready_learner()
        assert learner._last_retrain is None
        # Criteria are met and no cooldown → should retrain
        assert learner._should_retrain_now() is True


class TestActiveLearnerFeedbackCountTracking:
    """Test feedback_count increments and cross-operator tracking."""

    def test_feedback_count_increments_on_each_call(self) -> None:
        """feedback_count in the return dict should equal total collected."""
        learner = ActiveLearner(customer_id="cust_01")
        for i in range(7):
            result = learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
            )
        assert result["feedback_count"] == 7

    def test_feedback_count_aggregates_across_operators(self) -> None:
        """All operator feedback should be reflected in feedback_count."""
        learner = ActiveLearner(customer_id="cust_01")
        for i, op in enumerate(["op_a", "op_b", "op_c"]):
            result = learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id=op,
            )
        assert result["feedback_count"] == 3

    def test_statistics_retrainings_completed_increments(self) -> None:
        """retrainings_completed should increment by 1 per trigger_retraining call."""
        learner = ActiveLearner(customer_id="cust_01")
        for _ in range(20):
            learner.validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_TRAINING_SAMPLES + 5):
            learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift",
            )
        learner.trigger_retraining()
        stats = learner.get_statistics()
        assert stats["retrainings_completed"] == 1
        assert stats["last_retrain"] is not None

    def test_statistics_operator_reliability_populated_for_feedback_ops(self) -> None:
        """operator_reliability dict must include operators who submitted feedback."""
        learner = ActiveLearner(customer_id="cust_01")
        learner.collect_operator_feedback(
            incident_id="inc_001",
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        stats = learner.get_statistics()
        assert "op_alice" in stats["operator_reliability"]

    def test_statistics_retains_evicted_operator_from_validator(self) -> None:
        """Evicted buffer entries must not drop validator-tracked operators."""
        learner = ActiveLearner(customer_id="cust_01")
        small = FeedbackCollector(capacity=2)
        learner.feedback_collector = small
        learner.validator.record_feedback_outcome("op_evicted", True)
        for i in range(5):
            small.collect(
                incident_id=f"inc_{i}",
                sequence=None,
                is_true_anomaly=True,
                operator_id=f"op_other_{i}",
            )
        stats = learner.get_statistics()
        assert "op_evicted" in stats["operator_reliability"]
        assert stats["operator_reliability"]["op_evicted"] > 0.5

    def test_trigger_retraining_next_eligible_field_present(self) -> None:
        """Successful retraining result must include next_retrain_eligible."""
        learner = ActiveLearner(customer_id="cust_01")
        for _ in range(20):
            learner.validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_TRAINING_SAMPLES + 5):
            learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift",
            )
        result = learner.trigger_retraining()
        assert result["status"] == "retraining_completed"
        assert "next_retrain_eligible" in result
        assert isinstance(result["next_retrain_eligible"], str)

    def test_trigger_retraining_samples_capped_at_max(self) -> None:
        """samples_used must never exceed ACTIVE_LEARNING_MAX_TRAINING_SAMPLES."""
        from src.config import ACTIVE_LEARNING_MAX_TRAINING_SAMPLES
        learner = ActiveLearner(customer_id="cust_01")
        for _ in range(20):
            learner.validator.record_feedback_outcome("op_alice", True)
        # Add more samples than the cap
        for i in range(ACTIVE_LEARNING_MAX_TRAINING_SAMPLES + 50):
            learner.collect_operator_feedback(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift",
            )
        result = learner.trigger_retraining()
        assert result["samples_used"] <= ACTIVE_LEARNING_MAX_TRAINING_SAMPLES


# =========================================================================
# RetrainingOrchestrator — additional edge cases
# =========================================================================
class TestRetrainingOrchestratorEdgeCases:
    """Edge cases and structural checks for RetrainingOrchestrator."""

    def test_empty_customer_id_raises(self) -> None:
        """Should propagate ValueError for empty customer_id."""
        with pytest.raises(ValueError, match="customer_id"):
            RetrainingOrchestrator(customer_id="")

    def test_whitespace_customer_id_raises(self) -> None:
        """Should propagate ValueError for whitespace-only customer_id."""
        with pytest.raises(ValueError, match="customer_id"):
            RetrainingOrchestrator(customer_id="   ")

    def test_learner_property_returns_active_learner_instance(self) -> None:
        """learner property should expose the underlying ActiveLearner."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        assert isinstance(orch.learner, ActiveLearner)
        assert orch.learner.customer_id == "cust_01"

    def test_get_progress_fields_all_present(self) -> None:
        """get_progress must contain every documented field."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        progress = orch.get_progress()
        expected_keys = {
            "customer_id",
            "progress_percent",
            "samples_needed",
            "high_quality_count",
            "total_feedback",
            "last_retrain",
        }
        assert expected_keys.issubset(progress.keys())

    def test_get_progress_samples_needed_zero_when_target_met(self) -> None:
        """samples_needed must be 0 when target is met or exceeded."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        for _ in range(20):
            orch.learner.validator.record_feedback_outcome("op_alice", True)
        for i in range(ACTIVE_LEARNING_MIN_QUALITY_LABELS + 5):
            orch.observe(
                incident_id=f"inc_{i}",
                is_true_anomaly=True,
                operator_id="op_alice",
                pattern_type_observed="drift" if i % 2 == 0 else "spike",
            )
        progress = orch.get_progress()
        assert progress["samples_needed"] == 0

    def test_get_retrain_plan_full_structure(self) -> None:
        """get_retrain_plan must return all expected keys and correct types."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        plan = orch.get_retrain_plan()
        assert plan["phase"] == 1
        assert plan["risk_level"] == "low"
        assert "components" in plan
        assert isinstance(plan["components"], list)
        assert "description" in plan
        assert isinstance(plan["description"], str)

    def test_validate_and_promote_returns_all_fields(self) -> None:
        """validate_and_promote result must contain all documented fields."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        for improvement in (0.10, 0.01):
            result = orch.validate_and_promote(improvement)
            assert "action" in result
            assert "improvement_pct" in result
            assert "threshold" in result
            assert "reason" in result

    def test_validate_and_promote_exactly_at_5pct_boundary(self) -> None:
        """At exactly the threshold (5%) the model must be promoted."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        threshold = ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD
        result = orch.validate_and_promote(threshold)
        assert result["action"] == "promote"
        assert result["improvement_pct"] == threshold
        assert result["threshold"] == threshold

    def test_validate_and_promote_just_below_5pct_rolls_back(self) -> None:
        """Just below threshold (4.99%) must roll back."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        threshold = ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD
        result = orch.validate_and_promote(threshold - 0.0001)
        assert result["action"] == "rollback"

    def test_observe_invalid_incident_id_raises(self) -> None:
        """observe must propagate ValueError for empty incident_id."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        with pytest.raises(ValueError, match="incident_id"):
            orch.observe(
                incident_id="",
                is_true_anomaly=True,
                operator_id="op_alice",
            )

    def test_observe_with_all_optional_fields(self) -> None:
        """observe should succeed when all optional parameters are provided."""
        orch = RetrainingOrchestrator(customer_id="cust_01")
        result = orch.observe(
            incident_id="inc_full",
            is_true_anomaly=True,
            operator_id="op_alice",
            pattern_type_observed="drift",
            actual_root_cause="sensor_noise",
            operator_confidence=0.95,
            severity=7,
            notes="Cross-check with manual log",
        )
        assert result["status"] == "feedback_recorded"
        assert result["feedback_count"] == 1
