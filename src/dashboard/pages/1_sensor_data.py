import datetime
from datetime import time as _time
from typing import List

import pandas as pd
import streamlit as st

from src.dashboard.components.charts import (
    ANOMALY_RANGE_OPTIONS,
    apply_datetime_filter,
    get_sensor_label,
    has_raw_readings_in_window,
    plot_combined_sensors,
    query_raw_readings,
    query_sensor_readings,
)

st.set_page_config(page_title="Sensor Data", layout="wide")
st.title("Sensor Data Overview")

if "selected_tenant" not in st.session_state:
    st.warning("Please configure global settings in the sidebar of the main app.")
    st.stop()

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]

st.markdown(f"### Monitoring: **{tenant}** - Greenhouse **{greenhouse}**")

# ── Sidebar: time range selector ─────────────────────────────────────────────
st.sidebar.markdown("### Time range")
_range_sel = st.sidebar.selectbox(
    "Show data for",
    options=list(ANOMALY_RANGE_OPTIONS.keys()),
    index=list(ANOMALY_RANGE_OPTIONS.keys()).index("Last 7 days"),
    key="sdata_range",
)
_hours_back = ANOMALY_RANGE_OPTIONS[_range_sel]

# ── Load data scoped to the selected time window ──────────────────────────────
# Check raw readings within the SAME window so the decision is consistent
# with what query_raw_readings will actually return.
_use_raw = has_raw_readings_in_window(tenant, greenhouse, _hours_back)

if _use_raw:
    with st.spinner(f"Loading {_range_sel.lower()}…"):
        df = query_raw_readings(tenant, greenhouse, _hours_back)
else:
    with st.spinner("Fetching sensor data…"):
        df = query_sensor_readings(tenant, greenhouse)
    if not df.empty:
        # Apply the same time window so the data range matches the sidebar selection.
        _now = pd.Timestamp.utcnow()
        _win_start = _now - pd.Timedelta(hours=_hours_back)
        df = apply_datetime_filter(df, _win_start, _now)

if df.empty:
    st.info(
        f"No data found for the {_range_sel.lower()}. "
        "Try a wider time range in the sidebar."
    )
    st.stop()

# ── Detect plottable numeric columns ─────────────────────────────────────────
_EXCLUDE_COLS = {"timestamp", "_synthetic", "source"}
_CANONICAL_ORDER = ["temperature", "humidity", "co2", "light", "anomaly_score"]

_numeric_set: set = {
    c for c in df.columns
    if c not in _EXCLUDE_COLS and pd.api.types.is_numeric_dtype(df[c])
}
_canonical_in_data = [c for c in _CANONICAL_ORDER if c in _numeric_set]
_extra_cols = [c for c in df.columns if c in _numeric_set and c not in set(_CANONICAL_ORDER)]
_all_numeric: List[str] = _canonical_in_data + _extra_cols

# ── Sidebar: date+time range refinement ──────────────────────────────────────
# Default to actual data bounds within the loaded window (not full DB range).
_ts_col = pd.to_datetime(df["timestamp"])
_data_min_ts = _ts_col.min()
_data_max_ts = _ts_col.max()

# Range-scoped keys so switching "Show data for" resets the pickers.
_range_key = _range_sel.replace(" ", "_").lower()

st.sidebar.markdown("---")
st.sidebar.markdown("### Date range")

_start_col, _stime_col = st.sidebar.columns(2)
_start_date = _start_col.date_input(
    "From date", value=_data_min_ts.date(), key=f"sdata_{_range_key}_from"
)
_start_time = _stime_col.time_input(
    "From time", value=_time(0, 0), key=f"sdata_{_range_key}_from_t"
)
start_dt = datetime.datetime.combine(_start_date, _start_time)
st.session_state["start_date"] = start_dt

_end_col, _etime_col = st.sidebar.columns(2)
_end_date = _end_col.date_input(
    "To date", value=_data_max_ts.date(), key=f"sdata_{_range_key}_to"
)
_end_time = _etime_col.time_input(
    "To time", value=_time(23, 59), key=f"sdata_{_range_key}_to_t"
)
end_dt = datetime.datetime.combine(_end_date, _end_time)
st.session_state["end_date"] = end_dt

# Apply datetime filter; fall back to full window if selection yields no rows.
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
    st.warning(
        f"No data in the selected range ({_start_date} {_start_time} → {_end_date} {_end_time}). "
        f"Available data in this window: {_data_min_ts.date()} → {_data_max_ts.date()}. "
        "Showing full loaded window. Expand 'Show data for' in the sidebar if needed."
    )

# ── Sidebar: per-sensor checkboxes ───────────────────────────────────────────
st.sidebar.markdown("---")
st.sidebar.markdown("### Sensor channels")
if not _use_raw and _extra_cols == []:
    st.sidebar.caption(
        "Only canonical sensors available. Re-upload your CSV to see all sensor columns."
    )
selected_cols: List[str] = [
    col for col in _all_numeric
    if st.sidebar.checkbox(get_sensor_label(col), value=True, key=f"cb_{col}")
]

if not selected_cols:
    st.info("Select at least one sensor channel from the left panel.")
    st.stop()

# ── Summary metrics (first 4 selected channels) ───────────────────────────────
_metric_cols = st.columns(min(len(selected_cols), 4))
for i, col_name in enumerate(selected_cols[:4]):
    _metric_cols[i].metric(
        f"Avg {get_sensor_label(col_name)}",
        f"{df[col_name].mean():.1f}",
    )

st.markdown("---")

# ── Combined sensor chart ─────────────────────────────────────────────────────
st.subheader("Sensor Overview")
st.caption(
    "All values normalized to 0–100 % of their visible range so sensors with "
    "different units (°C, %, ppm, lux) appear on one chart. "
    "Hover over any line to see the actual reading."
)

_CHART_CONFIG = {"scrollZoom": True, "displayModeBar": True}
_fig = plot_combined_sensors(df, selected_cols)
st.plotly_chart(_fig, use_container_width=True, key="combined_chart", config=_CHART_CONFIG)

# ── Data table ────────────────────────────────────────────────────────────────
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
