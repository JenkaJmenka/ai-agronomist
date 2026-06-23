"""
Active Learning: close the operator feedback loop.

:class:`RetrainingDecisionMaker` decides WHEN/WHAT to retrain.
:class:`ActiveLearner` orchestrates the full per-customer cycle.
All thresholds from :mod:`src.config` (``ACTIVE_LEARNING_*``).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.config import (
    ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE,
    ACTIVE_LEARNING_FEEDBACK_CAPACITY,
    ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS,
    ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD,
    ACTIVE_LEARNING_MAX_TRAINING_SAMPLES,
    ACTIVE_LEARNING_MIN_PATTERN_TYPES,
    ACTIVE_LEARNING_MIN_QUALITY_LABELS,
    ACTIVE_LEARNING_MIN_RETRAIN_INTERVAL_HOURS,
    ACTIVE_LEARNING_MIN_TRAINING_SAMPLES,
    ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD,
)
from src.models.customer_config_store import get_config_store
from src.models.feedback_collector import FeedbackCollector
from src.models.feedback_validator import FeedbackValidator
from src.models.retrain_evaluator import RetrainEvaluator
from src.models.retrain_executor import RetrainExecutor

logger = logging.getLogger(__name__)


class RetrainingDecisionMaker:
    """Decide when and what to retrain.

    Criteria: 20+ high-quality labels, 2+ pattern types, operator
    reliability >= threshold. Phase 1 = ensemble weights + thresholds.
    """

    def should_retrain(
        self,
        feedback_collector: FeedbackCollector,
        validator: FeedbackValidator,
    ) -> bool:
        """Check whether retraining criteria are met.

        Args:
            feedback_collector: Source of recent operator feedback.
            validator: Operator reliability tracker.

        Returns:
            ``True`` if all criteria are satisfied.
        """
        recent = feedback_collector.get_recent(
            days=ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS
        )

        high_quality = [
            f
            for f in recent
            if validator.get_operator_reliability(f.get("operator_id", ""))
            >= ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD
        ]

        if len(high_quality) < ACTIVE_LEARNING_MIN_QUALITY_LABELS:
            return False

        pattern_types = {
            f["pattern_type"]
            for f in high_quality
            if f.get("pattern_type")
        }

        if len(pattern_types) < ACTIVE_LEARNING_MIN_PATTERN_TYPES:
            return False

        return True

    def what_to_retrain(self) -> Dict[str, Any]:
        """Return the Phase 1 retrain plan (low risk).

        Returns:
            Dict describing which components to retrain and why.
        """
        return {
            "phase": 1,
            "risk_level": "low",
            "components": ["ensemble_weights", "pattern_thresholds"],
            "description": (
                "Retrain ensemble detector weights and pattern "
                "thresholds only"
            ),
        }


class ActiveLearner:
    """Per-customer active learning orchestrator."""

    def __init__(self, customer_id: str) -> None:
        """
        Args:
            customer_id: Non-empty customer identifier.

        Raises:
            ValueError: If ``customer_id`` is empty.
        """
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError(
                "customer_id must be a non-empty string, "
                f"got {customer_id!r}"
            )

        self.customer_id: str = customer_id
        self.feedback_collector: FeedbackCollector = FeedbackCollector(
            capacity=ACTIVE_LEARNING_FEEDBACK_CAPACITY
        )
        self.validator: FeedbackValidator = FeedbackValidator()
        self.decision_maker: RetrainingDecisionMaker = (
            RetrainingDecisionMaker()
        )
        self._retraining_history: List[Dict[str, Any]] = []
        self._last_retrain: Optional[datetime] = None

    def collect_operator_feedback(
        self,
        incident_id: str,
        is_true_anomaly: bool,
        operator_id: str,
        pattern_type_observed: Optional[str] = None,
        actual_root_cause: Optional[str] = None,
        operator_confidence: float = ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE,
        severity: Optional[int] = None,
        notes: Optional[str] = None,
        sequence: Optional[Any] = None,
        detector_scores: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Record operator feedback and auto-retrain when criteria are met.

        When all retraining criteria are satisfied (sufficient high-quality
        labels, pattern diversity, operator reliability, and cooldown
        expired), retraining is triggered automatically.  The result dict
        includes the ``retraining`` key with the retraining outcome when
        this happens.

        Args:
            incident_id: Non-empty incident identifier.
            is_true_anomaly: Operator verdict.
            operator_id: Non-empty operator identifier.
            pattern_type_observed: Pattern the operator identified.
            actual_root_cause: Root cause the operator believes is correct.
            operator_confidence: Self-assessed confidence [0, 1].
            severity: Optional 1-10 scale.
            notes: Free-text context.

        Returns:
            Dict with ``status``, ``learning_triggered``,
            ``feedback_count``, ``high_quality_count``, and optionally
            ``retraining`` when learning was triggered.

        Raises:
            ValueError: On invalid inputs (delegated to FeedbackCollector).
        """
        self.feedback_collector.collect(
            incident_id=incident_id,
            sequence=sequence,
            is_true_anomaly=is_true_anomaly,
            operator_id=operator_id,
            pattern_type_observed=pattern_type_observed,
            actual_root_cause=actual_root_cause,
            severity=severity,
            operator_confidence=operator_confidence,
            notes=notes,
            detector_scores=detector_scores,
        )

        is_learning_triggered = self._should_retrain_now()

        result: Dict[str, Any] = {
            "status": "feedback_recorded",
            "learning_triggered": is_learning_triggered,
            "feedback_count": self.feedback_collector.count(),
            "high_quality_count": self._count_high_quality(),
        }

        if is_learning_triggered:
            retrain_result = self.trigger_retraining()
            result["retraining"] = retrain_result

        return result

    def trigger_retraining(self) -> Dict[str, Any]:
        """Execute Phase 1 retraining with promote/rollback."""
        training_data = self._get_training_feedback()

        if len(training_data) < ACTIVE_LEARNING_MIN_TRAINING_SAMPLES:
            logger.warning(
                "[%s] Not enough training data: %d (need %d)",
                self.customer_id,
                len(training_data),
                ACTIVE_LEARNING_MIN_TRAINING_SAMPLES,
            )
            return {
                "status": "insufficient_data",
                "samples_available": len(training_data),
                "samples_needed": ACTIVE_LEARNING_MIN_TRAINING_SAMPLES,
            }

        config_store = get_config_store()
        current = config_store.get(self.customer_id)
        before = RetrainEvaluator.evaluate(current, training_data)
        proposed = RetrainExecutor.apply_phase1(training_data, current)
        after = RetrainEvaluator.evaluate(proposed, training_data)
        improvement = RetrainEvaluator.improvement_pct(before, after)

        if improvement >= ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD:
            config_store.promote(self.customer_id, proposed, snapshot=current)
            action = "promote"
        else:
            config_store.rollback(self.customer_id)
            action = "rollback"

        logger.info(
            "[%s] Retraining on %d samples (Phase 1) — %s (%.1f%% FP improvement)",
            self.customer_id,
            len(training_data),
            action,
            improvement * 100,
        )

        now = datetime.now(timezone.utc)
        retrain_info: Dict[str, Any] = {
            "phase": 1,
            "training_samples": len(training_data),
            "timestamp": now.isoformat(),
            "status": "completed",
            "action": action,
            "improvement_pct": round(improvement, 4),
            "before_metrics": before,
            "after_metrics": after,
            "proposed_config": proposed,
        }

        self._retraining_history.append(retrain_info)
        self._last_retrain = now

        next_eligible = now + timedelta(
            hours=ACTIVE_LEARNING_MIN_RETRAIN_INTERVAL_HOURS
        )

        return {
            "status": "retraining_completed",
            "phase": 1,
            "samples_used": len(training_data),
            "next_retrain_eligible": next_eligible.isoformat(),
            "action": action,
            "improvement_pct": round(improvement, 4),
            "before_metrics": before,
            "after_metrics": after,
        }

    def get_statistics(self) -> Dict[str, Any]:
        """Return learning progress statistics.

        Returns:
            Dict with ``customer_id``, ``total_feedback``,
            ``high_quality_feedback``, ``operator_count``,
            ``retrainings_completed``, ``last_retrain``,
            ``operator_reliability``.
        """
        total = self.feedback_collector.count()
        high_quality = self._count_high_quality()

        operator_ids = self._get_operator_ids()
        operator_reliability = {
            op_id: self.validator.get_operator_reliability(op_id)
            for op_id in operator_ids
        }

        return {
            "customer_id": self.customer_id,
            "total_feedback": total,
            "high_quality_feedback": high_quality,
            "operator_count": len(operator_ids),
            "retrainings_completed": len(self._retraining_history),
            "last_retrain": (
                self._last_retrain.isoformat()
                if self._last_retrain is not None
                else None
            ),
            "operator_reliability": operator_reliability,
        }

    def _should_retrain_now(self) -> bool:
        """Check retrain criteria including cooldown."""
        if self._last_retrain is not None:
            elapsed = datetime.now(timezone.utc) - self._last_retrain
            cooldown = timedelta(
                hours=ACTIVE_LEARNING_MIN_RETRAIN_INTERVAL_HOURS
            )
            if elapsed < cooldown:
                return False

        return self.decision_maker.should_retrain(
            self.feedback_collector, self.validator
        )

    def _count_high_quality(self) -> int:
        """Count feedback from reliable operators."""
        count = 0
        for fb in self.feedback_collector.feedback:
            reliability = self.validator.get_operator_reliability(
                fb.get("operator_id", "")
            )
            if reliability >= ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD:
                count += 1
        return count

    def _get_training_feedback(self) -> List[Dict[str, Any]]:
        """Gather high-quality feedback for retraining, capped."""
        recent = self.feedback_collector.get_recent(
            days=ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS
        )
        training = [
            f
            for f in recent
            if self.validator.get_operator_reliability(
                f.get("operator_id", "")
            )
            >= ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD
        ]
        return training[:ACTIVE_LEARNING_MAX_TRAINING_SAMPLES]

    def _get_operator_ids(self) -> List[str]:
        """Operator IDs from validator (persistent) plus current buffer."""
        operator_ids = set(self.validator.operator_ids)
        for fb in self.feedback_collector.feedback:
            op_id = fb.get("operator_id", "")
            if op_id:
                operator_ids.add(op_id)
        return list(operator_ids)

    def export_state(self) -> Dict[str, Any]:
        """Serialize learner state for DB persistence."""
        return {
            "feedback_items": self.feedback_collector.export_state(),
            "validator": self.validator.export_state(),
            "retraining_history": list(self._retraining_history),
            "last_retrain": (
                self._last_retrain.isoformat()
                if self._last_retrain is not None
                else None
            ),
        }

    def load_state(self, state: Dict[str, Any]) -> None:
        """Restore learner state from DB persistence."""
        self.feedback_collector.load_state(state.get("feedback_items", []))
        self.validator.load_state(state.get("validator", {}))
        self._retraining_history = list(state.get("retraining_history", []))
        last_retrain = state.get("last_retrain")
        self._last_retrain = (
            datetime.fromisoformat(last_retrain) if last_retrain else None
        )
