"""Gas Sensor Anomaly Detection dashboard page.

Reads C2H4, CO2, NO, NO2, NOx, CO from the /api/gas/summary endpoint and
displays per-sensor time-series subplots (real values, not %), metric cards
with clickable anomaly filters, and a severity-highlighted anomaly table.
"""

import datetime
import os
from datetime import time as _time
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st

from src.dashboard.components.charts import apply_datetime_filter, get_sensor_color, get_sensor_label

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Gas Sensors", page_icon="🧪", layout="wide")
st.title("Gas Sensor Monitoring")
st.markdown("Statistical anomaly detection for C₂H₄, CO₂, NO, NO₂, NOx and CO.")

if "selected_tenant" not in st.session_state:
    st.warning("Please configure global settings in the sidebar of the main app.")
    st.stop()

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]

# ── Sidebar controls ──────────────────────────────────────────────────────────
st.sidebar.markdown("### Time window")
_WINDOW_OPTIONS = {"Last 6 hours": 6, "Last 24 hours": 24, "Last 48 hours": 48, "Last 7 days": 168}
# Read persisted value so switching pages doesn't reset the selector.
_current_label = st.session_state.get("gas_window", "Last 24 hours")
_safe_index = list(_WINDOW_OPTIONS.keys()).index(_current_label) if _current_label in _WINDOW_OPTIONS else 1
_window_label = st.sidebar.selectbox(
    "Show data for",
    options=list(_WINDOW_OPTIONS.keys()),
    index=_safe_index,
    key="gas_window",
)
hours = _WINDOW_OPTIONS[_window_label]

if st.sidebar.button("Refresh", use_container_width=True, key="gas_refresh"):
    st.rerun()

# ── Filter state ──────────────────────────────────────────────────────────────
if "gas_filter_col" not in st.session_state:
    st.session_state["gas_filter_col"] = None

# ── Fetch data ────────────────────────────────────────────────────────────────
with st.spinner("Loading gas sensor data…"):
    try:
        resp = requests.get(
            f"{API_URL}/api/gas/summary",
            params={"tenant_id": tenant, "greenhouse_id": greenhouse, "hours": hours},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.exceptions.RequestException as exc:
        st.error(f"Could not reach the API: {exc}")
        st.stop()

gas_columns: list[dict[str, Any]] = payload.get("gas_columns", [])

if not gas_columns:
    st.info(
        "No gas sensor data found for this tenant / greenhouse in the selected window. "
        "Upload a CSV that contains columns like `c2h4`, `gas_co2`, `no`, `no2`, `nox`, or `co`."
    )
    st.stop()

# ── Labels and units ──────────────────────────────────────────────────────────
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

# ── Build wide DataFrame and anomaly points ───────────────────────────────────
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
            _anomaly_points.append({
                "timestamp": ts,
                "column": col,
                "value": val,
                "z_score": reading.get("z_score", 0.0),
                "severity": reading.get("severity", "normal"),
                "method": reading.get("method", "none"),
            })

df_wide = pd.DataFrame(list(_rows.values())).sort_values("timestamp").reset_index(drop=True)
df_wide["timestamp"] = pd.to_datetime(df_wide["timestamp"])
gas_cols_present = [c["column"] for c in gas_columns]

# ── Sidebar: date range (added after data loads so defaults use data bounds) ──
_d_min = df_wide["timestamp"].min()
_d_max = df_wide["timestamp"].max()
_rkey = _window_label.replace(" ", "_").lower()

st.sidebar.markdown("---")
st.sidebar.markdown("### Date range")

_sd_col, _st_col = st.sidebar.columns(2)
_start_date = _sd_col.date_input("From date", value=_d_min.date(), key=f"gas_{_rkey}_from")
_start_time = _st_col.time_input("From time", value=_time(0, 0), key=f"gas_{_rkey}_from_t")
_start_dt = datetime.datetime.combine(_start_date, _start_time)

_ed_col, _et_col = st.sidebar.columns(2)
_end_date = _ed_col.date_input("To date", value=_d_max.date(), key=f"gas_{_rkey}_to")
_end_time = _et_col.time_input("To time", value=_time(23, 59), key=f"gas_{_rkey}_to_t")
_end_dt = datetime.datetime.combine(_end_date, _end_time)

_from_ts = pd.Timestamp(_start_dt)
_to_ts = pd.Timestamp(_end_dt)
if df_wide["timestamp"].dt.tz is not None:
    _from_ts = _from_ts.tz_localize("UTC") if _from_ts.tzinfo is None else _from_ts
    _to_ts = _to_ts.tz_localize("UTC") if _to_ts.tzinfo is None else _to_ts
_dw_filtered = df_wide[(df_wide["timestamp"] >= _from_ts) & (df_wide["timestamp"] <= _to_ts)]
if not _dw_filtered.empty:
    df_wide = _dw_filtered.reset_index(drop=True)
    _anomaly_points = [
        p for p in _anomaly_points
        if _from_ts <= pd.Timestamp(p["timestamp"]) <= _to_ts
    ]
else:
    st.sidebar.warning(
        f"No data in {_start_date} → {_end_date}. Showing full window."
    )

# ── Metric cards with clickable anomaly filter ────────────────────────────────
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

# Filter anomaly points for table display
_filtered_points = (
    [ap for ap in _anomaly_points if ap["column"] == active_filter]
    if active_filter else _anomaly_points
)
if active_filter:
    active_label = _GAS_LABELS.get(active_filter, active_filter.upper())
    st.info(f"Filtering to **{active_label}** anomalies. Click '✕ Clear filter' to show all.")

# ── Per-sensor time-series subplots (real Y-axis values) ─────────────────────
st.markdown("### Gas sensor time-series")

_SEVERITY_COLORS = {"critical": "#ef4444", "warning": "#f59e0b", "normal": "#94a3b8"}

_valid_cols = [
    col for col in gas_cols_present
    if col in df_wide.columns and not pd.to_numeric(df_wide[col], errors="coerce").isna().all()
]

if _valid_cols:
    n_rows = len(_valid_cols)
    subplot_titles = [
        f"{_GAS_LABELS.get(col, col.upper())} ({_GAS_UNITS.get(col, '')})"
        for col in _valid_cols
    ]

    fig = make_subplots(
        rows=n_rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06 if n_rows > 1 else 0,
        subplot_titles=subplot_titles,
    )

    for row_idx, col in enumerate(_valid_cols, start=1):
        series = pd.to_numeric(df_wide[col], errors="coerce")
        unit = _GAS_UNITS.get(col, "")
        label = _GAS_LABELS.get(col, col.upper())

        fig.add_trace(
            go.Scatter(
                x=df_wide["timestamp"],
                y=series,
                mode="lines",
                name=label,
                line=dict(width=1.5),
                hovertemplate=f"<b>{label}</b><br>Value: %{{y:.4f}} {unit}<br>Time: %{{x}}<extra></extra>",
            ),
            row=row_idx, col=1,
        )

        # Anomaly markers for this subplot
        for ap in (p for p in _anomaly_points if p["column"] == col):
            try:
                raw_val = float(ap["value"])
            except (TypeError, ValueError):
                continue
            fig.add_trace(
                go.Scatter(
                    x=[ap["timestamp"]],
                    y=[raw_val],
                    mode="markers",
                    marker=dict(
                        color=_SEVERITY_COLORS.get(ap["severity"], "#ef4444"),
                        size=9,
                        symbol="circle",
                        line=dict(color="white", width=1),
                    ),
                    showlegend=False,
                    hovertemplate=(
                        f"<b>⚠ {label}</b><br>"
                        f"Value: {raw_val:.4f} {unit}<br>"
                        f"Z-score: {ap['z_score']:.2f}<br>"
                        f"Severity: {ap['severity']}<br>"
                        "Time: %{x}<extra></extra>"
                    ),
                ),
                row=row_idx, col=1,
            )

        fig.update_yaxes(title_text=unit, row=row_idx, col=1)

    fig.update_layout(
        height=max(220, 180 * n_rows),
        margin=dict(t=40, b=40),
        dragmode="pan",
        showlegend=False,
        xaxis=dict(type="date"),
    )
    st.plotly_chart(fig, use_container_width=True)

# ── Anomaly events table with severity highlighting ───────────────────────────
st.markdown("### Anomaly events")

if not _filtered_points:
    if active_filter:
        st.info(f"No anomalies for {_GAS_LABELS.get(active_filter, active_filter.upper())} in this window.")
    else:
        st.success("No gas sensor anomalies detected in the selected window.")
else:
    df_anom = pd.DataFrame(_filtered_points)
    df_anom["timestamp"] = pd.to_datetime(df_anom["timestamp"])
    df_anom = df_anom.sort_values("timestamp", ascending=False).reset_index(drop=True)

    df_anom["sensor"] = df_anom["column"].map(lambda c: _GAS_LABELS.get(c, c.upper()))
    df_anom["unit"] = df_anom["column"].map(lambda c: _GAS_UNITS.get(c, ""))
    df_anom["value_str"] = df_anom.apply(lambda r: f"{r['value']:.4f} {r['unit']}", axis=1)
    df_anom["z_score"] = df_anom["z_score"].round(2)

    df_display = df_anom[["timestamp", "sensor", "value_str", "z_score", "method", "severity"]].rename(
        columns={
            "timestamp": "Time",
            "sensor": "Sensor",
            "value_str": "Value",
            "z_score": "Z-score",
            "method": "Detection method",
            "severity": "Severity",
        }
    )

    _SEV_BG = {
        "critical": "background-color: rgba(239, 68, 68, 0.20)",
        "warning": "background-color: rgba(251, 146, 60, 0.20)",
    }

    def _highlight_severity(row: pd.Series) -> list[str]:
        bg = _SEV_BG.get(str(row.get("Severity", "")).lower(), "")
        return [bg] * len(row)

    st.dataframe(
        df_display.style.apply(_highlight_severity, axis=1),
        use_container_width=True,
        hide_index=True,
    )
    st.markdown(
        '<div style="display:flex;gap:16px;font-size:12px;margin-top:4px;">'
        '<span><span style="display:inline-block;width:12px;height:12px;vertical-align:middle;'
        'margin-right:4px;border-radius:2px;background:rgba(239,68,68,0.5);"></span>Critical</span>'
        '<span><span style="display:inline-block;width:12px;height:12px;vertical-align:middle;'
        'margin-right:4px;border-radius:2px;background:rgba(251,146,60,0.5);"></span>Warning</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.caption(f"{len(df_anom)} anomalous reading{'s' if len(df_anom) != 1 else ''} in the {_window_label.lower()}")

# ── Baseline statistics ────────────────────────────────────────────────────────
with st.expander("Baseline statistics"):
    st.dataframe(
        pd.DataFrame([
            {
                "Sensor": _GAS_LABELS.get(c["column"], c["column"].upper()),
                "Mean": round(c["mean"], 4),
                "Std dev": round(c["std"], 4),
                "Anomalies": c["anomaly_count"],
                "Readings": len(c.get("readings", [])),
            }
            for c in gas_columns
        ]),
        use_container_width=True,
        hide_index=True,
    )
