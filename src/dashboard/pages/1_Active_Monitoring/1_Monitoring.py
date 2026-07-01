from typing import List

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.dashboard.components.sensor_columns import get_all_sensor_columns
from src.dashboard.components.charts import (
    apply_datetime_filter,
    get_sensor_label,
    plot_combined_sensors,
)
from src.dashboard.components.color_manager import get_chart_color_manager
from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.components.monitoring_jump import (
    apply_pending_monitoring_jump,
    load_monitoring_readings,
    resolve_highlight_columns,
)
from src.dashboard.components.timeline import (
    init_monitoring_session_state,
    render_timeline_controls,
)

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
            hover_z = f" · z={z_score:.1f}" if z_score is not None else ""
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

with st.spinner("Loading sensor data…"):
    try:
        df, _data_min_ts, _data_max_ts = load_monitoring_readings(tenant, greenhouse)
    except Exception as exc:
        st.error(
            f"Could not load sensor data — is the API server running? "
            f"Details: {exc}"
        )
        st.stop()

if df.empty:
    st.info("No data found. Upload a CSV with sensor readings via the Upload page.")
    st.stop()

_all_numeric: List[str] = get_all_sensor_columns(df)

init_monitoring_session_state(tenant, greenhouse, _data_min_ts, _data_max_ts)
apply_pending_monitoring_jump(_all_numeric)

timeline_start, timeline_end = render_timeline_controls(_data_min_ts, _data_max_ts)

df = apply_datetime_filter(df, timeline_start, timeline_end)
if df.empty:
    st.warning(
        "No data in the selected window. "
        f"Available data: {_data_min_ts} → {_data_max_ts}."
    )
    st.stop()

st.markdown("---")
st.markdown("### Sensor channels")

_default_checked = set(_all_numeric[:2])

_sensor_cols = st.columns(6)
selected_cols: List[str] = []
for i, col in enumerate(_all_numeric):
    if _sensor_cols[i % 6].checkbox(get_sensor_label(col), value=(col in _default_checked), key=f"cb_{col}"):
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

st.plotly_chart(_fig, use_container_width=True, key="combined_chart", config=_CHART_CONFIG)

st.markdown("---")
st.subheader("Data table")

_table_df = df[["timestamp"] + selected_cols].copy()
_table_df["timestamp"] = pd.to_datetime(_table_df["timestamp"])
_table_df = _table_df.sort_values("timestamp").reset_index(drop=True)

_n_raw = len(_table_df)
_table_df = _table_df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
_n_dupes = _n_raw - len(_table_df)

_table_df["timestamp"] = _table_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

st.dataframe(_table_df, use_container_width=True, height=320)

_dl_col, _cap_col = st.columns([1, 4])
_dl_col.download_button(
    "Download CSV",
    data=_table_df.to_csv(index=False).encode("utf-8"),
    file_name=f"sensor_data_{tenant}_{greenhouse}.csv",
    mime="text/csv",
)
_caption = f"{len(_table_df):,} rows in selected period"
if _n_dupes:
    _caption += f" · {_n_dupes:,} duplicate timestamp{'s' if _n_dupes > 1 else ''} removed"
_cap_col.caption(_caption)
