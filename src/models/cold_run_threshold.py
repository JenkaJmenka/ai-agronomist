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


def calibrate_threshold(
    scores: np.ndarray,
    *,
    method: str | None = None,
) -> tuple[float, str]:
    """Derive a detection threshold from scores observed in one uploaded file.

    Args:
        scores: Every raw anomaly score produced for the file.
        method: Relative criterion — ``"percentile"`` or ``"pot"``.  Defaults
            to ``COLD_RUN_THRESHOLD_METHOD``.

    Returns:
        ``(threshold, method_used)``.  ``method_used`` carries
        ``_ROBUST_GATE_SUFFIX`` when the absolute gate raised the threshold
        above the relative one.
    """
    if scores.size == 0:
        return 1.0, "percentile"

    arr = np.asarray(scores, dtype=np.float64)
    chosen = method or COLD_RUN_THRESHOLD_METHOD
    percentile_threshold = float(np.percentile(arr, COLD_RUN_PERCENTILE))

    threshold, threshold_method = percentile_threshold, "percentile"
    if chosen == "pot":
        pot_threshold, used = _calibrate_pot(arr, percentile_threshold)
        if used == "pot" and pot_threshold >= percentile_threshold:
            threshold, threshold_method = pot_threshold, "pot"

    return _apply_robust_gate(arr, threshold, threshold_method)


def _apply_robust_gate(
    arr: np.ndarray, threshold: float, threshold_method: str
) -> tuple[float, str]:
    """Raise a file-relative threshold to the absolute anomalousness gate."""
    if not COLD_RUN_ROBUST_GATE_ENABLED:
        return threshold, threshold_method
    gate = _robust_score_gate(arr)
    if gate is None or gate <= threshold:
        return threshold, threshold_method
    return gate, f"{threshold_method}{_ROBUST_GATE_SUFFIX}"


def _robust_score_gate(arr: np.ndarray) -> float | None:
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
    return median + COLD_RUN_ROBUST_GATE_Z * _MAD_TO_SIGMA * mad


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
