"""Cold-run inference results display for the Anomaly Detection page."""

from __future__ import annotations

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
    payload = build_cache_payload(
        results,
        tenant_id,
        greenhouse_id,
        filename=filename,
    )
    st.session_state[_INF_RESULTS_KEY] = payload
    write_upload_cache(payload)


def clear_inference_results() -> None:
    existing = st.session_state.get(_INF_RESULTS_KEY)
    if isinstance(existing, dict):
        tenant_id = existing.get("tenant_id")
        greenhouse_id = existing.get("greenhouse_id")
        if tenant_id and greenhouse_id:
            clear_inference_results_for(str(tenant_id), str(greenhouse_id))
            return
    st.session_state.pop(_INF_RESULTS_KEY, None)


def clear_inference_results_for(tenant_id: str, greenhouse_id: str) -> None:
    """Remove cold-run upload results for a tenant/greenhouse (session + disk)."""
    delete_upload_cache(tenant_id, greenhouse_id)
    existing = st.session_state.get(_INF_RESULTS_KEY)
    if (
        isinstance(existing, dict)
        and existing.get("tenant_id") == tenant_id
        and existing.get("greenhouse_id") == greenhouse_id
    ):
        st.session_state.pop(_INF_RESULTS_KEY, None)


def load_inference_results(tenant_id: str, greenhouse_id: str) -> dict | None:
    """Restore cold-run upload results from session or on-disk cache."""
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

    return None


def get_recent_inference_results() -> dict | None:
    """Return the latest upload payload from session regardless of sidebar selection."""
    existing = st.session_state.get(_INF_RESULTS_KEY)
    return existing if isinstance(existing, dict) else None


def build_sensor_dataframe_from_upload(results: dict) -> pd.DataFrame:
    """Build a minimal timestamp + anomaly_score frame from cold-run upload results."""
    scores = results.get("anomaly_scores") or []
    if not scores:
        return pd.DataFrame()

    seq_ts = results.get("sequence_timestamps") or []
    records: list[dict[str, object]] = []
    for index, score in enumerate(scores):
        if index >= len(seq_ts):
            break
        raw_ts = seq_ts[index]
        if not raw_ts or raw_ts == "unknown":
            continue
        ts = pd.to_datetime(raw_ts, errors="coerce", utc=True)
        if pd.isna(ts):
            continue
        records.append({"timestamp": ts, "anomaly_score": float(score)})

    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


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


def render_upload_status_bar(saved_inf: dict | None) -> bool:
    """Single-line upload status; returns True when user clears results."""
    status_col, clear_col = st.columns([6, 1])
    if saved_inf:
        name = saved_inf.get("filename") or "last upload"
        tenant_id = saved_inf["tenant_id"]
        greenhouse_id = saved_inf["greenhouse_id"]
        detected = saved_inf["results"].get("anomalies_detected", 0)
        status_col.caption(
            f"Last cold-run: **{name}** · `{tenant_id}` / `{greenhouse_id}` · "
            f"{detected} anomalies detected"
        )
        return clear_col.button("Clear upload", key="clear_inf_results")
    status_col.caption(
        "No cold-run upload for this greenhouse — expand **Upload & find anomalies** above to analyze a CSV."
    )
    clear_col.write("")
    return False


def _lstm_chart_message(text: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        xaxis_title="Time",
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

    upload_results = resolve_upload_results(inference_results)
    using_upload_fallback = upload_results is not None and _looks_like_upload_score_frame(df)
    chart_df = df
    if not chart_df.empty and len(chart_df) > 2000:
        from src.dashboard.services.anomalies_page_data import downsample_for_chart

        chart_df = downsample_for_chart(chart_df)

    if using_upload_fallback:
        st.markdown("#### Anomaly scores (from upload)")
        st.caption(
            "Full LSTM score series from your CSV upload in the selected date range. "
            f"Threshold used at upload: {threshold:.3f}."
        )
        st.plotly_chart(
            plot_anomaly_timeline(
                chart_df,
                threshold,
                anomalies_only=False,
                x_range=x_range,
            ),
            use_container_width=True,
        )
        return

    st.markdown("#### Stored anomalies")
    st.caption("Flagged anomalies from platform data in the selected date range.")
    st.plotly_chart(
        plot_anomaly_timeline(
            chart_df,
            threshold,
            anomalies_only=True,
            x_range=x_range,
        ),
        use_container_width=True,
    )

    st.markdown("#### LSTM anomaly scores (cold-run calibrated)")
    st.caption("Full LSTM score series from the most recent CSV upload for this greenhouse.")
    if upload_results:
        lstm_fig = plot_upload_lstm_chart(upload_results, x_range=x_range)
        if lstm_fig is not None:
            st.plotly_chart(lstm_fig, use_container_width=True, key="lstm_upload_chart")
        else:
            st.plotly_chart(
                _missing_scores_lstm_chart(
                    int(upload_results.get("sequences_processed", 0) or 0)
                ),
                use_container_width=True,
            )
    else:
        st.plotly_chart(_empty_lstm_chart(), use_container_width=True)


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
    source_df: pd.DataFrame | None = None,
    available_columns: list[str] | None = None,
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

    stat_enriched = enrich_cold_run_rows(
        stat_rows_to_jump_rows(stat_anomalies),
        incidents,
    )
    all_enriched = enrich_cold_run_rows(high_anomalies, incidents)

    render_anomaly_table(
        stat_enriched,
        on_feedback=on_feedback,
        feedback_state_key=feedback_state_key,
        table_title="Statistical anomalies (per sensor)",
        table_key="inf_stat_anomalies",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        source_df=source_df,
        available_columns=available_columns,
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
        source_df=source_df,
        available_columns=available_columns,
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
