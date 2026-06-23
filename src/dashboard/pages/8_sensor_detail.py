"""Climate Sensor Detail dashboard page.

Per-channel subplots with real Y-axis values for temperature, humidity, CO₂
and light. Z-score anomaly detection per channel with clickable badge filters
and a severity-highlighted anomaly table — matching the gas sensor page pattern.
"""

import datetime
from datetime import time as _time
from typing import Any, List

import pandas as pd
import streamlit as st

from src.dashboard.components.charts import (
    ANOMALY_RANGE_OPTIONS,
    apply_datetime_filter,
    get_sensor_label,
    has_raw_readings_in_window,
    plot_synced_sensor_grid,
    query_raw_readings,
    query_sensor_readings,
)

_Z_ANOMALY = 3.0
_Z_CRITICAL = 5.0

_SENSOR_UNITS: dict[str, str] = {
    "temperature": "°C",
    "humidity": "%",
    "co2": "ppm",
    "light": "lux",
}
_SEV_BG = {
    "critical": "background-color: rgba(239, 68, 68, 0.20)",
    "warning": "background-color: rgba(251, 146, 60, 0.20)",
}


def _z_score_anomalies(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    numeric = pd.to_numeric(series, errors="coerce")
    std = numeric.std()
    if pd.isna(std) or std == 0:
        return pd.Series(False, index=series.index), pd.Series(0.0, index=series.index)
    z = (numeric - numeric.mean()).abs() / std
    return z >= _Z_ANOMALY, z.round(2)


st.set_page_config(page_title="Climate Sensors", page_icon="🌡️", layout="wide")
st.title("Climate Sensor Detail")
st.markdown("Per-channel time-series with statistical anomaly detection for temperature, humidity, CO₂ and light.")

if "selected_tenant" not in st.session_state:
    st.warning("Please configure global settings in the sidebar of the main app.")
    st.stop()

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]

# ── Sidebar: time window ──────────────────────────────────────────────────────
st.sidebar.markdown("### Time window")
_current_label = st.session_state.get("sdetail_window", "Last 7 days")
_safe_index = (
    list(ANOMALY_RANGE_OPTIONS.keys()).index(_current_label)
    if _current_label in ANOMALY_RANGE_OPTIONS else 2
)
_window_label = st.sidebar.selectbox(
    "Show data for",
    options=list(ANOMALY_RANGE_OPTIONS.keys()),
    index=_safe_index,
    key="sdetail_window",
)
hours_back = ANOMALY_RANGE_OPTIONS[_window_label]

if st.sidebar.button("Refresh", use_container_width=True, key="sdetail_refresh"):
    query_raw_readings.clear()
    query_sensor_readings.clear()
    st.rerun()

# ── Filter state ──────────────────────────────────────────────────────────────
if "sdetail_filter_col" not in st.session_state:
    st.session_state["sdetail_filter_col"] = None

# ── Load data ─────────────────────────────────────────────────────────────────
_use_raw = has_raw_readings_in_window(tenant, greenhouse, hours_back)
with st.spinner(f"Loading {_window_label.lower()}…"):
    if _use_raw:
        df = query_raw_readings(tenant, greenhouse, hours_back)
    else:
        df = query_sensor_readings(tenant, greenhouse)
        if not df.empty:
            _now = pd.Timestamp.utcnow()
            df = apply_datetime_filter(df, _now - pd.Timedelta(hours=hours_back), _now)

if df.empty:
    st.info(
        f"No data found for the {_window_label.lower()}. "
        "Try a wider time range or upload a CSV with sensor readings."
    )
    st.stop()

# ── Available columns ─────────────────────────────────────────────────────────
_CLIMATE_ORDER = ["temperature", "humidity", "co2", "light"]
_EXCLUDE = {"timestamp", "_synthetic", "source", "anomaly_score"}
_all_numeric = [c for c in df.columns if c not in _EXCLUDE and pd.api.types.is_numeric_dtype(df[c])]
_climate_present = [c for c in _CLIMATE_ORDER if c in _all_numeric]
_available = _climate_present + [c for c in _all_numeric if c not in set(_CLIMATE_ORDER)]

if not _available:
    st.info("No numeric sensor columns found in the loaded data.")
    st.stop()

# ── Sidebar: date range ───────────────────────────────────────────────────────
_ts_col = pd.to_datetime(df["timestamp"])
_d_min = _ts_col.min()
_d_max = _ts_col.max()
_rkey = _window_label.replace(" ", "_").lower()

st.sidebar.markdown("---")
st.sidebar.markdown("### Date range")

_sd_col, _st_col = st.sidebar.columns(2)
_start_date = _sd_col.date_input("From date", value=_d_min.date(), key=f"sd_{_rkey}_from")
_start_time = _st_col.time_input("From time", value=_time(0, 0), key=f"sd_{_rkey}_from_t")
start_dt = datetime.datetime.combine(_start_date, _start_time)

_ed_col, _et_col = st.sidebar.columns(2)
_end_date = _ed_col.date_input("To date", value=_d_max.date(), key=f"sd_{_rkey}_to")
_end_time = _et_col.time_input("To time", value=_time(23, 59), key=f"sd_{_rkey}_to_t")
end_dt = datetime.datetime.combine(_end_date, _end_time)

_from_ts = pd.Timestamp(start_dt)
_to_ts = pd.Timestamp(end_dt)
_ts_work = pd.to_datetime(df["timestamp"])
if _ts_work.dt.tz is not None:
    _from_ts = _from_ts.tz_localize("UTC") if _from_ts.tzinfo is None else _from_ts
    _to_ts = _to_ts.tz_localize("UTC") if _to_ts.tzinfo is None else _to_ts
_filtered = df[(_ts_work >= _from_ts) & (_ts_work <= _to_ts)].copy()
if not _filtered.empty:
    df = _filtered
else:
    st.sidebar.warning(
        f"No data in {_start_date} → {_end_date}. Showing full window."
    )

# ── Sidebar: per-channel checkboxes ──────────────────────────────────────────
st.sidebar.markdown("---")
st.sidebar.markdown("### Sensor channels")
selected_cols: List[str] = [
    col for col in _available
    if st.sidebar.checkbox(
        get_sensor_label(col),
        value=col in _climate_present,
        key=f"sd_cb_{col}",
    )
]

if not selected_cols:
    st.info("Select at least one sensor channel from the sidebar.")
    st.stop()

# ── Per-column z-score anomaly detection ─────────────────────────────────────
_col_stats: dict[str, Any] = {}
_anomaly_pts: list[dict[str, Any]] = []

for col in selected_cols:
    series = pd.to_numeric(df[col], errors="coerce")
    is_anom, z_scores = _z_score_anomalies(series)
    anom_mask = is_anom.fillna(False)
    count = int(anom_mask.sum())
    last_val = series.dropna().iloc[-1] if not series.dropna().empty else None
    _col_stats[col] = {"count": count, "current": last_val}
    for idx in series[anom_mask].index:
        z = float(z_scores[idx])
        _anomaly_pts.append({
            "timestamp": df.loc[idx, "timestamp"],
            "column": col,
            "value": float(series[idx]),
            "z_score": round(z, 2),
            "severity": "critical" if z >= _Z_CRITICAL else "warning",
        })

# ── Metric cards ──────────────────────────────────────────────────────────────
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

# ── Filter info ───────────────────────────────────────────────────────────────
_filtered_pts = (
    [p for p in _anomaly_pts if p["column"] == active_filter]
    if active_filter else _anomaly_pts
)
if active_filter:
    st.info(f"Filtering to **{get_sensor_label(active_filter)}** anomalies. Click '✕ …' to clear.")

# ── Per-channel synced subplots ───────────────────────────────────────────────
st.markdown("### Sensor time-series")
fig = plot_synced_sensor_grid(df, selected_cols)
st.plotly_chart(fig, use_container_width=True, key="sdetail_grid",
                config={"scrollZoom": True, "displayModeBar": True})

# ── Anomaly events table ──────────────────────────────────────────────────────
st.markdown("### Anomaly events")

if not _filtered_pts:
    if active_filter:
        st.info(f"No anomalies for {get_sensor_label(active_filter)} in this window.")
    else:
        st.success("No climate sensor anomalies detected in the selected window.")
else:
    df_anom = pd.DataFrame(_filtered_pts)
    df_anom["timestamp"] = pd.to_datetime(df_anom["timestamp"])
    df_anom = df_anom.sort_values("timestamp", ascending=False).reset_index(drop=True)
    df_anom["sensor"] = df_anom["column"].map(get_sensor_label)
    df_anom["unit"] = df_anom["column"].map(lambda c: _SENSOR_UNITS.get(c, ""))
    df_anom["value_str"] = df_anom.apply(lambda r: f"{r['value']:.2f} {r['unit']}", axis=1)

    df_display = df_anom[["timestamp", "sensor", "value_str", "z_score", "severity"]].rename(
        columns={"timestamp": "Time", "sensor": "Sensor", "value_str": "Value",
                 "z_score": "Z-score", "severity": "Severity"}
    )

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
    st.caption(
        f"{len(df_anom)} anomalous reading{'s' if len(df_anom) != 1 else ''} in the {_window_label.lower()}"
    )

# ── Raw data table ────────────────────────────────────────────────────────────
with st.expander("Raw data table"):
    _table_df = df[["timestamp"] + selected_cols].copy()
    _table_df["timestamp"] = pd.to_datetime(_table_df["timestamp"])
    _table_df = (
        _table_df.sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"])
        .reset_index(drop=True)
    )
    _table_df["timestamp"] = _table_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    st.dataframe(_table_df, use_container_width=True, height=320)
    st.download_button(
        "Download CSV",
        data=_table_df.to_csv(index=False).encode("utf-8"),
        file_name=f"climate_sensors_{tenant}_{greenhouse}.csv",
        mime="text/csv",
    )
