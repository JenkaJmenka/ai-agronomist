"""Canonical anomaly score transform — ADR-005 §2.1.

``canonical(raw, p, hi)`` maps a producer's native anomaly score to a
calibrated exceedance ratio in ``[0, 1]`` where:

* ``0.5`` == the producing detector's own decision point
* ``1.0`` == the producer's saturation point (asymptotic for unbounded scales)

Four-branch piecewise (continuous at ``raw == p``, strictly monotone)::

    raw < 0                   →  0.0            # clamp below-zero
    raw <= p                  →  0.5 * raw / p
    raw >  p, hi is not None  →  0.5 + 0.5 * (raw - p) / (hi - p)
    raw >  p, hi is None      →  0.5 + 0.5 * (raw - p) / raw   # asymptotes to 1

Non-finite guard (ADR §2.4)
---------------------------
Returns ``None`` — which should be stored as SQL ``NULL`` — for:

* ``raw`` is NaN or ±Inf
* ``p`` is NaN, ±Inf, zero, or negative
* ``hi is not None`` and ``hi`` is non-finite

Every ``None`` result is logged at WARNING with tenant / greenhouse / timestamp
context so Postgres JSON-serialisation failures can be traced back to the row
that caused them (the known SQLite/Postgres NaN gap makes this guard
mandatory — see AGENT_MEMORY.md).

Pattern reused from ``src/live/events.py:sanitize_channels`` and
``_validate_finite`` (:52, :62).  That module strips non-finite floats before
JSON-column writes; this module guards the *score* column path.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


def canonical(
    raw: float,
    p: float,
    hi: Optional[float],
    *,
    tenant_id: str = "",
    greenhouse_id: str = "",
    timestamp: str = "",
) -> Optional[float]:
    """Transform a raw producer score to the canonical ``[0, 1]`` scale.

    Parameters
    ----------
    raw:
        The producer's native anomaly score (e.g. LSTM-VAE reconstruction
        error scaled by ``ANOMALY_SCALING_FACTOR``, or a fused probability).
    p:
        The producer's own decision threshold in its native units.
        Must be finite and strictly positive.
    hi:
        Saturation value — the raw score that maps to canonical 1.0.
        Pass ``None`` for unbounded producers (live SCADA scorer).
    tenant_id, greenhouse_id, timestamp:
        Context fields included in WARNING log entries so non-finite inputs
        can be traced back to the offending row.

    Returns
    -------
    float in ``[0, 1]``, or ``None`` if the input is invalid / non-finite.
    """
    # Guard: validate p
    if not math.isfinite(p) or p <= 0.0:
        logger.warning(
            "canonical(): invalid p=%r (must be finite and > 0); returning NULL "
            "[tenant=%s greenhouse=%s ts=%s]",
            p, tenant_id, greenhouse_id, timestamp,
        )
        return None

    # Guard: validate raw
    if not math.isfinite(raw):
        logger.warning(
            "canonical(): non-finite raw=%r; returning NULL "
            "[tenant=%s greenhouse=%s ts=%s]",
            raw, tenant_id, greenhouse_id, timestamp,
        )
        return None

    # Guard: validate hi when provided
    if hi is not None and not math.isfinite(hi):
        logger.warning(
            "canonical(): non-finite hi=%r; returning NULL "
            "[tenant=%s greenhouse=%s ts=%s]",
            hi, tenant_id, greenhouse_id, timestamp,
        )
        return None

    # Branch 1 — clamp negative raw
    if raw < 0.0:
        return 0.0

    # Branch 2 — below threshold: linear 0→0.5
    if raw <= p:
        return 0.5 * raw / p

    # raw > p — above threshold
    if hi is not None:
        # Branch 3 — bounded: linear 0.5→1.0
        return 0.5 + 0.5 * (raw - p) / (hi - p)

    # Branch 4 — unbounded: asymptotic to 1.0, never reaches it
    return 0.5 + 0.5 * (raw - p) / raw
