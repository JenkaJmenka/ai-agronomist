"""File-calibrated anomaly thresholds for cold-run CSV inference.

Two criteria, composed:

1. A *relative* threshold — the ``COLD_RUN_PERCENTILE`` quantile of the
   uploaded file's own scores (optionally a stricter POT/EVT fit).  Because it
   is a quantile, it flags a fixed FRACTION of every file regardless of data
   quality, so the anomaly count tracks file size rather than anomalousness.
2. An *absolute* gate — median + z * MAD of the same scores, which measures how
   far the tail sits from the bulk and is therefore independent of row count.
   Off by default; see ``COLD_RUN_ROBUST_GATE_ENABLED``.

The gate can only raise the threshold, never lower it, so enabling it can only
remove anomalies.  It does move the ADR-005 canonical anchor for the file
(``canonical 0.5 == threshold``), which is why it is behind a switch.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

import numpy as np

from src.config import (
    COLD_RUN_MIN_ANOMALIES,
    COLD_RUN_PERCENTILE,
    COLD_RUN_ROBUST_GATE_ENABLED,
    COLD_RUN_ROBUST_GATE_Z,
    COLD_RUN_THRESHOLD_METHOD,
)
from src.models.pot_threshold import POTThresholdFitter

# Scale factor making the median absolute deviation a consistent estimator of
# the standard deviation for normally distributed data.
_MAD_TO_SIGMA: float = 1.4826

# Appended to the reported threshold_method when the absolute gate binds, so
# the response says which criterion actually set the number.
_ROBUST_GATE_SUFFIX: str = "+robust_gate"


class _FromConfig(Enum):
    """Sentinel: read the robust-gate setting from ``src.config`` at call time."""

    FROM_CONFIG = "from_config"


def calibrate_threshold(
    scores: np.ndarray,
    *,
    method: str | None = None,
    percentile: Optional[float] = None,
    robust_gate_z: Union[float, None, _FromConfig] = _FromConfig.FROM_CONFIG,
) -> tuple[float, str]:
    """Derive a detection threshold from scores observed in one uploaded file.

    Args:
        scores: Every raw anomaly score produced for the file.
        method: Relative criterion — ``"percentile"`` or ``"pot"``.  Defaults
            to ``COLD_RUN_THRESHOLD_METHOD``.
        percentile: Quantile for the relative criterion. Defaults to
            ``COLD_RUN_PERCENTILE`` (ADR-014: the batch profile passes its own).
        robust_gate_z: Robust-gate z, or ``None`` to disable the gate.
            Defaults to ``COLD_RUN_ROBUST_GATE_Z`` when
            ``COLD_RUN_ROBUST_GATE_ENABLED``, else disabled.

    Returns:
        ``(threshold, method_used)``.  ``method_used`` carries
        ``_ROBUST_GATE_SUFFIX`` when the absolute gate raised the threshold
        above the relative one.
    """
    if scores.size == 0:
        return 1.0, "percentile"

    arr = np.asarray(scores, dtype=np.float64)
    chosen = method or COLD_RUN_THRESHOLD_METHOD
    quantile = COLD_RUN_PERCENTILE if percentile is None else percentile
    percentile_threshold = float(np.percentile(arr, quantile))

    threshold, threshold_method = percentile_threshold, "percentile"
    if chosen == "pot":
        pot_threshold, used = _calibrate_pot(arr, percentile_threshold)
        if used == "pot" and pot_threshold >= percentile_threshold:
            threshold, threshold_method = pot_threshold, "pot"

    if robust_gate_z is _FromConfig.FROM_CONFIG:
        robust_gate_z = COLD_RUN_ROBUST_GATE_Z if COLD_RUN_ROBUST_GATE_ENABLED else None
    return _apply_robust_gate(arr, threshold, threshold_method, robust_gate_z)


def _apply_robust_gate(
    arr: np.ndarray, threshold: float, threshold_method: str, gate_z: Optional[float]
) -> tuple[float, str]:
    """Raise a file-relative threshold to the absolute anomalousness gate.

    ``gate_z`` ``None`` means the gate is disabled.
    """
    if gate_z is None:
        return threshold, threshold_method
    gate = _robust_score_gate(arr, gate_z)
    if gate is None or gate <= threshold:
        return threshold, threshold_method
    return gate, f"{threshold_method}{_ROBUST_GATE_SUFFIX}"


def _robust_score_gate(arr: np.ndarray, gate_z: float) -> float | None:
    """Score at which a reading is genuinely far from the bulk of the file.

    Uses median/MAD rather than mean/std so that the anomalies themselves do
    not inflate the estimate of "normal spread".

    Returns:
        The gate, or ``None`` when the distribution has no usable scale (a
        zero MAD means over half the scores are identical, and the relative
        threshold is already degenerate in that case).
    """
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    if not np.isfinite(median) or not np.isfinite(mad) or mad <= 0.0:
        return None
    return median + gate_z * _MAD_TO_SIGMA * mad


def _calibrate_pot(arr: np.ndarray, percentile_floor: float) -> tuple[float, str]:
    """POT is only accepted when it is stricter than the percentile floor."""
    fitter = POTThresholdFitter()
    for value in arr:
        fitter._buffer.append(float(value))
    threshold = float(fitter.fit())
    if not np.isfinite(threshold):
        return percentile_floor, "percentile"
    if threshold < percentile_floor:
        return percentile_floor, "percentile"
    if threshold > float(arr.max()):
        return percentile_floor, "percentile"
    if int((arr > threshold).sum()) < COLD_RUN_MIN_ANOMALIES:
        return percentile_floor, "percentile"
    return threshold, "pot"
