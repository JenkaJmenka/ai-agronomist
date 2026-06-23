"""
Polling dropout detector for SCADA sensor sequences.

Implements SCADA spec section 1.5 exactly:
    ψ_t = 1 if any sensor value is NaN or null at step t
    Alarm: P consecutive steps with ψ_t = 1
"""
from __future__ import annotations

import math
from typing import Any

try:
    from src.config import DROPOUT_CONSECUTIVE_P
except ImportError:  # pragma: no cover
    DROPOUT_CONSECUTIVE_P = 5


class DropoutDetector:
    """Detects P consecutive polling dropouts per SCADA spec section 1.5.

    A timestep is flagged (ψ=1) when ANY sensor value is null or NaN.
    An alarm fires when P such steps occur consecutively.
    """

    CONSECUTIVE_THRESHOLD: int = DROPOUT_CONSECUTIVE_P

    # Backward-compat alias: comprehensive_pipeline.py reads this attribute.
    # TODO(ml-engineer): backend-engineer should migrate to CONSECUTIVE_THRESHOLD — 2026-06-23
    PERSISTENCE_THRESHOLD: float = 0.25

    def detect(self, sequence_raw: list[dict[str, Any]]) -> bool:
        """Return True if P or more consecutive steps have any null/NaN sensor.

        Args:
            sequence_raw: List of dicts, one per time step.  Each dict
                maps sensor names to their readings (or None for missing).
                An empty dict is treated as a null row.

        Returns:
            True when P consecutive steps each have at least one null/NaN
            sensor value, indicating a polling dropout fault.
        """
        consecutive = 0
        for row in sequence_raw:
            if _any_null(row):
                consecutive += 1
                if consecutive >= self.CONSECUTIVE_THRESHOLD:
                    return True
            else:
                consecutive = 0
        return False


def _any_null(row: dict[str, Any]) -> bool:
    """Return True when any value in the row is None, NaN, or the row is empty."""
    if not row:
        return True
    return any(
        v is None or (isinstance(v, float) and math.isnan(v))
        for v in row.values()
    )
