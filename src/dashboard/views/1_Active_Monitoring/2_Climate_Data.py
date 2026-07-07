"""Climate data dashboard page."""

from typing import Any, List

from src.dashboard.constants import DEFAULT_ANOMALY_THRESHOLD

import pandas as pd
import streamlit as st

from src.dashboard.components.charts import (
    apply_datetime_filter,
    get_sensor_label,
    plot_combined_sensors,
    plot_synced_sensor_grid,
    query_incidents,
    query_sensor_readings_window,
    query_sensor_timestamp_bounds,
)
from src.dashboard.components.color_manager import get_chart_color_manager
from src.dashboard.components.data_availability import render_no_sensor_data_hint
from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.components.sensor_columns import get_climate_columns
from src.dashboard.components.timeline import (
    init_monitoring_session_state,
    render_timeline_controls,
)
from src.dashboard.services.anomaly_events import (
    build_unified_anomaly_events,
    count_events_for_column,
    event_affects_column,
)
from src.dashboard.components.events_table import render_events_table
from src.dashboard.services.diagnosis_enricher import format_sensor_label

_TAB_CONTEXT = "climate"

_SENSOR_UNITS: dict[str, str] = {
    "temperature": "°C",
    "humidity": "%",
    "co2": "ppm",
    "light": "lux",
}

st.set_page_config(page_title="Climate Data", page_icon="🌡️", layout="wide")
render_global_sidebar()

st.title("Climate Data")
st.markdown("Per-channel time-series for greenhouse climate sensors (temperature, humidity, light, vents, screens, and related channels).")

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]
_anomaly_threshold = st.session_state.get("anomaly_threshold", DEFAULT_ANOMALY_THRESHOLD)

if "sdetail_filter_col" not in st.session_state:
    st.session_state["sdetail_filter_col"] = None

try:
    _min_iso, _max_iso = query_sensor_timestamp_bounds(tenant, greenhouse)
except Exception as exc:
    st.error(
        f"Could not load climate data — is the API server running? "
        f"Details: {exc}"
    )
    st.stop()

if not _min_iso or not _max_iso:
    render_no_sensor_data_hint(tenant, greenhouse)
    st.stop()

_d_min = pd.Timestamp(_min_iso)
_d_max = pd.Timestamp(_max_iso)

# Bounds → timeline → window query (see Monitoring): pick the range, then load
# exactly that window so the pickers span the full dataset, not just its tail.
init_monitoring_session_state(tenant, greenhouse, _d_min, _d_max)
timeline_start, timeline_end = render_timeline_controls(_d_min, _d_max)

with st.spinner("Loading climate data…"):
    try:
        df = query_sensor_readings_window(
            tenant, greenhouse, timeline_start.isoformat(), timeline_end.isoformat()
        )
    except Exception as exc:
        st.error(
            f"Could not load climate data — is the API server running? "
            f"Details: {exc}"
        )
        st.stop()

df = apply_datetime_filter(df, timeline_start, timeline_end)
if df.empty:
    st.warning(
        "No data in the selected window. "
        f"Available data: {_d_min} → {_d_max}."
    )
    st.stop()

_available = get_climate_columns(df)
if not _available:
    st.info("No climate sensor columns found in the loaded data.")
    st.stop()

st.markdown("---")
st.markdown("### View settings")

_default_checked = set(_available[:2])

_sensor_cols = st.columns(6)
selected_cols: List[str] = []
for i, col in enumerate(_available):
    if _sensor_cols[i % 6].checkbox(
        get_sensor_label(col),
        value=(col in _default_checked),
        key=f"sd_cb_{col}",
    ):
        selected_cols.append(col)

accumulative = st.toggle(
    "Accumulative view",
    value=st.session_state.get("accumulative_climate", False),
    key="accumulative_climate",
    help="Show all selected sensors on one chart instead of separate panels.",
)

if not selected_cols:
    st.info("Select at least one sensor channel above.")
    st.stop()

_color_manager = get_chart_color_manager(_TAB_CONTEXT)
_col_stats: dict[str, Any] = {}

df_incidents = query_incidents(tenant, greenhouse)
df_anomaly_events = build_unified_anomaly_events(
    df,
    df_incidents,
    start=timeline_start,
    end=timeline_end,
    threshold=_anomaly_threshold,
)

for col in selected_cols:
    series = pd.to_numeric(df[col], errors="coerce")
    last_val = series.dropna().iloc[-1] if not series.dropna().empty else None
    count = count_events_for_column(df_anomaly_events, col)
    _col_stats[col] = {"count": count, "current": last_val}

st.markdown("### Current readings")
st.caption("Click an anomaly counter below a sensor card to filter the table to that sensor only.")

_card_cols = st.columns(min(len(selected_cols), 4))
active_filter: str | None = st.session_state["sdetail_filter_col"]
total_rows = len(df)

for card_col, col in zip(_card_cols, selected_cols[:4]):
    stats = _col_stats[col]
    label = get_sensor_label(col)
    unit = _SENSOR_UNITS.get(col, "")
    current = stats["current"]
    count = stats["count"]
    value_str = f"{current:.2f} {unit}" if current is not None else "—"

    if count == 0:
        status = "🟢 Normal"
    elif count <= max(1, total_rows * 0.05):
        status = "🟡 Warning"
    else:
        status = "🔴 Critical"

    is_active = active_filter == col
    card_col.metric(label=label, value=value_str)

    if count > 0:
        badge = (
            f"✕ {count} anomal{'y' if count == 1 else 'ies'}"
            if is_active
            else f"↑ {count} anomal{'y' if count == 1 else 'ies'}"
        )
        if card_col.button(badge, key=f"sdetail_filter_{col}",
                           type="primary" if is_active else "secondary"):
            st.session_state["sdetail_filter_col"] = None if is_active else col
            st.rerun()
    else:
        card_col.caption("↑ 0 anomalies")

    card_col.caption(status)

_filtered_events = df_anomaly_events
if active_filter and not _filtered_events.empty:
    _filtered_events = _filtered_events[
        _filtered_events.apply(lambda row: event_affects_column(row, active_filter), axis=1)
    ]
if active_filter:
    st.info(f"Filtering to **{get_sensor_label(active_filter)}** anomalies. Click '✕ …' to clear.")

chart_cols = list(selected_cols)
if active_filter:
    chart_cols = [active_filter] if active_filter in _available else chart_cols

_timeline_range = (timeline_start, timeline_end)

st.markdown("### Sensor time-series")
st.caption(
    f"Showing {len(df):,} readings from "
    f"{timeline_start.strftime('%Y-%m-%d %H:%M')} to "
    f"{timeline_end.strftime('%Y-%m-%d %H:%M')}"
    + (f" · chart limited to **{get_sensor_label(active_filter)}**" if active_filter else "")
)
if accumulative:
    fig = plot_combined_sensors(
        df,
        chart_cols,
        color_manager=_color_manager,
        tab_context=_TAB_CONTEXT,
        normalized=True,
        x_range=_timeline_range,
    )
else:
    fig = plot_synced_sensor_grid(
        df,
        chart_cols,
        color_manager=_color_manager,
        tab_context=_TAB_CONTEXT,
        x_range=_timeline_range,
    )
st.plotly_chart(
    fig,
    width="stretch",
    key=f"climate_chart_{active_filter or 'all'}_{len(chart_cols)}_{len(df)}",
    config={"scrollZoom": True, "displayModeBar": True},
)

st.markdown("### Anomaly events")

if _filtered_events.empty:
    if active_filter:
        st.info(f"No anomalies for {get_sensor_label(active_filter)} in this window.")
    else:
        st.success("No climate sensor anomalies detected in the selected window.")
else:
    df_display = _filtered_events.copy()
    df_display["Time"] = df_display["detected_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df_display["Anomaly Score"] = df_display["anomaly_score"].apply(
        lambda x: f"{x:.2f}" if pd.notnull(x) else "—"
    )
    df_display["Fault Type"] = df_display["fault_type"].fillna("Unknown")
    df_display["Reasoning"] = df_display["reasoning"].fillna("—")
    df_display["Source"] = df_display["source"].fillna("—")
    df_display["Sensors"] = df_display["affected_sensors"].apply(
        lambda items: (
            ", ".join(format_sensor_label(str(item)) for item in items)
            if isinstance(items, list) and items
            else (str(items) if items else "—")
        )
    )

    render_events_table(
        df_display[
            ["Time", "Fault Type", "Reasoning", "Anomaly Score", "Source", "Sensors"]
        ],
        wrap_columns=("Reasoning",),
    )

    if not df_display.empty:
        st.markdown("---")
        from src.dashboard.components import diagnosis_card
        diagnosis_card.render(df_display.iloc[0].to_dict())

    anomaly_count = len(df_display)
    st.caption(
        f"{anomaly_count} anomal{'y' if anomaly_count == 1 else 'ies'} in the selected window "
        f"(from persisted scores and incidents)"
    )

with st.expander("Raw data table"):
    _table_df = df[["timestamp"] + chart_cols].copy()
    _table_df["timestamp"] = pd.to_datetime(_table_df["timestamp"])
    _table_df = (
        _table_df.sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"])
        .reset_index(drop=True)
    )
    _table_df["timestamp"] = _table_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    st.dataframe(_table_df, width="stretch", height=320)
    st.download_button(
        "Download CSV",
        data=_table_df.to_csv(index=False).encode("utf-8"),
        file_name=f"climate_sensors_{tenant}_{greenhouse}.csv",
        mime="text/csv",
    )
