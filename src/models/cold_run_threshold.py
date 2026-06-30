"""File-calibrated anomaly thresholds for cold-run CSV inference."""

from __future__ import annotations

import numpy as np

from src.config import (
    COLD_RUN_MIN_ANOMALIES,
    COLD_RUN_PERCENTILE,
    COLD_RUN_THRESHOLD_METHOD,
)
from src.models.pot_threshold import POTThresholdFitter


def calibrate_threshold(
    scores: np.ndarray,
    *,
    method: str | None = None,
) -> tuple[float, str]:
    """Derive a detection threshold from scores observed in one uploaded file."""
    if scores.size == 0:
        return 1.0, "percentile"

    arr = np.asarray(scores, dtype=np.float64)
    chosen = method or COLD_RUN_THRESHOLD_METHOD
    percentile_threshold = float(np.percentile(arr, COLD_RUN_PERCENTILE))

    if chosen == "pot":
        pot_threshold, used = _calibrate_pot(arr, percentile_threshold)
        if used == "pot" and pot_threshold >= percentile_threshold:
            return pot_threshold, "pot"

    return percentile_threshold, "percentile"


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
