"""Gas monitoring dashboard page."""

import os
from typing import Any

from src.dashboard.services.api_client import DEFAULT_API_URL

import pandas as pd
import streamlit as st

from src.dashboard.components.charts import (
    plot_gas_combined,
    plot_gas_sensor_stack,
    query_incidents,
)
from src.dashboard.components.color_manager import get_chart_color_manager
from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.components.timeline import (
    init_monitoring_session_state,
    render_timeline_controls,
)
from src.dashboard.services.http_utils import api_get

API_URL = os.environ.get("API_URL", DEFAULT_API_URL)
_TAB_CONTEXT = "gas"

_GAS_LABELS: dict[str, str] = {
    "c2h4": "C₂H₄ (ethylene)",
    "gas_co2": "CO₂ (gas sensor)",
    "no": "NO",
    "no2": "NO₂",
    "nox": "NOx",
    "co": "CO",
}
_GAS_UNITS: dict[str, str] = {
    "c2h4": "ppm",
    "gas_co2": "ppm",
    "no": "ppb",
    "no2": "ppb",
    "nox": "ppb",
    "co": "ppm",
}


def _filter_by_timeline(
    df_wide: pd.DataFrame,
    anomaly_points: list[dict[str, Any]],
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    ts_work = pd.to_datetime(df_wide["timestamp"])
    from_ts = start_ts
    to_ts = end_ts
    if ts_work.dt.tz is not None:
        from_ts = from_ts.tz_localize("UTC") if from_ts.tzinfo is None else from_ts
        to_ts = to_ts.tz_localize("UTC") if to_ts.tzinfo is None else to_ts
    filtered = df_wide[(ts_work >= from_ts) & (ts_work <= to_ts)].copy()
    if filtered.empty:
        return df_wide, anomaly_points
    filtered_points = [
        p for p in anomaly_points
        if from_ts <= pd.Timestamp(p["timestamp"]) <= to_ts
    ]
    return filtered.reset_index(drop=True), filtered_points


st.set_page_config(page_title="Gas Monitoring", page_icon="🧪", layout="wide")
render_global_sidebar()

st.title("Gas Monitoring")
st.markdown("Statistical anomaly detection for C₂H₄, CO₂, NO, NO₂, NOx and CO.")

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]

# Load one full year of data so any historical date is reachable via the timeline selector
hours = 365 * 24

if "gas_filter_col" not in st.session_state:
    st.session_state["gas_filter_col"] = None

with st.spinner("Loading gas sensor data…"):
    try:
        payload = api_get(
            f"{API_URL}/api/gas/summary",
            params={"tenant_id": tenant, "greenhouse_id": greenhouse, "hours": hours},
        ).json()
    except Exception as exc:
        st.error(f"Could not reach the API: {exc}")
        st.stop()

gas_columns: list[dict[str, Any]] = payload.get("gas_columns", [])

if not gas_columns:
    st.info(
        "No gas sensor data found for this tenant / greenhouse in the selected window. "
        "Upload a CSV that contains columns like `c2h4`, `gas_co2`, `no`, `no2`, `nox`, or `co`."
    )
    st.stop()

_rows: dict[str, dict[str, Any]] = {}
_anomaly_points: list[dict[str, Any]] = []

for col_data in gas_columns:
    col = col_data["column"]
    for reading in col_data["readings"]:
        ts = reading["timestamp"]
        if ts not in _rows:
            _rows[ts] = {"timestamp": ts}
        val = reading.get("value")
        if val is not None:
            _rows[ts][col] = val
        if reading.get("is_anomaly") and val is not None:
            pass # We no longer populate dummy points here, we'll fetch real incidents

df_incidents = query_incidents(tenant, greenhouse)
# Will filter incidents based on timeline later after `_d_min` / `_d_max` are determined

df_wide = pd.DataFrame(list(_rows.values())).sort_values("timestamp").reset_index(drop=True)
df_wide["timestamp"] = pd.to_datetime(df_wide["timestamp"])
gas_cols_present = [c["column"] for c in gas_columns]

_d_min = df_wide["timestamp"].min()
_d_max = df_wide["timestamp"].max()
init_monitoring_session_state(tenant, greenhouse, _d_min, _d_max)

st.markdown("---")
st.markdown("### View settings")

# TODO: save this inside the configurations
_default_checked = set(gas_cols_present[:2])

_sensor_cols = st.columns(6)
selected_gas_cols: list[str] = []
for i, col in enumerate(gas_cols_present):
    if _sensor_cols[i % 6].checkbox(
        _GAS_LABELS.get(col, col.upper()),
        value=(col in _default_checked),
        key=f"gas_cb_{col}",
    ):
        selected_gas_cols.append(col)

accumulative = st.toggle(
    "Accumulative view",
    value=st.session_state.get("accumulative_gas", False),
    key="accumulative_gas",
    help="Show all selected gas sensors on one chart instead of separate panels.",
)

if not selected_gas_cols:
    st.info("Select at least one gas sensor channel from the sidebar.")
    st.stop()

st.markdown("### Current readings")
st.caption("Click an anomaly counter below a sensor card to filter the table to that sensor only.")
_card_cols = st.columns(min(len(gas_columns), 6))
active_filter: str | None = st.session_state["gas_filter_col"]

for card_col, col_data in zip(_card_cols, gas_columns):
    col = col_data["column"]
    label = _GAS_LABELS.get(col, col.upper())
    unit = _GAS_UNITS.get(col, "")
    current = col_data.get("current_value")
    anomaly_count = col_data.get("anomaly_count", 0)
    total = len(col_data.get("readings", []))

    value_str = f"{current:.3f} {unit}" if current is not None else "—"

    if anomaly_count == 0:
        status = "🟢 Normal"
    elif anomaly_count <= max(1, total * 0.05):
        status = "🟡 Warning"
    else:
        status = "🔴 Critical"

    is_active = active_filter == col
    card_col.metric(label=label, value=value_str)

    if anomaly_count > 0:
        badge = (
            f"✕ {anomaly_count} anomal{'y' if anomaly_count == 1 else 'ies'}"
            if is_active
            else f"↑ {anomaly_count} anomal{'y' if anomaly_count == 1 else 'ies'}"
        )
        if card_col.button(badge, key=f"gas_filter_{col}",
                           type="primary" if is_active else "secondary"):
            st.session_state["gas_filter_col"] = None if is_active else col
            st.rerun()
    else:
        card_col.caption("↑ 0 anomalies")

    card_col.caption(status)

timeline_start, timeline_end = render_timeline_controls(_d_min, _d_max)

# Filter real incidents
_filtered_incidents = df_incidents
if not _filtered_incidents.empty:
    _filtered_incidents["detected_at"] = pd.to_datetime(_filtered_incidents["detected_at"])
    _filtered_incidents = _filtered_incidents[
        (_filtered_incidents["detected_at"] >= timeline_start) & 
        (_filtered_incidents["detected_at"] <= timeline_end)
    ].copy()

df_chart, _ = _filter_by_timeline(
    df_wide, [], timeline_start, timeline_end
)

if active_filter and not _filtered_incidents.empty:
    _filtered_incidents = _filtered_incidents[
        _filtered_incidents["root_cause_details"].apply(lambda x: active_filter in str(x) if x else False)
    ]
if active_filter:
    active_label = _GAS_LABELS.get(active_filter, active_filter.upper())
    st.info(f"Filtering to **{active_label}** anomalies. Click '✕ Clear filter' to show all.")

st.markdown("### Gas sensor time-series")
_color_manager = get_chart_color_manager(_TAB_CONTEXT)
_chart_cols = [col for col in selected_gas_cols if col in df_chart.columns]

if accumulative:
    fig = plot_gas_combined(
        df_chart,
        _chart_cols,
        _GAS_LABELS,
        color_manager=_color_manager,
        tab_context=_TAB_CONTEXT,
    )
else:
    fig = plot_gas_sensor_stack(
        df_chart,
        _chart_cols,
        _GAS_LABELS,
        _GAS_UNITS,
        _anomaly_points,
        color_manager=_color_manager,
        tab_context=_TAB_CONTEXT,
    )

if fig.data:
    st.plotly_chart(
        fig,
        use_container_width=True,
        key="gas_chart",
        config={"scrollZoom": True, "displayModeBar": True},
    )
else:
    st.info("No plottable gas sensor values in the selected date window.")

st.markdown("### Anomaly events")

if _filtered_incidents.empty:
    if active_filter:
        st.info(f"No anomalies for {_GAS_LABELS.get(active_filter, active_filter.upper())} in this window.")
    else:
        st.success("No gas sensor anomalies detected in the selected window.")
else:
    df_display = _filtered_incidents.copy()
    
    # Format columns
    df_display["Time"] = df_display["detected_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df_display["Anomaly Score"] = df_display["anomaly_score"].apply(lambda x: f"{x:.2f}" if pd.notnull(x) else "—")
    df_display["Fault Type"] = df_display["fault_type"].fillna("Unknown")
    df_display["Root Cause"] = df_display["root_cause"].fillna("Investigating...")
    
    # Extract cluster severity and ID if available
    def get_cluster_info(details):
        if not isinstance(details, dict):
            return "N/A"
        return details.get("cluster_severity", "N/A")
    
    df_display["Severity"] = df_display["root_cause_details"].apply(get_cluster_info)
    
    st.dataframe(
        df_display[["Time", "Fault Type", "Root Cause", "Anomaly Score", "Severity"]],
        use_container_width=True,
        hide_index=True,
    )
    
    # Add a diagnosis view for the most recent incident
    if not df_display.empty:
        st.markdown("---")
        latest_incident = df_display.iloc[0].to_dict()
        from src.dashboard.components import diagnosis_card
        diagnosis_card.render(latest_incident)

