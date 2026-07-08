"""Cold-run inference results display for the Anomaly Detection page."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from src.dashboard.constants import DEFAULT_ANOMALY_THRESHOLD

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from src.dashboard.components.anomaly_table import render as render_anomaly_table
from src.dashboard.services.diagnosis_enricher import enrich_cold_run_rows

from src.dashboard.services.upload_results_store import (
    build_cache_payload,
    delete_upload_cache,
    read_upload_cache,
    write_upload_cache,
)

_INF_RESULTS_KEY = "inf_upload_results"
_JOBS_FALLBACK_CHECKED_KEY = "_upload_results_jobs_checked"
_JOBS_FALLBACK_RECHECK_SECONDS = 30.0
_COLD_RUN_ENRICH_CACHE_KEY = "_cold_run_enrich_cache"
_UPLOAD_DISMISSED_KEY = "_upload_results_dismissed"
_PENDING_UPLOAD_JOB_KEY = "pending_upload_job"
_UPLOAD_FILE_WIDGET_KEY = "anomaly_upload_file"


def upload_scope_key(tenant_id: str, greenhouse_id: str) -> str:
    return f"{tenant_id}|{greenhouse_id}"


def is_upload_dismissed(tenant_id: str, greenhouse_id: str) -> bool:
    """True when the user explicitly cleared upload results for this scope."""
    dismissed = st.session_state.get(_UPLOAD_DISMISSED_KEY)
    if not isinstance(dismissed, set):
        return False
    return upload_scope_key(tenant_id, greenhouse_id) in dismissed


def mark_upload_dismissed(tenant_id: str, greenhouse_id: str) -> None:
    dismissed = st.session_state.setdefault(_UPLOAD_DISMISSED_KEY, set())
    dismissed.add(upload_scope_key(tenant_id, greenhouse_id))


def clear_upload_dismissed(tenant_id: str, greenhouse_id: str) -> None:
    dismissed = st.session_state.get(_UPLOAD_DISMISSED_KEY)
    if isinstance(dismissed, set):
        dismissed.discard(upload_scope_key(tenant_id, greenhouse_id))


def reset_upload_ui_state() -> None:
    """Drop uploader widgets and async job tracking after a clear."""
    st.session_state.pop(_UPLOAD_FILE_WIDGET_KEY, None)
    st.session_state.pop(_PENDING_UPLOAD_JOB_KEY, None)


def clear_upload_analysis(
    tenant_id: str,
    greenhouse_id: str,
    *,
    clear_reading_caches: bool = True,
) -> None:
    """Remove cold-run upload analysis for a tenant/greenhouse (session + disk)."""
    delete_upload_cache(tenant_id, greenhouse_id)
    clear_cold_run_enrich_cache()
    mark_upload_dismissed(tenant_id, greenhouse_id)
    reset_upload_ui_state()

    existing = st.session_state.get(_INF_RESULTS_KEY)
    if (
        isinstance(existing, dict)
        and existing.get("tenant_id") == tenant_id
        and existing.get("greenhouse_id") == greenhouse_id
    ):
        st.session_state.pop(_INF_RESULTS_KEY, None)

    scope = upload_scope_key(tenant_id, greenhouse_id)
    checked = st.session_state.get(_JOBS_FALLBACK_CHECKED_KEY)
    if isinstance(checked, dict):
        checked.pop(scope, None)

    if clear_reading_caches:
        from src.dashboard.components.charts import clear_sensor_cache, get_available_greenhouses
        from src.dashboard.services.anomalies_page_data import clear_anomaly_page_cache

        clear_sensor_cache()
        clear_anomaly_page_cache()
        get_available_greenhouses.clear()


def results_signature(results: dict) -> str:
    """Stable fingerprint for caching enrichment derived from upload results."""
    stat = results.get("statistical_summary") or {}
    stat_count = len(stat.get("anomalies") or []) if isinstance(stat, dict) else 0
    return (
        f"{results.get('sequences_processed', 0)}|"
        f"{results.get('anomalies_detected', 0)}|"
        f"{len(results.get('anomaly_scores') or [])}|"
        f"{len(results.get('high_anomalies') or [])}|"
        f"{stat_count}|"
        f"{results.get('threshold_used', '')}"
    )


def enrich_cold_run_rows_cached(
    results: dict,
    rows: list[dict],
    incidents: list[dict] | None,
    *,
    cache_scope: str,
) -> pd.DataFrame:
    """Cache cold-run row enrichment in session for repeat page visits."""
    if not rows:
        return pd.DataFrame()

    incident_key = (
        f"{len(incidents)}|{incidents[0].get('id')}|{incidents[-1].get('id')}"
        if incidents
        else "none"
    )
    row_key = tuple(
        (
            str(row.get("timestamp", "")),
            str(row.get("detector", "")),
            str(row.get("column", "")),
            str(row.get("score", "")),
        )
        for row in rows[:50]
    )
    cache_key = f"{cache_scope}|{results_signature(results)}|{incident_key}|{row_key}"

    store: dict = st.session_state.setdefault(_COLD_RUN_ENRICH_CACHE_KEY, {})
    cached = store.get(cache_key)
    if isinstance(cached, pd.DataFrame):
        return cached.copy()

    enriched = enrich_cold_run_rows(rows, incidents)
    store[cache_key] = enriched
    if len(store) > 8:
        for stale_key in list(store.keys())[:-8]:
            store.pop(stale_key, None)
    return enriched


def clear_cold_run_enrich_cache() -> None:
    st.session_state.pop(_COLD_RUN_ENRICH_CACHE_KEY, None)


def results_from_job_list(jobs: list[dict]) -> dict | None:
    """Newest usable upload result from a jobs-API listing (newest first)."""
    from src.dashboard.services.async_upload import (
        TERMINAL_JOB_STATUSES,
        extract_upload_results,
    )

    for job in jobs:
        if job.get("status") not in TERMINAL_JOB_STATUSES:
            continue
        results, _error = extract_upload_results(job)
        if results is not None:
            return results
    return None


def _fetch_results_from_jobs_api(tenant_id: str, greenhouse_id: str) -> dict | None:
    """Rehydrate the last upload's results from the durable jobs table.

    Session state and the container-local disk cache are ephemeral (redeploys
    wipe them; every browser session is separate), but the finished job result
    lives in Postgres — treat it as the source of truth on cache miss.
    Failure-tolerant: any API error just means no fallback this render.
    """
    from src.dashboard.services.api_client import DashboardApiClient

    try:
        jobs = DashboardApiClient(api_key=st.session_state.get("api_key")).list_jobs(
            tenant_id,
            greenhouse_id=greenhouse_id,
            job_type="upload_csv",
            limit=5,
        )
    except Exception:
        return None
    return results_from_job_list(jobs)


def _as_dict_list(items: Any) -> list[dict]:
    if not items:
        return []
    rows: list[dict] = []
    for item in items:
        if isinstance(item, dict):
            rows.append(dict(item))
        elif hasattr(item, "model_dump"):
            rows.append(item.model_dump())
    return rows


def _statistical_anomalies_from_results(results: dict) -> list[dict]:
    """Read statistical_summary.anomalies, with fallback from high_anomalies."""
    stat_summary = results.get("statistical_summary") or {}
    if hasattr(stat_summary, "model_dump"):
        stat_summary = stat_summary.model_dump()
    if isinstance(stat_summary, dict):
        anomalies = _as_dict_list(stat_summary.get("anomalies"))
        if anomalies:
            return anomalies

    stat_rows: list[dict] = []
    for row in _as_dict_list(results.get("high_anomalies")):
        detector = str(row.get("detector", ""))
        if detector in {"statistical", "lstm+statistical"} and row.get("column"):
            stat_rows.append(row)
    return stat_rows


def _high_anomalies_from_results(results: dict) -> list[dict]:
    return _as_dict_list(results.get("high_anomalies"))


def stat_rows_to_jump_rows(stat_anomalies: list[dict]) -> list[dict]:
    """Ensure statistical summary rows match high_anomalies shape."""
    rows: list[dict] = []
    for item in stat_anomalies:
        row = dict(item)
        row.setdefault("detector", "statistical")
        row.setdefault("score", min(1.0, float(row.get("z_score", 0)) / 5.0))
        rows.append(row)
    return rows


def save_inference_results(
    results: dict,
    tenant_id: str,
    greenhouse_id: str,
    *,
    filename: str | None = None,
) -> None:
    from src.dashboard.services.anomalies_page_data import clear_anomaly_page_cache

    payload = build_cache_payload(
        results,
        tenant_id,
        greenhouse_id,
        filename=filename,
    )
    st.session_state[_INF_RESULTS_KEY] = payload
    write_upload_cache(payload)
    clear_upload_dismissed(tenant_id, greenhouse_id)
    clear_anomaly_page_cache()
    clear_cold_run_enrich_cache()


def clear_inference_results() -> None:
    existing = st.session_state.get(_INF_RESULTS_KEY)
    if isinstance(existing, dict):
        tenant_id = existing.get("tenant_id")
        greenhouse_id = existing.get("greenhouse_id")
        if tenant_id and greenhouse_id:
            clear_upload_analysis(str(tenant_id), str(greenhouse_id))
            return
    st.session_state.pop(_INF_RESULTS_KEY, None)
    reset_upload_ui_state()


def clear_inference_results_for(tenant_id: str, greenhouse_id: str) -> None:
    """Backward-compatible alias for clear_upload_analysis."""
    clear_upload_analysis(tenant_id, greenhouse_id)


def load_inference_results(tenant_id: str, greenhouse_id: str) -> dict | None:
    """Restore cold-run upload results: session → disk cache → jobs API."""
    if is_upload_dismissed(tenant_id, greenhouse_id):
        return None

    existing = st.session_state.get(_INF_RESULTS_KEY)
    if (
        isinstance(existing, dict)
        and existing.get("tenant_id") == tenant_id
        and existing.get("greenhouse_id") == greenhouse_id
    ):
        return existing

    cached = read_upload_cache(tenant_id, greenhouse_id)
    if cached:
        st.session_state[_INF_RESULTS_KEY] = cached
        return cached

    scope = f"{tenant_id}|{greenhouse_id}"
    checked: dict = st.session_state.setdefault(_JOBS_FALLBACK_CHECKED_KEY, {})
    now = time.time()
    last_checked = checked.get(scope, 0.0)
    if now - last_checked < _JOBS_FALLBACK_RECHECK_SECONDS:
        return None
    checked[scope] = now

    results = _fetch_results_from_jobs_api(tenant_id, greenhouse_id)
    if results is None:
        return None
    payload = build_cache_payload(results, tenant_id, greenhouse_id, filename=None)
    st.session_state[_INF_RESULTS_KEY] = payload
    write_upload_cache(payload)
    return payload


def get_recent_inference_results() -> dict | None:
    """Return the latest upload payload from session regardless of sidebar selection."""
    existing = st.session_state.get(_INF_RESULTS_KEY)
    return existing if isinstance(existing, dict) else None


def build_sensor_dataframe_from_upload(results: dict) -> pd.DataFrame:
    """Build a minimal timestamp + anomaly_score frame from cold-run upload results.

    Vectorized on purpose: this runs on the anomaly page and the Monitoring merge
    path, over the full upload (~105k score/timestamp pairs). The previous per-row
    ``pd.to_datetime`` loop paid that call's large fixed overhead once per row and
    blocked the Streamlit script for minutes on a real upload. A single
    ``to_datetime`` pass (mirroring bounds_from_upload_results) does the same work
    in one shot. ``errors="coerce"`` maps ""/"unknown"/bad strings to NaT, which we
    then drop as score/timestamp pairs — preserving the old row-level filtering.
    """
    scores = results.get("anomaly_scores") or []
    seq_ts = results.get("sequence_timestamps") or []
    pair_count = min(len(scores), len(seq_ts))
    if pair_count == 0:
        return pd.DataFrame()

    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                pd.Series(seq_ts[:pair_count]), errors="coerce", utc=True
            ),
            "anomaly_score": pd.to_numeric(
                pd.Series(scores[:pair_count]), errors="coerce"
            ),
        }
    )
    frame = frame[frame["timestamp"].notna() & frame["anomaly_score"].notna()]
    if frame.empty:
        return pd.DataFrame()
    return frame.reset_index(drop=True)


def get_inference_results() -> dict | None:
    return st.session_state.get(_INF_RESULTS_KEY)


def resolve_upload_results(inference_results: dict | None) -> dict | None:
    """Normalize cold-run payload (unwrap saved session wrapper when needed)."""
    if not inference_results:
        saved = get_inference_results()
        inference_results = saved["results"] if saved else None
    if not inference_results:
        return None
    if "results" in inference_results and "anomaly_scores" not in inference_results:
        return inference_results["results"]
    return inference_results


def _filter_upload_results_by_window(
    inference_results: dict,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> dict:
    """Return upload results limited to the active date window."""
    from src.dashboard.services.anomalies_page_data import rows_in_window

    frame = build_sensor_dataframe_from_upload(inference_results)
    if frame.empty:
        return inference_results

    from src.dashboard.components.charts import apply_datetime_filter

    filtered = apply_datetime_filter(frame, window_start, window_end)
    slim = dict(inference_results)
    if filtered.empty:
        slim["anomaly_scores"] = []
        slim["sequence_timestamps"] = []
        slim["high_anomalies"] = []
        stat = slim.get("statistical_summary")
        if isinstance(stat, dict):
            stat = dict(stat)
            stat["anomalies"] = []
            slim["statistical_summary"] = stat
        return slim

    scores = inference_results.get("anomaly_scores") or []
    seq_ts = inference_results.get("sequence_timestamps") or []
    filtered_scores: list[float] = []
    filtered_ts: list[str] = []
    for score, raw_ts in zip(scores, seq_ts):
        if not raw_ts or raw_ts == "unknown":
            continue
        ts = pd.to_datetime(raw_ts, errors="coerce", utc=True)
        if pd.isna(ts):
            continue
        row_frame = pd.DataFrame({"timestamp": [ts], "anomaly_score": [score]})
        if not apply_datetime_filter(row_frame, window_start, window_end).empty:
            filtered_scores.append(float(score))
            filtered_ts.append(str(raw_ts))

    slim["anomaly_scores"] = filtered_scores
    slim["sequence_timestamps"] = filtered_ts
    slim["high_anomalies"] = rows_in_window(
        list(slim.get("high_anomalies") or []),
        window_start,
        window_end,
    )
    stat = slim.get("statistical_summary")
    if isinstance(stat, dict):
        stat = dict(stat)
        stat["anomalies"] = rows_in_window(
            list(stat.get("anomalies") or []),
            window_start,
            window_end,
        )
        slim["statistical_summary"] = stat
    return slim


def plot_upload_lstm_chart(
    inference_results: dict,
    *,
    x_range: tuple[pd.Timestamp, pd.Timestamp] | None = None,
) -> go.Figure | None:
    """LSTM score line from cold-run upload, optionally limited to *x_range*."""
    from src.dashboard.components.charts import _apply_chart_theme

    payload = inference_results
    if x_range is not None:
        payload = _filter_upload_results_by_window(
            inference_results, x_range[0], x_range[1]
        )

    scores = payload.get("anomaly_scores") or []
    if not scores:
        return None

    threshold_used = float(payload.get("threshold_used", DEFAULT_ANOMALY_THRESHOLD))
    threshold_method = payload.get("threshold_method", "file")
    seq_ts = payload.get("sequence_timestamps") or []

    y_vals = scores
    if len(seq_ts) == len(scores):
        ts_series = pd.to_datetime(
            pd.Series(seq_ts).replace("unknown", pd.NA),
            errors="coerce",
            utc=True,
        )
        if ts_series.notna().any():
            line_x = ts_series
            x_title = "Time"
        else:
            line_x = pd.Series(range(len(scores)))
            x_title = "Sequence index"
    else:
        line_x = pd.Series(range(len(scores)))
        x_title = "Sequence index"

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=line_x,
            y=y_vals,
            mode="lines",
            name="Anomaly Score",
            line=dict(color="#60a5fa", width=1.5),
        )
    )
    fig.add_hline(
        y=threshold_used,
        line_dash="dash",
        line_color="red",
        annotation_text=f"Threshold ({threshold_method})",
    )
    y_max = max(float(max(y_vals)), threshold_used * 1.1, 0.1)
    fig.update_layout(
        xaxis_title=x_title,
        yaxis_title="Anomaly Score",
        yaxis=dict(range=[0, min(1.05, y_max * 1.15)]),
        height=420,
    )
    return _apply_chart_theme(fig, bottom_margin=60)


def _on_clear_upload_click() -> None:
    """Instant clear via button callback — avoids rerun-then-rehydrate race."""
    existing = st.session_state.get(_INF_RESULTS_KEY)
    if not isinstance(existing, dict):
        st.session_state.pop(_INF_RESULTS_KEY, None)
        reset_upload_ui_state()
        return
    tenant_id = existing.get("tenant_id")
    greenhouse_id = existing.get("greenhouse_id")
    if tenant_id and greenhouse_id:
        clear_upload_analysis(str(tenant_id), str(greenhouse_id))
        return
    st.session_state.pop(_INF_RESULTS_KEY, None)
    reset_upload_ui_state()


def render_upload_status_bar(saved_inf: dict | None) -> None:
    """Single-line upload status with a clear action that cannot rehydrate from jobs."""
    status_col, clear_col = st.columns([6, 1])
    if saved_inf:
        name = saved_inf.get("filename") or "last upload"
        tenant_id = saved_inf["tenant_id"]
        greenhouse_id = saved_inf["greenhouse_id"]
        results = saved_inf["results"]
        detected = results.get("anomalies_detected", 0)
        persisted = bool(results.get("data_persisted"))
        has_row_counts = "sensor_rows_uploaded" in results and "raw_rows_uploaded" in results
        sensor_rows_uploaded = results.get("sensor_rows_uploaded", 0)
        raw_rows_uploaded = results.get("raw_rows_uploaded", 0)
        persist_wrote_nothing = (
            persisted
            and has_row_counts
            and sensor_rows_uploaded == 0
            and raw_rows_uploaded == 0
        )
        processed_at = saved_inf.get("processed_at")
        processed_note = ""
        if processed_at:
            try:
                processed_note = (
                    f" · cached {pd.Timestamp(processed_at).strftime('%d %b %H:%M')}"
                )
            except (TypeError, ValueError):
                processed_note = ""
        status_col.caption(
            f"Showing saved analysis: **{name}** · `{tenant_id}` / `{greenhouse_id}` · "
            f"{detected} anomalies{processed_note} — no re-inference on page load"
        )
        if persist_wrote_nothing:
            status_col.error(
                "Persistence was requested but 0 rows were written to the database "
                "(sensor_readings/raw_readings are empty). Check the worker service "
                "logs, or re-upload."
            )
        elif persisted:
            status_col.caption(
                "**Clear upload** removes the saved analysis view only. "
                "Persisted sensor readings stay in the database — use "
                "**Upload & Operations → Clear all data** to delete them."
            )
        clear_col.button(
            "Clear upload",
            key="clear_inf_results",
            on_click=_on_clear_upload_click,
        )
        return
    if is_upload_dismissed(
        st.session_state.get("selected_tenant", ""),
        st.session_state.get("selected_greenhouse", ""),
    ):
        status_col.caption(
            "Upload analysis cleared for this greenhouse. "
            "Upload a new CSV or restore persisted readings below."
        )
    else:
        status_col.caption(
            "No cold-run upload for this greenhouse — expand **Upload & find anomalies** above to analyze a CSV."
        )
    clear_col.write("")


def _lstm_chart_message(text: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis_title="Anomaly Score",
        height=420,
        annotations=[
            dict(
                text=text,
                xref="paper",
                yref="paper",
                x=0.5,
                y=0.5,
                showarrow=False,
                font=dict(size=13, color="#94a3b8"),
            )
        ],
    )
    from src.dashboard.components.charts import _apply_chart_theme
    return _apply_chart_theme(fig, bottom_margin=60)


def _empty_lstm_chart() -> go.Figure:
    return _lstm_chart_message(
        "No upload results — run Upload & find anomalies to see LSTM scores here."
    )


def _missing_scores_lstm_chart(sequences_processed: int) -> go.Figure:
    if sequences_processed > 0:
        text = (
            f"Upload processed {sequences_processed:,} sequences but no LSTM score series "
            "was returned — re-run Upload & find anomalies."
        )
    else:
        text = (
            "Upload results are missing LSTM anomaly scores — re-run Upload & find anomalies."
        )
    return _lstm_chart_message(text)


def _looks_like_upload_score_frame(df: pd.DataFrame) -> bool:
    """True when df was built from cold-run upload (timestamp + anomaly_score only)."""
    if df.empty:
        return False
    columns = set(df.columns)
    return columns == {"timestamp", "anomaly_score"}


def render_anomaly_score_charts(
    df: pd.DataFrame,
    threshold: float,
    inference_results: dict | None,
    *,
    x_range: tuple[pd.Timestamp, pd.Timestamp] | None = None,
) -> None:
    """Render timeline + optional LSTM upload chart."""
    from src.dashboard.components.charts import plot_anomaly_timeline
    from src.dashboard.services.anomalies_page_data import (
        _has_usable_anomaly_scores,
    )

    upload_results = resolve_upload_results(inference_results)
    using_upload_fallback = upload_results is not None and _looks_like_upload_score_frame(df)
    chart_df = df

    upload_score_df = pd.DataFrame()
    if upload_results and not using_upload_fallback:
        upload_score_df = build_sensor_dataframe_from_upload(upload_results)
        if x_range is not None and not upload_score_df.empty:
            from src.dashboard.components.charts import apply_datetime_filter

            upload_score_df = apply_datetime_filter(
                upload_score_df, x_range[0], x_range[1]
            )

    timeline_df = chart_df
    timeline_threshold = threshold
    if (
        upload_results
        and not _has_usable_anomaly_scores(chart_df)
        and _has_usable_anomaly_scores(upload_score_df)
    ):
        timeline_df = upload_score_df
        timeline_threshold = float(
            upload_results.get("threshold_used", threshold)
        )
        st.markdown("#### Anomaly scores (from upload)")
        st.caption(
            "LSTM scores from your CSV upload in the selected date range. "
            f"Threshold used at upload: {timeline_threshold:.3f}."
        )
    elif using_upload_fallback:
        st.markdown("#### Anomaly scores (from upload)")
        st.caption(
            "Full LSTM score series from your CSV upload in the selected date range. "
            f"Threshold used at upload: {threshold:.3f}."
        )
    else:
        st.markdown("#### Stored anomalies")
        st.caption("Flagged anomalies from platform data in the selected date range.")

    st.plotly_chart(
        plot_anomaly_timeline(
            timeline_df,
            timeline_threshold,
            anomalies_only=not (
                using_upload_fallback or _has_usable_anomaly_scores(upload_score_df)
            ),
            x_range=x_range,
        ),
        width="stretch",
    )

    if using_upload_fallback:
        return

    st.markdown("#### LSTM anomaly scores (cold-run calibrated)")
    st.caption("Full LSTM score series from the most recent CSV upload for this greenhouse.")
    if upload_results:
        lstm_fig = plot_upload_lstm_chart(upload_results, x_range=x_range)
        if lstm_fig is not None:
            st.plotly_chart(lstm_fig, width="stretch", key="lstm_upload_chart")
        else:
            st.plotly_chart(
                _missing_scores_lstm_chart(
                    int(upload_results.get("sequences_processed", 0) or 0)
                ),
                width="stretch",
            )
    else:
        st.plotly_chart(_empty_lstm_chart(), width="stretch")


def render_inference_upload_summary(results: dict, tenant_id: str, greenhouse_id: str) -> None:
    """Detailed upload outcome (shown inside the upload expander only)."""
    threshold_used = results.get("threshold_used", DEFAULT_ANOMALY_THRESHOLD)
    threshold_method = results.get("threshold_method", "unknown")
    imputed = results.get("imputed_channels") or []

    st.success(
        f"Processed {results.get('sequences_processed', 0)} sequences. "
        f"Detected {results.get('anomalies_detected', 0)} anomalies "
        f"(threshold {float(threshold_used):.3f}, {threshold_method})."
    )

    if results.get("data_persisted"):
        rows = results.get("rows_uploaded", 0)
        sensor_rows = results.get("sensor_rows_uploaded", 0)
        raw_rows = results.get("raw_rows_uploaded", 0)
        st.info(
            f"Loaded **{rows:,}** rows for `{tenant_id}` / `{greenhouse_id}` "
            f"({sensor_rows:,} canonical, {raw_rows:,} raw)."
        )

    if imputed:
        st.warning(
            "Imputed sensors (not in CSV): "
            + ", ".join(imputed)
            + " — excluded from stuck-fault checks."
        )

    if results.get("model_trained") and results.get("model_id"):
        st.success(f"Trained and registered cold-start model `{results['model_id']}`.")
    elif results.get("training_skipped_reason") == "active_model_exists":
        active_id = results.get("model_id")
        if active_id:
            st.info(f"Scored with existing model `{active_id}`.")
        else:
            st.info("Scored with the active tenant model (training skipped).")


def scoped_upload_anomalies(
    results: dict,
    window_start: pd.Timestamp | None,
    window_end: pd.Timestamp | None,
) -> tuple[list[dict], list[dict], bool]:
    """(stat_rows, high_rows, used_fallback) for the cold-run tables.

    Rows are scoped to the active date window; when the window contains none,
    fall back to the full upload. The page window is anchored to DB bounds,
    which can be disjoint from the upload's timestamps (multiple files per
    tenant) — a real upload result must never hide behind an accidental
    empty window.
    """
    from src.dashboard.services.anomalies_page_data import trim_cold_run_rows

    scoped = results
    window_scoped = window_start is not None and window_end is not None
    if window_scoped:
        scoped = _filter_upload_results_by_window(results, window_start, window_end)
    stat_rows = trim_cold_run_rows(_statistical_anomalies_from_results(scoped))
    high_rows = trim_cold_run_rows(_high_anomalies_from_results(scoped))
    if window_scoped and not stat_rows and not high_rows:
        full_stat = trim_cold_run_rows(_statistical_anomalies_from_results(results))
        full_high = trim_cold_run_rows(_high_anomalies_from_results(results))
        if full_stat or full_high:
            return full_stat, full_high, True
    return stat_rows, high_rows, False


def render_cold_run_anomaly_tables(
    results: dict | None,
    tenant_id: str,
    greenhouse_id: str,
    *,
    incidents: list[dict] | None = None,
    on_feedback: Callable[..., None] | None = None,
    feedback_state_key: str = "feedback_results",
    available_columns: list[str] | None = None,
    source_df: pd.DataFrame | None = None,
    window_start: pd.Timestamp | None = None,
    window_end: pd.Timestamp | None = None,
) -> None:
    """Cold-run tables using the same columns and reasoning as Top Anomalies."""
    stat_anomalies: list[dict] = []
    high_anomalies: list[dict] = []
    if results:
        stat_anomalies, high_anomalies, used_fallback = scoped_upload_anomalies(
            results, window_start, window_end
        )
        if used_fallback:
            st.info(
                "No upload anomalies fall inside the selected date window — "
                f"showing all {len(high_anomalies)} anomalies "
                f"({len(stat_anomalies)} statistical) from the last upload instead. "
                "Adjust the date range above to narrow them down."
            )

    empty_hint = (
        "No anomalies in this section. Upload a CSV via **Upload & find anomalies** "
        "above to populate cold-run results."
    )
    uploaded_stat_msg = "No statistical anomalies in the last upload."
    uploaded_all_msg = "No merged anomalies in the last upload."

    enrich_scope = f"{tenant_id}|{greenhouse_id}"
    base_results = results or {}
    stat_enriched = enrich_cold_run_rows_cached(
        base_results,
        stat_rows_to_jump_rows(stat_anomalies),
        incidents,
        cache_scope=enrich_scope,
    )
    all_enriched = enrich_cold_run_rows_cached(
        base_results,
        high_anomalies,
        incidents,
        cache_scope=enrich_scope,
    )

    render_anomaly_table(
        stat_enriched,
        on_feedback=on_feedback,
        feedback_state_key=feedback_state_key,
        table_title="Statistical anomalies (per sensor)",
        table_key="inf_stat_anomalies",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        available_columns=available_columns,
        source_df=source_df,
        empty_message=uploaded_stat_msg if results else empty_hint,
        show_legend=False,
    )

    render_anomaly_table(
        all_enriched,
        on_feedback=on_feedback,
        feedback_state_key=feedback_state_key,
        table_title="All detected anomalies",
        table_key="inf_all_anomalies",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        available_columns=available_columns,
        source_df=source_df,
        empty_message=uploaded_all_msg if results else empty_hint,
        show_legend=False,
    )


def render_inference_anomaly_tables(
    results: dict,
    tenant_id: str,
    greenhouse_id: str,
) -> None:
    """Backward-compatible alias."""
    render_cold_run_anomaly_tables(results, tenant_id, greenhouse_id)
