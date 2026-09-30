import logging
from typing import List

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.utils.profiling import timed
from src.dashboard.components.sensor_columns import get_all_sensor_columns
from src.dashboard.components.charts import (
    apply_datetime_filter,
    get_sensor_label,
    plot_combined_sensors,
    query_sensor_timestamp_bounds,
)
from src.dashboard.components.color_manager import get_chart_color_manager
from src.dashboard.components.data_table import render as render_data_table
from src.dashboard.components.data_availability import render_no_sensor_data_hint
from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.components.monitoring_jump import (
    apply_pending_jump_columns,
    apply_pending_jump_window,
    load_monitoring_window,
    peek_monitoring_prefetch_bounds,
    resolve_highlight_columns,
)
from src.dashboard.components.timeline import (
    init_monitoring_session_state,
    render_timeline_controls,
)

logger = logging.getLogger("dashboard.monitoring")

_LSTM_SEQ_LEN = 20
_DRIFT_WINDOW_STEPS = 72


def _normalized_y(df: pd.DataFrame, column: str, raw_val: float) -> float:
    series = pd.to_numeric(df[column], errors="coerce").dropna()
    if series.empty:
        return 50.0
    col_min, col_max = series.min(), series.max()
    span = col_max - col_min
    return float((raw_val - col_min) / span * 100) if span > 0 else 50.0


def _add_marker_dot(
    fig: go.Figure,
    *,
    timestamp: pd.Timestamp,
    y_norm: float,
    color: str,
    label: str,
    hover: str,
) -> None:
    fig.add_trace(
        go.Scatter(
            x=[timestamp],
            y=[y_norm],
            mode="markers",
            marker=dict(
                size=13,
                color=color,
                symbol="circle",
                line=dict(width=2, color="white"),
            ),
            name=label,
            hovertemplate=hover + "<extra></extra>",
            showlegend=True,
        )
    )


def _drift_window_steps(highlight: dict) -> int:
    method = str(highlight.get("method") or "").lower()
    severity = str(highlight.get("severity") or "").lower()
    if "drift" in method or "drift" in severity:
        return _DRIFT_WINDOW_STEPS
    return _LSTM_SEQ_LEN


def _add_anomaly_markers(
    fig: go.Figure,
    df: pd.DataFrame,
    selected_cols: List[str],
    highlights: list,
) -> go.Figure:
    """Overlay anomaly markers: dots for spikes, shaded bands for LSTM/drift windows."""
    df_ts = df.copy()
    df_ts["timestamp"] = pd.to_datetime(df_ts["timestamp"]).dt.tz_localize(None)
    df_sorted = df_ts.sort_values("timestamp").reset_index(drop=True)

    for h in highlights:
        try:
            ts = pd.Timestamp(h.get("timestamp", "")).tz_localize(None)
        except Exception:
            continue

        detector = str(h.get("detector", "lstm"))
        is_agreement = bool(h.get("agreement", False))
        score = float(h.get("score", 0))
        z_score = h.get("z_score")
        frozen_hours = h.get("frozen_hours_at_detection")
        has_lstm = "lstm" in detector

        if is_agreement:
            band_color = "rgba(255,50,50,0.13)"
            line_color = "rgba(255,80,80,0.9)"
            dot_color = "#ff3333"
        elif has_lstm:
            band_color = "rgba(255,140,0,0.10)"
            line_color = "rgba(255,140,0,0.85)"
            dot_color = "#ff8c00"
        else:
            band_color = "rgba(255,215,0,0.13)"
            line_color = "rgba(255,215,0,0.85)"
            dot_color = "#ffd700"

        marker_cols = resolve_highlight_columns(h, selected_cols) or list(selected_cols)

        if has_lstm:
            window_steps = _drift_window_steps(h)
            before = df_sorted[df_sorted["timestamp"] <= ts]
            window_start = (
                before["timestamp"].iloc[-window_steps]
                if len(before) >= window_steps
                else df_sorted["timestamp"].iloc[0]
            )
            fig.add_vrect(
                x0=window_start,
                x1=ts,
                fillcolor=band_color,
                line_width=0,
                layer="below",
            )
            fig.add_vline(
                x=ts,
                line_dash="dot",
                line_color=line_color,
                line_width=1.5,
                annotation=dict(
                    text=f"⚠ {detector} {score:.2f}",
                    font=dict(size=10, color=line_color),
                    textangle=-90,
                    yref="paper",
                    y=0.97,
                    xanchor="right",
                ),
            )

        for column in marker_cols:
            if column not in df.columns:
                continue
            match_idx = (df_sorted["timestamp"] - ts).abs().idxmin()
            match_row = df_sorted.loc[match_idx]
            raw_val = pd.to_numeric(match_row.get(column), errors="coerce")
            if pd.isna(raw_val):
                continue

            y_norm = _normalized_y(df, column, float(raw_val))
            # Stuck-fault hits carry no z_score (see UploadHighAnomalyDetail);
            # render their hours-frozen-at-detection instead of a z-score.
            if detector == "stuck_fault" and frozen_hours is not None:
                hover_z = f" · frozen {frozen_hours:.1f}h"
            elif z_score is not None:
                hover_z = f" · z={z_score:.1f}"
            else:
                hover_z = ""
            _add_marker_dot(
                fig,
                timestamp=match_row["timestamp"],
                y_norm=y_norm,
                color=dot_color,
                label=f"⚠ {column}{hover_z}",
                hover=(
                    f"<b>Anomaly: {column}</b><br>"
                    f"Value: {raw_val:.2f}{hover_z}<br>"
                    f"Detector: {detector}<br>"
                    "Time: %{x}"
                ),
            )

    return fig

st.set_page_config(page_title="Monitoring", layout="wide")
render_global_sidebar()

st.title("Monitoring")

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]
st.markdown(f"**{tenant}** · Greenhouse **{greenhouse}**")

try:
    _prefetch_bounds = peek_monitoring_prefetch_bounds()
    if _prefetch_bounds is not None:
        _data_min_ts, _data_max_ts = _prefetch_bounds
        _min_iso, _max_iso = _data_min_ts.isoformat(), _data_max_ts.isoformat()
    else:
        _min_iso, _max_iso = query_sensor_timestamp_bounds(tenant, greenhouse)
        if not _min_iso or not _max_iso:
            render_no_sensor_data_hint(tenant, greenhouse)
            st.stop()
        _data_min_ts = pd.Timestamp(_min_iso)
        _data_max_ts = pd.Timestamp(_max_iso)
except Exception as exc:
    st.error(
        f"Could not load sensor data — is the API server running? "
        f"Details: {exc}"
    )
    st.stop()

if _prefetch_bounds is None and (not _min_iso or not _max_iso):
    render_no_sensor_data_hint(tenant, greenhouse)
    st.stop()

# Bounds → timeline → window query: pick the range first, then load exactly that
# window from the API. Loading a fixed recent frame and filtering it in memory
# is what limited these pages to the tail of the dataset.
init_monitoring_session_state(tenant, greenhouse, _data_min_ts, _data_max_ts)
apply_pending_jump_window()

timeline_start, timeline_end = render_timeline_controls(_data_min_ts, _data_max_ts)

with st.spinner("Loading sensor data…"):
    try:
        with timed(logger, "dashboard.monitor.page_load_data"):
            df = load_monitoring_window(tenant, greenhouse, timeline_start, timeline_end)
    except Exception as exc:
        st.error(
            f"Could not load sensor data — is the API server running? "
            f"Details: {exc}"
        )
        st.stop()

# Cheap in-memory trim to the exact bounds (the window query is inclusive).
df = apply_datetime_filter(df, timeline_start, timeline_end)
if df.empty:
    st.warning(
        "No data in the selected window. "
        f"Available data: {_data_min_ts} → {_data_max_ts}."
    )
    st.stop()

_all_numeric: List[str] = get_all_sensor_columns(df)
apply_pending_jump_columns(_all_numeric)

st.markdown("---")
st.markdown("### Sensor channels")

_default_checked = set(_all_numeric[:2])

# Seed checkbox session state with defaults only when the key is not already
# present (e.g. set by apply_pending_jump_columns → apply_sensor_checkbox_selection).
# Providing both `value=` and `key=` when the key already exists in session_state
# raises Streamlit's check_session_state_rules warning and the `value=` arg is
# silently ignored anyway, so we use setdefault here and omit `value=` below.
for col in _all_numeric:
    st.session_state.setdefault(f"cb_{col}", col in _default_checked)

_sensor_cols = st.columns(6)
selected_cols: List[str] = []
for i, col in enumerate(_all_numeric):
    if _sensor_cols[i % 6].checkbox(get_sensor_label(col), key=f"cb_{col}"):
        selected_cols.append(col)

if not selected_cols:
    st.info("Select at least one sensor channel above.")
    st.stop()

_metric_cols = st.columns(min(len(selected_cols), 4))
for i, col_name in enumerate(selected_cols[:4]):
    _metric_cols[i].metric(
        f"Avg {get_sensor_label(col_name)}",
        f"{df[col_name].mean():.1f}",
    )

st.markdown("---")
st.subheader("Sensor Overview")
st.caption(
    "All values normalized to 0–100 % of their visible range so sensors with "
    "different units (°C, %, ppm, lux) appear on one chart. "
    "Hover over any line to see the actual reading."
)

_CHART_CONFIG = {"scrollZoom": True, "displayModeBar": True}
_color_manager = get_chart_color_manager("monitoring")
with timed(logger, "dashboard.monitor.plot_combined", rows=len(df), cols=len(selected_cols)):
    _fig = plot_combined_sensors(
        df,
        selected_cols,
        color_manager=_color_manager,
        tab_context="monitoring",
        x_range=(timeline_start, timeline_end),
    )

_highlights = st.session_state.get("monitoring_anomaly_highlights")
if _highlights:
    _hl = _highlights[0]
    st.info(
        f"Anomaly highlight active — **{_hl.get('detector', 'unknown')}** at "
        f"`{str(_hl.get('timestamp', ''))[:19].replace('T', ' ')}`"
        + (f" on **{_hl.get('column')}**" if _hl.get("column") else "")
    )
    _fig = _add_anomaly_markers(_fig, df, selected_cols, _highlights)

st.plotly_chart(_fig, width="stretch", key="combined_chart", config=_CHART_CONFIG)

st.markdown("---")

_table_df = df[["timestamp"] + selected_cols].copy()
_table_df["timestamp"] = pd.to_datetime(_table_df["timestamp"])
_table_df = _table_df.sort_values("timestamp").reset_index(drop=True)

_n_raw = len(_table_df)
_table_df = _table_df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
_n_dupes = _n_raw - len(_table_df)

_table_df["timestamp"] = _table_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

render_data_table(
    _table_df,
    title="Data table",
    table_key="monitoring_sensor_data",
    max_height=320,
    export_file_name=f"sensor_data_{tenant}_{greenhouse}.csv",
)

_caption = f"{len(_table_df):,} rows in selected period"
if _n_dupes:
    _caption += f" · {_n_dupes:,} duplicate timestamp{'s' if _n_dupes > 1 else ''} removed"
st.caption(_caption)
