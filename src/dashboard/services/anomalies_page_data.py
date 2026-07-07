"""Cached data loading and transforms for the Anomaly Detection page."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard.constants import CACHE_TTL_LONG, CACHE_TTL_MEDIUM, CACHE_TTL_SHORT

from src.dashboard.components.charts import (
    apply_datetime_filter,
    merge_anomaly_scores_into_readings,
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


def _has_usable_anomaly_scores(df: pd.DataFrame) -> bool:
    if df.empty or "anomaly_score" not in df.columns:
        return False
    scores = pd.to_numeric(df["anomaly_score"], errors="coerce")
    return bool(scores.notna().any())


def _upload_score_frame_for_window(
    tenant_id: str,
    greenhouse_id: str,
    inf_results: dict,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> pd.DataFrame:
    upload_frame = load_upload_score_frame(
        tenant_id,
        greenhouse_id,
        int(inf_results.get("sequences_processed", 0) or 0),
        len(inf_results.get("anomaly_scores") or []),
        inf_results,
    )
    if upload_frame.empty:
        return upload_frame
    return apply_datetime_filter(upload_frame, window_start, window_end)


def _upload_only_results(inf_results: dict | None) -> bool:
    """True when inference exists only in the upload cache (not yet in the DB)."""
    return bool(inf_results) and not inf_results.get("data_persisted")


def _inf_cache_token(inf_results: dict | None) -> str:
    """Stable cache key fragment for upload inference payloads."""
    if not inf_results:
        return "none"
    return "|".join(
        [
            str(inf_results.get("job_id") or ""),
            str(inf_results.get("sequences_processed", 0)),
            str(len(inf_results.get("anomaly_scores") or [])),
            str(inf_results.get("data_persisted")),
            str(inf_results.get("threshold_used")),
        ]
    )


@st.cache_data(ttl=CACHE_TTL_LONG, show_spinner=False)
def load_scored_envelope(
    tenant_id: str,
    greenhouse_id: str,
    bounds_min_iso: str,
    bounds_max_iso: str,
    upload_job_id: str | None,
    _inf_token: str,
    _inf_results: dict | None,
) -> tuple[pd.DataFrame, bool]:
    """Load the full scored envelope once; callers slice in-memory per filter."""
    del upload_job_id, _inf_token
    window_start = pd.Timestamp(bounds_min_iso)
    window_end = pd.Timestamp(bounds_max_iso)
    return resolve_window_dataframe(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start_iso=bounds_min_iso,
        end_iso=bounds_max_iso,
        window_start=window_start,
        window_end=window_end,
        upload_job_id=None,
        _inf_results=_inf_results,
    )


@st.cache_data(ttl=CACHE_TTL_SHORT, show_spinner=False)
def resolve_window_dataframe(
    *,
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    upload_job_id: str | None,
    _inf_results: dict | None,
) -> tuple[pd.DataFrame, bool]:
    """Load the active window, merging upload scores when DB rows lack them.

    Returns (dataframe, using_upload_fallback). Fallback is True only when the
    primary series comes from the cold-run upload rather than persisted readings.
    """
    if _upload_only_results(_inf_results):
        upload_frame = _upload_score_frame_for_window(
            tenant_id, greenhouse_id, _inf_results, window_start, window_end
        )
        if not upload_frame.empty:
            # Cast upload frame scores to numeric immediately.
            upload_frame["anomaly_score"] = pd.to_numeric(upload_frame["anomaly_score"], errors="coerce")
            return upload_frame, True

    df_window = load_readings_window(tenant_id, greenhouse_id, start_iso, end_iso)
    upload_frame = (
        _upload_score_frame_for_window(
            tenant_id, greenhouse_id, _inf_results, window_start, window_end
        )
        if _inf_results
        else pd.DataFrame()
    )

    def _finalize(df: pd.DataFrame, fallback: bool) -> tuple[pd.DataFrame, bool]:
        if not df.empty and "anomaly_score" in df.columns:
            df["anomaly_score"] = pd.to_numeric(df["anomaly_score"], errors="coerce")
        return df, fallback

    if not df_window.empty:
        if not upload_frame.empty:
            merged = merge_anomaly_scores_into_readings(
                df_window, upload_frame, tenant_id, greenhouse_id
            )
            if _has_usable_anomaly_scores(merged):
                return _finalize(merged, False)
            if _has_usable_anomaly_scores(upload_frame):
                return _finalize(upload_frame, True)
        if _has_usable_anomaly_scores(df_window):
            return _finalize(df_window, False)
        if not upload_frame.empty and _has_usable_anomaly_scores(upload_frame):
            return _finalize(upload_frame, True)
        return _finalize(df_window, False)

    if not upload_frame.empty:
        return _finalize(upload_frame, True)
    return pd.DataFrame(), False


def resolve_data_bounds(
    tenant_id: str,
    greenhouse_id: str,
    inf_results: dict | None,
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Union DB and upload timestamp bounds so filters span all available data."""
    upload_bounds = bounds_from_upload_results(inf_results) if inf_results else None
    if _upload_only_results(inf_results) and upload_bounds:
        return upload_bounds

    min_iso, max_iso = load_timestamp_bounds(tenant_id, greenhouse_id)

    if min_iso and max_iso:
        db_min, db_max = pd.Timestamp(min_iso), pd.Timestamp(max_iso)
        if upload_bounds:
            u_min, u_max = upload_bounds
            return min(db_min, u_min), max(db_max, u_max)
        return db_min, db_max

    return upload_bounds


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


def load_anomalies_for_window(
    *,
    tenant_id: str,
    greenhouse_id: str,
    bounds_min_iso: str,
    bounds_max_iso: str,
    start_iso: str,
    end_iso: str,
    threshold: float,
    upload_job_id: str | None,
    _inf_results: dict | None,
) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    """Slice the scored envelope in-memory, then filter anomalies."""
    window_start = pd.Timestamp(start_iso)
    window_end = pd.Timestamp(end_iso)
    envelope, using_fallback = load_scored_envelope(
        tenant_id,
        greenhouse_id,
        bounds_min_iso,
        bounds_max_iso,
        upload_job_id,
        _inf_cache_token(_inf_results),
        _inf_results,
    )
    df_window = apply_datetime_filter(envelope, window_start, window_end)
    effective_threshold = threshold
    if using_fallback and _inf_results:
        effective_threshold = float(_inf_results.get("threshold_used", threshold))
    anomalies = filter_anomalies(df_window, effective_threshold)
    return df_window, anomalies, using_fallback


@st.cache_data(ttl=CACHE_TTL_SHORT, show_spinner=False)
def load_enriched_top_anomalies(
    anomalies: pd.DataFrame,
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> pd.DataFrame:
    """Cache enriched anomaly rows for the Top Anomalies table."""
    return enrich_anomalies_table(
        anomalies, tenant_id, greenhouse_id, start_iso, end_iso
    )


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
    load_enriched_top_anomalies.clear()
    load_scored_envelope.clear()
    resolve_window_dataframe.clear()


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
