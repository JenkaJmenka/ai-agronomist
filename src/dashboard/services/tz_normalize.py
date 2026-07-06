"""Consistent timezone normalization for dashboard timestamp filtering.

The API may return tz-aware (UTC) or tz-naive timestamps depending on
the code path.  Comparing a tz-aware Series against a tz-naive bound
silently drops every row, producing the "only December shows" bug.

These helpers ensure both sides of any comparison share the same tz state.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def normalize_series_tz(series: pd.Series) -> pd.Series:
    """Force a timestamp Series to a single tz state (all UTC or all naive).

    Mixed columns (some tz-aware, some naive) arise when pd.to_datetime
    parses heterogeneous ISO strings.  We unify to UTC-aware if ANY value
    carries tz info, otherwise stay naive.
    """
    if series.empty:
        return series

    # Already homogeneous — fast path
    if series.dt.tz is not None:
        return series

    # Check for object-dtype columns with mixed tz in individual values
    if series.dtype == object:
        series = pd.to_datetime(series, utc=True)
        return series

    return series


def align_bound_to_series(
    bound: pd.Timestamp,
    series: pd.Series,
) -> pd.Timestamp:
    """Align a single Timestamp bound to match the tz state of *series*."""
    series_tz_aware = series.dt.tz is not None
    bound_tz_aware = getattr(bound, "tzinfo", None) is not None

    if series_tz_aware and not bound_tz_aware:
        return bound.tz_localize("UTC")
    if not series_tz_aware and bound_tz_aware:
        return bound.tz_localize(None)
    return bound


def strip_tz(ts: pd.Timestamp) -> pd.Timestamp:
    """Remove timezone info from a Timestamp, no-op if already naive."""
    if getattr(ts, "tzinfo", None) is not None:
        return ts.tz_localize(None)
    return ts
