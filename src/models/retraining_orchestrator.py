"""
Retraining orchestrator: facade over the active learning subsystem.

Wraps :class:`~src.models.active_learner.ActiveLearner`,
:class:`~src.models.feedback_collector.FeedbackCollector`, and
:class:`~src.models.feedback_validator.FeedbackValidator` into a single
entry point for the API layer.

All thresholds come from :mod:`src.config` (``ACTIVE_LEARNING_*``).
"""

import logging
import math
from typing import Any, Dict, Optional

from src.config import (
    ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE,
    ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD,
    ACTIVE_LEARNING_MIN_QUALITY_LABELS,
)
from src.models.active_learner import ActiveLearner

logger = logging.getLogger(__name__)


class RetrainingOrchestrator:
    """Single facade for feedback collection, retraining, and promotion.

    Usage::

        orch = RetrainingOrchestrator(customer_id="cust_01")
        result = orch.observe(
            incident_id="inc_001",
            is_true_anomaly=True,
            operator_id="op_alice",
            pattern_type_observed="drift",
        )

    Delegates to :class:`ActiveLearner` internally and adds
    ``get_retrain_plan``, ``get_progress``, and ``validate_and_promote``
    on top.
    """

    def __init__(self, customer_id: str) -> None:
        """
        Args:
            customer_id: Non-empty customer identifier.

        Raises:
            ValueError: If ``customer_id`` is empty (via ActiveLearner).
        """
        self._learner: ActiveLearner = ActiveLearner(
            customer_id=customer_id
        )
        self.customer_id: str = customer_id

    def observe(
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
        """Record operator feedback; trigger retrain if criteria are met.

        Args:
            incident_id: Non-empty incident identifier.
            is_true_anomaly: Operator verdict.
            operator_id: Non-empty operator identifier.
            pattern_type_observed: Pattern the operator identified.
            actual_root_cause: Root cause the operator believes.
            operator_confidence: Self-assessed confidence [0, 1].
            severity: Optional 1-10 scale.
            notes: Free-text context.

        Returns:
            Dict with ``status``, ``learning_triggered``,
            ``feedback_count``, ``high_quality_count``, and optionally
            ``retraining`` when learning was triggered.

        Raises:
            ValueError: On invalid inputs (delegated to ActiveLearner).
        """
        result = self._learner.collect_operator_feedback(
            incident_id=incident_id,
            is_true_anomaly=is_true_anomaly,
            operator_id=operator_id,
            pattern_type_observed=pattern_type_observed,
            actual_root_cause=actual_root_cause,
            operator_confidence=operator_confidence,
            severity=severity,
            notes=notes,
            sequence=sequence,
            detector_scores=detector_scores,
        )

        # collect_operator_feedback now auto-triggers retraining when
        # criteria are met, so the result already includes "retraining"
        # when learning_triggered is True.
        return result

    def get_retrain_plan(self) -> Dict[str, Any]:
        """Return what would be retrained and why.

        Returns:
            Phase 1 retrain plan dict from
            :meth:`RetrainingDecisionMaker.what_to_retrain`.
        """
        return self._learner.decision_maker.what_to_retrain()

    def get_progress(self) -> Dict[str, Any]:
        """Return progress toward the next retrain trigger.

        Returns:
            Dict with ``customer_id``, ``progress_percent``,
            ``samples_needed``, ``high_quality_count``,
            ``total_feedback``, and ``last_retrain``.
        """
        stats = self._learner.get_statistics()
        high_quality = stats["high_quality_feedback"]
        target = ACTIVE_LEARNING_MIN_QUALITY_LABELS
        progress_pct = min(
            (high_quality / target) * 100.0 if target > 0 else 100.0,
            100.0,
        )
        samples_needed = max(0, target - high_quality)

        return {
            "customer_id": self.customer_id,
            "progress_percent": round(progress_pct, 1),
            "samples_needed": samples_needed,
            "high_quality_count": high_quality,
            "total_feedback": stats["total_feedback"],
            "last_retrain": stats["last_retrain"],
        }

    def validate_and_promote(
        self, model_improvement_pct: float
    ) -> Dict[str, Any]:
        """Decide whether to promote a retrained model.

        Promotes if ``model_improvement_pct`` >=
        ``ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD``; otherwise rolls back.

        Args:
            model_improvement_pct: Fractional improvement (e.g. 0.08
                means 8% improvement). Must be finite and >= 0.

        Returns:
            Dict with ``action`` (``"promote"`` or ``"rollback"``),
            ``improvement_pct``, ``threshold``, and ``reason``.

        Raises:
            ValueError: If ``model_improvement_pct`` is negative or
                non-finite.
        """
        if not isinstance(model_improvement_pct, (int, float)):
            raise ValueError(
                "model_improvement_pct must be a number, "
                f"got {type(model_improvement_pct).__name__}"
            )
        if not math.isfinite(model_improvement_pct):
            raise ValueError(
                "model_improvement_pct must be finite, "
                f"got {model_improvement_pct}"
            )
        if model_improvement_pct < 0.0:
            raise ValueError(
                "model_improvement_pct must be >= 0, "
                f"got {model_improvement_pct}"
            )

        threshold = ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD

        if model_improvement_pct >= threshold:
            logger.info(
                "[%s] Model promoted: %.1f%% improvement >= %.1f%% threshold",
                self.customer_id,
                model_improvement_pct * 100,
                threshold * 100,
            )
            return {
                "action": "promote",
                "improvement_pct": model_improvement_pct,
                "threshold": threshold,
                "reason": (
                    f"Improvement {model_improvement_pct:.1%} meets "
                    f"threshold {threshold:.1%}"
                ),
            }

        logger.info(
            "[%s] Model rolled back: %.1f%% improvement < %.1f%% threshold",
            self.customer_id,
            model_improvement_pct * 100,
            threshold * 100,
        )
        return {
            "action": "rollback",
            "improvement_pct": model_improvement_pct,
            "threshold": threshold,
            "reason": (
                f"Improvement {model_improvement_pct:.1%} below "
                f"threshold {threshold:.1%}"
            ),
        }

    @property
    def learner(self) -> ActiveLearner:
        """Access the underlying ActiveLearner (for statistics etc.)."""
        return self._learner

    def export_state(self) -> Dict[str, Any]:
        """Serialize orchestrator state for DB persistence."""
        return self._learner.export_state()

    def load_state(self, state: Dict[str, Any]) -> None:
        """Restore orchestrator state from DB persistence."""
        self._learner.load_state(state)
