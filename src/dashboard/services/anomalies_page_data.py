"""Cached data loading and transforms for the Anomaly Detection page."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard.constants import CACHE_TTL_LONG, CACHE_TTL_MEDIUM, CACHE_TTL_SHORT

from src.dashboard.components.charts import (
    query_sensor_readings_window,
    query_sensor_timestamp_bounds,
)
from src.dashboard.components.inference_results import build_sensor_dataframe_from_upload
from src.dashboard.services.anomaly_events import flagged_anomaly_mask
from src.dashboard.services.diagnosis_enricher import enrich_dataframe
from src.dashboard.services.incident_loader import fetch_incidents_in_range

_CHART_MAX_POINTS = 2000
_TABLE_PAGE_SIZE = 10
_COLD_RUN_TABLE_LIMIT = 40


def load_readings_window(
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> pd.DataFrame:
    # Caching is handled by query_sensor_readings_window in charts.py.
    return query_sensor_readings_window(tenant_id, greenhouse_id, start_iso, end_iso)


def load_timestamp_bounds(
    tenant_id: str,
    greenhouse_id: str,
) -> tuple[str | None, str | None]:
    # Caching is handled by query_sensor_timestamp_bounds in charts.py.
    return query_sensor_timestamp_bounds(tenant_id, greenhouse_id)


@st.cache_data(ttl=CACHE_TTL_LONG, show_spinner=False)
def load_upload_score_frame(
    tenant_id: str,
    greenhouse_id: str,
    sequences_processed: int,
    score_count: int,
    results: dict,
) -> pd.DataFrame:
    del tenant_id, greenhouse_id, sequences_processed, score_count
    return build_sensor_dataframe_from_upload(results)


@st.cache_data(ttl=CACHE_TTL_SHORT, show_spinner=False)
def enrich_anomalies_table(
    anomalies: pd.DataFrame,
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> pd.DataFrame:
    if anomalies.empty:
        return anomalies
    incidents = fetch_incidents_in_range(tenant_id, greenhouse_id, start_iso, end_iso)
    return enrich_dataframe(anomalies, incidents)


def bounds_from_upload_results(results: dict) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    frame = build_sensor_dataframe_from_upload(results)
    if frame.empty:
        return None
    ts = pd.to_datetime(frame["timestamp"])
    return ts.min(), ts.max()


def filter_anomalies(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
    if df.empty or "anomaly_score" not in df.columns:
        return pd.DataFrame()
    mask = flagged_anomaly_mask(df["anomaly_score"], threshold)
    return df.loc[mask].copy()


def downsample_for_chart(df: pd.DataFrame, max_points: int = _CHART_MAX_POINTS) -> pd.DataFrame:
    if df.empty or len(df) <= max_points:
        return df
    step = max(1, len(df) // max_points)
    work = df.sort_values("timestamp") if "timestamp" in df.columns else df
    return work.iloc[::step].copy()


def clear_anomaly_page_cache() -> None:
    """Invalidate cached anomaly-page queries and enrichment."""
    # load_readings_window and load_timestamp_bounds are plain pass-throughs;
    # their underlying functions in charts.py carry their own cache.
    load_upload_score_frame.clear()
    enrich_anomalies_table.clear()


def rows_in_window(
    rows: list[dict],
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> list[dict]:
    """Keep cold-run anomaly dicts whose timestamp falls in [window_start, window_end]."""
    if not rows:
        return []
    start = pd.Timestamp(window_start)
    end = pd.Timestamp(window_end)
    kept: list[dict] = []
    for row in rows:
        raw_ts = row.get("timestamp")
        if not raw_ts:
            continue
        ts = pd.to_datetime(raw_ts, errors="coerce", utc=True)
        if pd.isna(ts):
            continue
        if start.tzinfo is not None and ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        elif start.tzinfo is None and ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        if start <= ts <= end:
            kept.append(row)
    return kept


def trim_cold_run_rows(rows: list[dict], *, limit: int = _COLD_RUN_TABLE_LIMIT) -> list[dict]:
    if len(rows) <= limit:
        return rows

    def _sort_key(row: dict) -> float:
        for key in ("score", "z_score", "anomaly_score"):
            val = row.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return 0.0

    ranked = sorted(rows, key=_sort_key, reverse=True)
    return ranked[:limit]
