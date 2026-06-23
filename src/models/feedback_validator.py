"""
Validate feedback quality and track operator reliability.

Uses an exponential moving average (EMA) to update each operator's
reliability score as ground-truth outcomes are recorded. The validator
can then classify feedback batches by operator quality.

All thresholds come from :mod:`src.config` (``ACTIVE_LEARNING_*``).
"""

import logging
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List

from src.config import (
    ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES,
    ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD,
    ACTIVE_LEARNING_OPERATOR_ALPHA,
    ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT,
)

logger = logging.getLogger(__name__)


class FeedbackValidator:
    """Track operator accuracy and classify feedback quality.

    Usage::

        validator = FeedbackValidator()
        validator.record_feedback_outcome("op_alice", True)
        reliability = validator.get_operator_reliability("op_alice")

    Reliability is an EMA updated with
    ``ACTIVE_LEARNING_OPERATOR_ALPHA``; unknown operators get
    ``ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT``.
    """

    def __init__(self) -> None:
        self._operator_accuracy: Dict[str, float] = {}
        self._operator_feedback_count: DefaultDict[str, int] = defaultdict(int)
        self._feedback_history: DefaultDict[str, List[bool]] = defaultdict(
            list
        )

    def record_feedback_outcome(
        self, operator_id: str, feedback_was_correct: bool
    ) -> None:
        """Record whether an operator's past feedback was accurate.

        Updates the operator's EMA reliability score. Called after
        ground truth becomes available (e.g., an expert confirms the
        operator's earlier verdict).

        Args:
            operator_id: Non-empty operator identifier.
            feedback_was_correct: Whether the feedback matched reality.

        Raises:
            ValueError: If ``operator_id`` is empty.
        """
        if not isinstance(operator_id, str) or not operator_id.strip():
            raise ValueError(
                "operator_id must be a non-empty string, "
                f"got {operator_id!r}"
            )

        if operator_id not in self._operator_accuracy:
            self._operator_accuracy[operator_id] = (
                ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT
            )

        alpha = ACTIVE_LEARNING_OPERATOR_ALPHA
        self._operator_accuracy[operator_id] = (
            alpha * float(feedback_was_correct)
            + (1.0 - alpha) * self._operator_accuracy[operator_id]
        )

        self._operator_feedback_count[operator_id] += 1
        self._feedback_history[operator_id].append(feedback_was_correct)

        logger.info(
            "Operator %s accuracy updated to %.2f%% (count=%d)",
            operator_id,
            self._operator_accuracy[operator_id] * 100,
            self._operator_feedback_count[operator_id],
        )

    def get_operator_reliability(self, operator_id: str) -> float:
        """Return the reliability score for an operator.

        Returns ``ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT`` for
        operators with no recorded outcomes. When the outcome count is
        below ``ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES``, the raw EMA is
        dampened toward the default so new operators cannot swing
        retraining decisions after a single label.

        Args:
            operator_id: Operator identifier.

        Returns:
            Reliability in [0, 1].
        """
        if operator_id not in self._operator_accuracy:
            return ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT

        raw = self._operator_accuracy[operator_id]
        count = self._operator_feedback_count.get(operator_id, 0)
        min_samples = ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES

        if count >= min_samples:
            return raw

        weight = count / min_samples
        return (
            weight * raw
            + (1.0 - weight) * ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT
        )

    def get_operator_feedback_count(self, operator_id: str) -> int:
        """Return the total outcome count for an operator."""
        return self._operator_feedback_count.get(operator_id, 0)

    def validate_batch(
        self, feedback_list: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Classify a batch of feedback by operator reliability.

        Args:
            feedback_list: Feedback dicts, each containing at minimum
                an ``"operator_id"`` key.

        Returns:
            Dict with ``total``, ``high_quality``, ``low_quality``, and
            ``operator_stats`` (per-operator reliability + count).
        """
        results: Dict[str, Any] = {
            "total": len(feedback_list),
            "high_quality": 0,
            "low_quality": 0,
            "operator_stats": {},
        }

        for fb in feedback_list:
            operator_id = fb.get("operator_id", "")
            reliability = self.get_operator_reliability(operator_id)

            if reliability >= ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD:
                results["high_quality"] += 1
            else:
                results["low_quality"] += 1

            if operator_id not in results["operator_stats"]:
                results["operator_stats"][operator_id] = {
                    "reliability": reliability,
                    "count": self._operator_feedback_count.get(
                        operator_id, 0
                    ),
                }

        return results

    def export_state(self) -> Dict[str, Any]:
        """Serialize validator state for persistence."""
        return {
            "operator_accuracy": dict(self._operator_accuracy),
            "operator_feedback_count": dict(self._operator_feedback_count),
            "feedback_history": {
                operator_id: list(history)
                for operator_id, history in self._feedback_history.items()
            },
        }

    def load_state(self, state: Dict[str, Any]) -> None:
        """Restore validator state from persistence."""
        self._operator_accuracy = dict(state.get("operator_accuracy", {}))
        self._operator_feedback_count = defaultdict(int)
        self._operator_feedback_count.update(
            state.get("operator_feedback_count", {})
        )
        self._feedback_history = defaultdict(list)
        for operator_id, history in state.get("feedback_history", {}).items():
            self._feedback_history[operator_id] = list(history)

    @property
    def operator_ids(self) -> List[str]:
        """All operator IDs that have recorded outcomes."""
        return list(self._operator_accuracy.keys())
