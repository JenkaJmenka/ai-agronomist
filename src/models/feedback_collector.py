"""
Collect and store operator feedback about detected anomalies.

Maintains a bounded circular buffer of feedback entries, each annotated
with operator confidence, pattern type, root cause, and a timestamp.
Supports querying recent entries. Operator quality is determined by
:class:`~src.models.feedback_validator.FeedbackValidator`, not fields
on individual feedback items.

All thresholds come from :mod:`src.config` (``ACTIVE_LEARNING_*``).
"""

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional

from src.config import ACTIVE_LEARNING_FEEDBACK_CAPACITY

logger = logging.getLogger(__name__)


class FeedbackCollector:
    """Collect operator annotations for anomaly learning.

    Usage::

        collector = FeedbackCollector()
        collector.collect(
            incident_id="inc_001",
            sequence=readings,
            is_true_anomaly=True,
            operator_id="op_alice",
        )
        recent = collector.get_recent(days=7)

    Stores entries in an in-memory :class:`collections.deque` with
    ``maxlen=capacity``; oldest entries are evicted when full.
    """

    def __init__(
        self, capacity: int = ACTIVE_LEARNING_FEEDBACK_CAPACITY
    ) -> None:
        """
        Args:
            capacity: Maximum feedback entries to retain. Must be > 0.

        Raises:
            ValueError: If ``capacity`` is not a positive integer.
        """
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError(
                f"capacity must be a positive integer, got {capacity!r}"
            )
        self.capacity: int = capacity
        self._feedback: Deque[Dict[str, Any]] = deque(maxlen=capacity)

    def collect(
        self,
        incident_id: str,
        sequence: Any,
        is_true_anomaly: bool,
        operator_id: str,
        pattern_type_observed: Optional[str] = None,
        actual_root_cause: Optional[str] = None,
        severity: Optional[int] = None,
        operator_confidence: float = 0.8,
        notes: Optional[str] = None,
        detector_scores: Optional[Dict[str, float]] = None,
    ) -> None:
        """Store operator feedback about an incident.

        Args:
            incident_id: Non-empty incident identifier.
            sequence: Sensor readings (stored as-is for future retraining).
            is_true_anomaly: Operator's verdict.
            operator_id: Non-empty identifier of the annotating operator.
            pattern_type_observed: Pattern the operator identified.
            actual_root_cause: Root cause the operator believes is correct.
            severity: Optional severity on a 1-10 scale.
            operator_confidence: Operator's self-assessed confidence [0, 1].
            notes: Free-text context.

        Raises:
            ValueError: On empty ``incident_id`` / ``operator_id``,
                ``operator_confidence`` outside [0, 1], or ``severity``
                outside [1, 10] when provided.
        """
        self._validate_collect_inputs(
            incident_id, operator_id, operator_confidence, severity
        )

        feedback_item: Dict[str, Any] = {
            "incident_id": incident_id,
            "sequence": sequence,
            "is_anomaly": is_true_anomaly,
            "operator_id": operator_id,
            "pattern_type": pattern_type_observed,
            "root_cause": actual_root_cause,
            "severity": severity,
            "operator_confidence": operator_confidence,
            "notes": notes,
            "detector_scores": detector_scores,
            "timestamp": datetime.now(timezone.utc),
        }

        self._feedback.append(feedback_item)
        logger.info(
            "Collected feedback: incident=%s, is_anomaly=%s, operator=%s",
            incident_id,
            is_true_anomaly,
            operator_id,
        )

    def get_recent(self, days: int = 30) -> List[Dict[str, Any]]:
        """Return feedback from the last ``days`` days.

        Args:
            days: Look-back window in days (>= 0).

        Returns:
            List of feedback dicts, newest first.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return [
            f for f in self._feedback if f["timestamp"] >= cutoff
        ]

    def count(self) -> int:
        """Total feedback entries currently stored."""
        return len(self._feedback)

    @property
    def feedback(self) -> tuple:
        """Read-only snapshot of the feedback buffer."""
        return tuple(self._feedback)

    def _validate_collect_inputs(
        self,
        incident_id: str,
        operator_id: str,
        operator_confidence: float,
        severity: Optional[int],
    ) -> None:
        """Reject invalid inputs at the boundary."""
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise ValueError(
                "incident_id must be a non-empty string, "
                f"got {incident_id!r}"
            )
        if not isinstance(operator_id, str) or not operator_id.strip():
            raise ValueError(
                "operator_id must be a non-empty string, "
                f"got {operator_id!r}"
            )
        if not 0.0 <= operator_confidence <= 1.0:
            raise ValueError(
                "operator_confidence must be in [0, 1], "
                f"got {operator_confidence}"
            )
        if severity is not None and not (1 <= severity <= 10):
            raise ValueError(
                f"severity must be in [1, 10] when provided, got {severity}"
            )

    def export_state(self) -> List[Dict[str, Any]]:
        """Serialize feedback items for persistence."""
        items: List[Dict[str, Any]] = []
        for item in self._feedback:
            copy = dict(item)
            timestamp = copy.get("timestamp")
            if isinstance(timestamp, datetime):
                copy["timestamp"] = timestamp.isoformat()
            items.append(copy)
        return items

    def load_state(self, items: List[Dict[str, Any]]) -> None:
        """Restore feedback items from persisted state."""
        self._feedback.clear()
        for item in items:
            loaded = dict(item)
            timestamp = loaded.get("timestamp")
            if isinstance(timestamp, str):
                loaded["timestamp"] = datetime.fromisoformat(timestamp)
            self._feedback.append(loaded)
