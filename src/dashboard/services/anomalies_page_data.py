"""Cached data loading and transforms for the Anomaly Detection page."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard.constants import (
    CACHE_MAX_ENTRIES_FRAME,
    CACHE_TTL_LONG,
    CACHE_TTL_MEDIUM,
    CACHE_TTL_SHORT,
    DEFAULT_ANOMALY_THRESHOLD,
)

from src.api.services.anomaly_ranking import anomaly_rank_key
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
from src.dashboard.services.tz_normalize import strip_tz

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


@st.cache_data(ttl=CACHE_TTL_LONG, show_spinner=False, max_entries=CACHE_MAX_ENTRIES_FRAME)
def load_scored_window(
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
    upload_job_id: str | None,
    _inf_token: str,
    _inf_results: dict | None,
) -> tuple[pd.DataFrame, bool]:
    """Load the scored window data from the database."""
    del upload_job_id, _inf_token
    window_start = pd.Timestamp(start_iso)
    window_end = pd.Timestamp(end_iso)
    return resolve_window_dataframe(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start_iso=start_iso,
        end_iso=end_iso,
        window_start=window_start,
        window_end=window_end,
        upload_job_id=None,
        _inf_results=_inf_results,
    )


@st.cache_data(ttl=CACHE_TTL_SHORT, show_spinner=False, max_entries=CACHE_MAX_ENTRIES_FRAME)
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
    """Union DB and upload timestamp bounds so filters span all available data.

    DB bounds come back tz-naive while upload bounds can be tz-aware (UTC);
    comparing the two raises TypeError, so every bound is stripped to tz-naive
    before the min/max union. The page's date pickers operate tz-naive too, so
    a naive union keeps the whole pipeline on one tz state.
    """
    upload_bounds = bounds_from_upload_results(inf_results) if inf_results else None
    if upload_bounds:
        upload_bounds = (strip_tz(upload_bounds[0]), strip_tz(upload_bounds[1]))
    if _upload_only_results(inf_results) and upload_bounds:
        return upload_bounds

    min_iso, max_iso = load_timestamp_bounds(tenant_id, greenhouse_id)

    if min_iso and max_iso:
        db_min, db_max = strip_tz(pd.Timestamp(min_iso)), strip_tz(pd.Timestamp(max_iso))
        if upload_bounds:
            u_min, u_max = upload_bounds
            return min(db_min, u_min), max(db_max, u_max)
        return db_min, db_max

    return upload_bounds


@st.cache_data(ttl=CACHE_TTL_LONG, show_spinner=False, max_entries=CACHE_MAX_ENTRIES_FRAME)
def load_upload_score_frame(
    tenant_id: str,
    greenhouse_id: str,
    sequences_processed: int,
    score_count: int,
    _results: dict,
) -> pd.DataFrame:
    # tenant_id, greenhouse_id, sequences_processed, score_count are cache-key
    # differentiators only; _results is underscore-prefixed so Streamlit skips
    # hashing the large inference dict (avoids pickling 100k+ score arrays on
    # every rerun while the scalar args still uniquely identify the payload).
    del tenant_id, greenhouse_id, sequences_processed, score_count
    return build_sensor_dataframe_from_upload(_results)


@st.cache_data(ttl=CACHE_TTL_SHORT, show_spinner=False, max_entries=CACHE_MAX_ENTRIES_FRAME)
def enrich_anomalies_table(
    _anomalies: pd.DataFrame,
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> pd.DataFrame:
    # _anomalies is underscore-prefixed so Streamlit skips hashing the DataFrame.
    # pandas cannot hash cells that contain lists or dicts (from RawReading.data
    # JSON columns), which caused Streamlit to fall back to pickling the entire
    # frame on every rerun.  The remaining scalar args (tenant/time range) already
    # uniquely identify the incident set used for enrichment; start_iso/end_iso are
    # computed from the top-anomaly timestamps in the caller, so they change
    # whenever the relevant anomaly set changes (e.g. threshold slider move).
    if _anomalies.empty:
        return _anomalies
    incidents = fetch_incidents_in_range(tenant_id, greenhouse_id, start_iso, end_iso)
    return enrich_dataframe(_anomalies, incidents)


def bounds_from_upload_results(results: dict) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Min/max upload timestamps without materializing the full score frame.

    resolve_data_bounds runs on every page render, so rebuilding the entire
    ~105k-row upload frame here (a per-row Python to_datetime loop) re-processed
    the whole saved upload each time. Bounds only need the timestamps aligned to
    a score, resolved in a single vectorized to_datetime pass. Mirrors the
    pairing in build_sensor_dataframe_from_upload: scores[i] ↔ sequence_timestamps[i].
    """
    scores = results.get("anomaly_scores") or []
    seq_ts = results.get("sequence_timestamps") or []
    if not scores or not seq_ts:
        return None
    aligned_ts = seq_ts[: len(scores)]
    ts = pd.to_datetime(pd.Series(aligned_ts), errors="coerce", utc=True).dropna()
    if ts.empty:
        return None
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
    envelope, using_fallback = load_scored_window(
        tenant_id,
        greenhouse_id,
        start_iso,
        end_iso,
        upload_job_id,
        _inf_cache_token(_inf_results),
        _inf_results,
    )
    df_window = apply_datetime_filter(envelope, window_start, window_end)
    effective_threshold = _effective_anomaly_threshold(threshold, using_fallback, _inf_results)
    anomalies = filter_anomalies(df_window, effective_threshold)
    return df_window, anomalies, using_fallback


def _effective_anomaly_threshold(
    threshold: float, using_fallback: bool, inf_results: dict | None
) -> float:
    """Resolve the score cutoff, preferring the model's calibrated value.

    The raw dashboard default (0.8) sits far above the squashed score scale
    (scores are ``raw / ANOMALY_SCALING_FACTOR`` clipped to [0, 1]; a real day
    tops out near 0.2), so filtering a *persisted* upload at 0.8 hides every
    anomaly the model actually found. When the upload provides a calibrated
    ``threshold_used`` and the user has NOT overridden the slider on the
    Thresholds page (i.e. it is still the default), use the calibrated cutoff —
    on both the DB and upload-only paths. An explicit user threshold always wins.
    """
    calibrated = (inf_results or {}).get("threshold_used")
    if calibrated is None:
        return threshold
    user_overrode = threshold != DEFAULT_ANOMALY_THRESHOLD
    if using_fallback or not user_overrode:
        return float(calibrated)
    return threshold


@st.cache_data(ttl=CACHE_TTL_SHORT, show_spinner=False, max_entries=CACHE_MAX_ENTRIES_FRAME)
def load_enriched_top_anomalies(
    _anomalies: pd.DataFrame,
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> pd.DataFrame:
    """Cache enriched anomaly rows for the Top Anomalies table.

    _anomalies is underscore-prefixed so Streamlit skips hashing the DataFrame
    (see enrich_anomalies_table for the full rationale).
    """
    return enrich_anomalies_table(
        _anomalies, tenant_id, greenhouse_id, start_iso, end_iso
    )





def clear_anomaly_page_cache() -> None:
    """Invalidate cached anomaly-page queries and enrichment."""
    # load_readings_window and load_timestamp_bounds are plain pass-throughs;
    # their underlying functions in charts.py carry their own cache.
    load_upload_score_frame.clear()
    enrich_anomalies_table.clear()
    load_enriched_top_anomalies.clear()
    load_scored_window.clear()
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
    """Cap the display table, keeping the most severe rows.

    Ranked by ``anomaly_rank_key`` (severity, then detector tier, then
    |z_score|) rather than the raw canonical score, so an urgent stuck-fault
    freeze survives the cap ahead of a merely-high-scoring statistical
    warning. Works on dict rows sourced from a stored Job.result/
    JobPortion.result blob, including rows with no "detector" key at all
    (StatisticalAnomalyDetail dicts from statistical_summary.anomalies) —
    anomaly_rank_key degrades gracefully to (severity_rank, |z|) for those.
    """
    if len(rows) <= limit:
        return rows

    ranked = sorted(rows, key=anomaly_rank_key, reverse=True)
    return ranked[:limit]
