"""Shared window-data loading for dashboard pages.

Both the Thresholds and System Metrics pages follow the Monitoring data pattern:
resolve full DB bounds → user picks a range → Apply → fetch exactly that window
from the API (merging upload scores when the DB rows lack them). This module
centralises that fetch so the pages don't each re-implement it, and provides the
threshold-slider scaling helper both the Thresholds page and its tests use.
"""

from __future__ import annotations

import math

import pandas as pd

from src.dashboard.components.charts import apply_datetime_filter
from src.dashboard.components.monitoring_jump import load_monitoring_window
from src.dashboard.services.anomalies_page_data import resolve_window_dataframe

# Fallback score span used to scale the slider when the window has no scores yet
# (LSTM-VAE scores are squashed to [0, 1] and top out near 0.2 on a real day).
_DEFAULT_SCORE_MAX = 0.2
_SLIDER_STEP_FINE = 0.01
_SLIDER_STEP_COARSE = 0.05
_SLIDER_FINE_RANGE = 0.5


def load_page_window(
    tenant_id: str,
    greenhouse_id: str,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    *,
    require_scores: bool = False,
    inf_results: dict | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Load the merged sensor window for a page.

    When ``require_scores`` is True (Thresholds), use the anomaly-page resolver
    so upload scores are merged into DB rows and, if the DB has none, the
    upload-only score frame is used as a fallback. When False (Metrics), a plain
    windowed readings fetch is enough — scores are surfaced only if they already
    exist. Returns ``(df, using_upload_fallback)``; the flag is True only when the
    primary series came from the cold-run upload rather than persisted readings.
    """
    if require_scores:
        df, using_fallback = resolve_window_dataframe(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            start_iso=window_start.isoformat(),
            end_iso=window_end.isoformat(),
            window_start=window_start,
            window_end=window_end,
            upload_job_id=str(inf_results.get("job_id")) if inf_results else None,
            _inf_results=inf_results,
        )
    else:
        df = load_monitoring_window(tenant_id, greenhouse_id, window_start, window_end)
        using_fallback = False

    df = apply_datetime_filter(df, window_start, window_end)
    return df, using_fallback


def threshold_slider_range(
    scores: pd.Series, saved: float
) -> tuple[float, float, float]:
    """Return ``(min, max, step)`` for a slider scaled to the score distribution.

    The raw dashboard default (0.8) sits far above the real score range, so a
    fixed 0–1 slider forces users to drag up from nothing. Scale the top of the
    slider to the window's own scores (with headroom), never below the saved
    threshold, and pick a finer step for narrow ranges.
    """
    numeric = pd.to_numeric(scores, errors="coerce").dropna()
    score_max = float(numeric.max()) if not numeric.empty else _DEFAULT_SCORE_MAX

    slider_min = 0.0
    slider_max = min(max(score_max * 1.5, saved, _DEFAULT_SCORE_MAX), 1.0)
    step = (
        _SLIDER_STEP_FINE
        if (slider_max - slider_min) <= _SLIDER_FINE_RANGE
        else _SLIDER_STEP_COARSE
    )
    return slider_min, slider_max, step


def lookback_hours_for_window(
    window_start: pd.Timestamp, window_end: pd.Timestamp
) -> int:
    """Whole hours spanned by the selected window (min 1), for pipeline metrics."""
    seconds = (window_end - window_start).total_seconds()
    return max(1, math.ceil(seconds / 3600))
