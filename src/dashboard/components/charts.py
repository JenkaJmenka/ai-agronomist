import math

import plotly.express as px
from typing import List, Mapping, Optional, Sequence

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from src.dashboard.components.color_manager import ColorAssignmentManager, color_at_index
from src.dashboard.constants import (
    CACHE_TTL_MEDIUM,
    CACHE_TTL_SHORT,
    DEFAULT_ANOMALY_THRESHOLD,
)
from src.api.services.readings_service import merge_anomaly_scores_into_readings
from src.dashboard.services.api_client import DashboardApiClient
from src.dashboard.services.tz_normalize import (
    align_bound_to_series,
    normalize_series_tz,
)

# All dashboard reads go through the API (which holds warm DB connections)
# instead of opening a fresh Supabase connection per query.
_client = DashboardApiClient()

__all__ = ["merge_anomaly_scores_into_readings"]  # re-exported for consumers/tests


def _rows_to_frame(rows: list) -> pd.DataFrame:
    """Rows from the readings API → DataFrame with parsed timestamps."""
    df = pd.DataFrame(rows)
    if not df.empty and "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


@st.cache_data(ttl=CACHE_TTL_SHORT)
def query_sensor_readings(tenant_id: str, greenhouse_id: str) -> pd.DataFrame:
    """Load the recent bounded window of sensor time-series (C2: no full-history load)."""
    payload = _client.get_readings_recent(tenant_id, greenhouse_id)
    return _rows_to_frame(payload["rows"])


@st.cache_data(ttl=CACHE_TTL_MEDIUM)
def query_sensor_readings_window(
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> pd.DataFrame:
    """Load sensor readings for an inclusive datetime window (Monitoring jump path)."""
    rows = _client.get_readings_window(tenant_id, greenhouse_id, start_iso, end_iso)
    return _rows_to_frame(rows)


@st.cache_data(ttl=CACHE_TTL_MEDIUM)
def query_sensor_timestamp_bounds(
    tenant_id: str,
    greenhouse_id: str,
) -> tuple[str | None, str | None]:
    """Return ISO min/max timestamps without loading full reading rows."""
    bounds = _client.get_readings_bounds(tenant_id, greenhouse_id)
    return bounds.get("min_timestamp"), bounds.get("max_timestamp")


@st.cache_data(ttl=CACHE_TTL_SHORT)
def query_incidents(tenant_id: str, greenhouse_id: str) -> pd.DataFrame:
    rows = _client.get_incidents(tenant_id, greenhouse_id)
    df = pd.DataFrame(rows)
    if not df.empty and "detected_at" in df.columns:
        df["detected_at"] = pd.to_datetime(df["detected_at"])
    return df


@st.cache_data(ttl=CACHE_TTL_SHORT)
def query_incidents_window(
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> pd.DataFrame:
    """Incidents within [start, end] — avoids loading full incident history."""
    rows = _client.get_incidents(
        tenant_id, greenhouse_id, start_iso=start_iso, end_iso=end_iso
    )
    df = pd.DataFrame(rows)
    if not df.empty and "detected_at" in df.columns:
        df["detected_at"] = pd.to_datetime(df["detected_at"])
    return df


@st.cache_data(ttl=CACHE_TTL_SHORT)
def get_data_date_bounds(tenant_id: str, greenhouse_id: str):
    """Return (min_date, max_date) for available readings, or (None, None)."""
    min_iso, max_iso = query_sensor_timestamp_bounds(tenant_id, greenhouse_id)
    if not min_iso or not max_iso:
        return None, None
    return pd.Timestamp(min_iso).date(), pd.Timestamp(max_iso).date()


def apply_date_filter(
    df: pd.DataFrame,
    start_date,
    end_date,
    *,
    show_warning: bool = True,
) -> pd.DataFrame:
    """Filter by inclusive datetime range; fall back to all data if result is empty.

    Accepts full datetime objects or date-only objects.  When a datetime is
    passed, it is used directly (time component preserved).  When a date object
    is passed, start defaults to midnight and end defaults to 23:59:59 so the
    full calendar day is included.
    """
    if df.empty or start_date is None or end_date is None:
        return df

    work = df.copy()
    # Align both sides to one tz state before comparing (see apply_datetime_filter).
    work["timestamp"] = normalize_series_tz(pd.to_datetime(work["timestamp"]))
    start = _parse_date_bound(start_date, is_end=False)
    end = _parse_date_bound(end_date, is_end=True)
    start = align_bound_to_series(start, work["timestamp"])
    end = align_bound_to_series(end, work["timestamp"])

    filtered = work[(work["timestamp"] >= start) & (work["timestamp"] <= end)]
    if filtered.empty and not work.empty:
        _warn_empty_range(work, show_warning)
        return work
    return filtered


def _parse_date_bound(value, *, is_end: bool) -> pd.Timestamp:
    """Convert a date or datetime bound to a Timestamp.

    datetime is a subclass of date, so it is checked first and used verbatim.
    A date-only end bound is extended to 23:59:59 so the full calendar day is
    included.
    """
    from datetime import datetime as _datetime

    if isinstance(value, _datetime):
        return pd.to_datetime(value)
    if is_end:
        return pd.Timestamp(value) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    return pd.Timestamp(value)


def _warn_empty_range(work: pd.DataFrame, show_warning: bool) -> None:
    """Warn (once) that the selected range matched nothing and all data is shown."""
    if not show_warning:
        return
    data_start = work["timestamp"].min()
    data_end = work["timestamp"].max()
    st.warning(
        "No readings in the selected date range. Showing all available data "
        f"({data_start.strftime('%Y-%m-%d')} → {data_end.strftime('%Y-%m-%d')}). "
        "Adjust **Date Range** in the sidebar or re-seed with `venv/bin/python run_seeds.py`."
    )


def clear_raw_cache() -> None:
    """Invalidate the raw readings time-window cache."""
    query_raw_readings.clear()


def clear_sensor_cache() -> None:
    """Invalidate cached sensor queries (call from Refresh Data)."""
    query_sensor_readings.clear()
    query_sensor_readings_window.clear()
    query_sensor_timestamp_bounds.clear()
    get_data_date_bounds.clear()
    query_anomaly_readings.clear()
    query_raw_readings.clear()
    query_incidents.clear()
    query_incidents_window.clear()
    from src.dashboard.components.monitoring_jump import clear_monitoring_prefetch
    from src.dashboard.services.anomalies_page_data import clear_anomaly_page_cache

    clear_monitoring_prefetch()
    clear_anomaly_page_cache()


# ---------------------------------------------------------------------------
# Anomaly page: time-windowed query cached independently from the full load
# ---------------------------------------------------------------------------

# Preset labels → hours.  Stored here so the page and tests share one source.
ANOMALY_RANGE_OPTIONS: dict = {
    "Last 24 hours": 24,
    "Last 48 hours": 48,
    "Last 7 days": 24 * 7,
    "Last 14 days": 24 * 14,
    "Last 30 days": 24 * 30,
    "Last 3 months": 24 * 90,
}
ANOMALY_RANGE_DEFAULT: str = "Last 7 days"


@st.cache_data(ttl=CACHE_TTL_MEDIUM)
def query_anomaly_readings(
    tenant_id: str,
    greenhouse_id: str,
    hours_back: int,
) -> pd.DataFrame:
    """Fetch only the rows within *hours_back* hours of now.

    The cache key is (tenant_id, greenhouse_id, hours_back) — stable across
    reruns — so the 2-minute TTL is the only thing that forces a refresh.
    """
    rows = _client.get_anomaly_readings(tenant_id, greenhouse_id, hours_back)
    return _rows_to_frame(rows)


def clear_anomaly_cache() -> None:
    """Invalidate the anomaly time-window cache."""
    query_anomaly_readings.clear()


# ---------------------------------------------------------------------------
# Raw readings: all CSV columns, time-windowed
# ---------------------------------------------------------------------------


@st.cache_data(ttl=CACHE_TTL_MEDIUM)
def query_raw_readings(
    tenant_id: str,
    greenhouse_id: str,
    hours_back: int,
) -> pd.DataFrame:
    """Fetch RawReading rows for the given time window, flatten JSON data into columns."""
    rows = _client.get_raw_readings(tenant_id, greenhouse_id, hours_back)
    return _rows_to_frame(rows)


def has_raw_readings(tenant_id: str, greenhouse_id: str) -> bool:
    """Return True if any RawReading exists for this tenant/greenhouse (any time)."""
    return _client.raw_readings_exist(tenant_id, greenhouse_id)


def has_raw_readings_in_window(
    tenant_id: str, greenhouse_id: str, hours_back: int
) -> bool:
    """Return True if RawReadings exist within the last hours_back hours."""
    return _client.raw_readings_exist(tenant_id, greenhouse_id, hours_back=hours_back)


@st.cache_data(ttl=CACHE_TTL_SHORT)
def get_available_greenhouses(tenant_id: str) -> List[str]:
    """Return distinct greenhouse IDs that have any data for this tenant."""
    greenhouses = _client.get_greenhouses(tenant_id)
    return greenhouses if greenhouses else ["gh_A", "gh_B", "gh_C", "default"]


_SENSOR_COLORS = {
    "temperature": "red",
    "humidity": "blue",
    "co2": "gray",
    "light": "orange",
}

SENSOR_LABELS = {
    "temperature": "Temperature (°C)",
    "humidity": "Relative Humidity (%)",
    "co2": "CO2 Concentration (ppm)",
    "light": "Light Intensity (lux)",
    "outdoor_temp": "Outdoor Temp (°C)",
    "measured_humidity_deficit": "Humidity Deficit (g/m³)",
    "measured_relative_humidity": "Relative Humidity (%)",
    "measured_plant_temp": "Plant Temp (°C)",
    "measured_greenhouse_temp": "Greenhouse Temp (°C)",
    "calculated_heating_temp": "Heating Setpoint (°C)",
    "measured_pipe_1_temp": "Pipe 1 Temp (°C)",
    "measured_pipe_2_temp": "Pipe 2 Temp (°C)",
    "measured_screen_1": "Screen Position (%)",
    "light_intensity": "Light Intensity (W/m²)",
    "measured_leeward_vent_2": "Leeward Vent (%)",
    "wind_speed": "Wind Speed (m/s)",
}
_SENSOR_LABELS = SENSOR_LABELS

_DEFAULT_CHART_COLORS: List[str] = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA",
    "#FFA15A", "#19D3F3", "#FF6692", "#B6E880",
]


def _resolve_line_colors(
    columns: Sequence[str],
    *,
    color_manager: ColorAssignmentManager | None = None,
    tab_context: str = "monitoring",
) -> dict[str, str]:
    """One unique color per column on the current chart."""
    if color_manager is not None:
        return color_manager.assign_colors(columns, tab_context)
    return {col: color_at_index(idx) for idx, col in enumerate(columns)}


def get_sensor_label(col: str) -> str:
    """Human-readable label for any column, falling back to title-cased column name."""
    return SENSOR_LABELS.get(col, col.replace("_", " ").title())


def get_sensor_color(col: str, *, color_index: int | None = None) -> str:
    """Consistent color for a column; prefer color_index for multi-series charts."""
    if color_index is not None:
        return color_at_index(color_index)
    if col in _SENSOR_COLORS:
        return _SENSOR_COLORS[col]
    return color_at_index(abs(hash(col)) % 1000)


def _sensor_trace(
    df: pd.DataFrame,
    sensor: str,
    *,
    color: Optional[str] = None,
    label: Optional[str] = None,
) -> go.Scatter:
    """Return a Plotly Scatter trace for a single sensor column."""
    return go.Scatter(
        x=df["timestamp"],
        y=df[sensor],
        mode="lines",
        name=label or get_sensor_label(sensor),
        line=dict(color=color or get_sensor_color(sensor)),
    )


def _apply_chart_theme(fig: go.Figure, *, bottom_margin: int = 20) -> go.Figure:
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#111111",
        margin=dict(t=40, b=bottom_margin),
    )
    return fig


def _apply_time_axis_style(
    fig: go.Figure,
    *,
    show_rangeslider: bool = False,
    rangeslider_thickness: float = 0.06,
) -> go.Figure:
    """Apply time-axis formatting."""
    xaxis_kwargs: dict = {"title_text": ""}
    if show_rangeslider:
        xaxis_kwargs["rangeslider"] = dict(visible=True, thickness=rangeslider_thickness)
    fig.update_xaxes(**xaxis_kwargs)
    fig.update_yaxes(title_text="", fixedrange=True)
    return fig


def apply_datetime_filter(
    df: pd.DataFrame,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> pd.DataFrame:
    """Filter by inclusive start/end datetime.

    Both sides of the comparison are aligned to one tz state first: a tz-aware
    Series compared against a tz-naive bound silently drops every row (a cause
    of the 'only recent data shows' symptom), so bounds are localized to match
    the data instead.
    """
    if df.empty:
        return df

    if pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        ts_series = df["timestamp"]
    else:
        ts_series = normalize_series_tz(pd.to_datetime(df["timestamp"]))

    start_dt = align_bound_to_series(pd.Timestamp(start_dt), ts_series)
    end_dt = align_bound_to_series(pd.Timestamp(end_dt), ts_series)
    
    mask = (ts_series >= start_dt) & (ts_series <= end_dt)
    work = df[mask].copy()
    
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        work["timestamp"] = ts_series[mask]
        
    return work


def _apply_x_range(
    fig: go.Figure,
    x_range: tuple[pd.Timestamp, pd.Timestamp] | None,
) -> go.Figure:
    """Pin chart x-axis to the selected timeline window."""
    if x_range is None:
        return fig
    start, end = x_range
    fig.update_xaxes(range=[start, end])
    return fig


_CHART_MAX_POINTS = 2000

def downsample_for_chart(df: pd.DataFrame, max_points: int = _CHART_MAX_POINTS) -> pd.DataFrame:
    if df.empty or len(df) <= max_points:
        return df
    step = max(1, len(df) // max_points)
    work = df.sort_values("timestamp") if "timestamp" in df.columns else df
    return work.iloc[::step].copy()


def plot_time_series(df: pd.DataFrame, column: str, title: str, color: str = "blue"):
    df = downsample_for_chart(df)
    if df.empty:
        return go.Figure()
    fig = px.line(df, x="timestamp", y=column, title=title)
    fig.update_traces(line_color=color)
    fig.update_layout(
        dragmode="pan",
        margin=dict(t=40, b=20),
    )
    return _apply_time_axis_style(fig)


def plot_synced_sensor_grid(
    df: pd.DataFrame,
    columns: List[str],
    *,
    color_manager=None,
    tab_context: str = "climate",
    x_range: tuple[pd.Timestamp, pd.Timestamp] | None = None,
) -> go.Figure:
    """Dynamic 2-column subplot grid with synced x-axis zoom across all panels."""
    df = downsample_for_chart(df)
    n = len(columns)
    if n == 0:
        return go.Figure()

    n_rows = math.ceil(n / 2)
    subtitles = [get_sensor_label(c) for c in columns]
    if n % 2 == 1:
        subtitles.append("")

    fig = make_subplots(
        rows=n_rows,
        cols=2,
        shared_xaxes=True,
        subplot_titles=subtitles,
        vertical_spacing=0.1,
        horizontal_spacing=0.08,
    )

    line_colors = _resolve_line_colors(
        columns,
        color_manager=color_manager,
        tab_context=tab_context,
    )
    xaxis_updates: dict = {}
    for idx, col in enumerate(columns):
        row_pos = idx // 2 + 1
        col_pos = idx % 2 + 1
        if col in df.columns:
            fig.add_trace(
                _sensor_trace(df, col, color=line_colors[col]),
                row=row_pos,
                col=col_pos,
            )
        axis_num = (row_pos - 1) * 2 + col_pos
        if axis_num > 1:
            xaxis_updates[f"xaxis{axis_num}"] = dict(matches="x")

    if xaxis_updates:
        fig.update_layout(**xaxis_updates)

    fig.update_xaxes(showticklabels=True, title_text="")
    fig.update_yaxes(fixedrange=True, title_text="")
    fig.update_layout(
        height=max(500, n_rows * 250),
        showlegend=False,
        dragmode="pan",
        xaxis=dict(),
    )
    _apply_chart_theme(fig, bottom_margin=80)
    fig.update_annotations(font_size=14)
    return _apply_x_range(fig, x_range)


def plot_combined_sensors(
    df: pd.DataFrame,
    columns: List[str],
    *,
    color_manager=None,
    tab_context: str = "monitoring",
    normalized: bool = True,
    labels: Optional[Mapping[str, str]] = None,
    x_range: tuple[pd.Timestamp, pd.Timestamp] | None = None,
) -> go.Figure:
    """Single chart with one line per column; normalized by default for mixed units."""
    df = downsample_for_chart(df)
    fig = go.Figure()
    plottable = [col for col in columns if col in df.columns]
    line_colors = _resolve_line_colors(
        plottable,
        color_manager=color_manager,
        tab_context=tab_context,
    )
    for col in plottable:
        series = pd.to_numeric(df[col], errors="coerce")
        if series.isna().all():
            continue
        display_label = (labels or {}).get(col, get_sensor_label(col))
        line_color = line_colors[col]
        if normalized:
            col_min = series.min()
            col_max = series.max()
            span = col_max - col_min
            y_values = (
                (series - col_min) / span * 100
                if span > 0
                else pd.Series(50.0, index=df.index)
            )
            hover = (
                f"<b>{display_label}</b><br>"
                "Value: %{customdata}<br>"
                "Time: %{x}<extra></extra>"
            )
            customdata = series.round(3)
        else:
            y_values = series
            hover = (
                f"<b>{display_label}</b><br>"
                "Value: %{y}<br>"
                "Time: %{x}<extra></extra>"
            )
            customdata = None
        fig.add_trace(go.Scatter(
            x=df["timestamp"],
            y=y_values,
            mode="lines",
            name=display_label,
            line=dict(color=line_color),
            hovertemplate=hover,
            customdata=customdata,
        ))
    yaxis_kwargs = dict(
        title="",
        fixedrange=True,
        showgrid=True,
        gridwidth=1,
        gridcolor="#333333",
    )
    if normalized:
        yaxis_kwargs.update(
            range=[0, 100],
            showticklabels=False,
        )
    fig.update_layout(
        dragmode="pan",
        height=500,
        margin=dict(t=20, b=140),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.18,
            xanchor="center",
            x=0.5,
        ),
        yaxis=yaxis_kwargs,
    )
    _apply_chart_theme(fig, bottom_margin=140)
    fig = _apply_time_axis_style(fig, show_rangeslider=False)
    return _apply_x_range(fig, x_range)


def plot_gas_sensor_stack(
    df: pd.DataFrame,
    columns: Sequence[str],
    labels: Mapping[str, str],
    units: Mapping[str, str],
    anomaly_points: Sequence[dict],
    *,
    color_manager=None,
    tab_context: str = "gas",
) -> go.Figure:
    """Vertical stack of gas sensor subplots with real Y values and anomaly markers."""
    df = downsample_for_chart(df)
    valid_cols = [
        col for col in columns
        if col in df.columns and not pd.to_numeric(df[col], errors="coerce").isna().all()
    ]
    if not valid_cols:
        return go.Figure()

    n_rows = len(valid_cols)
    subplot_titles = [
        f"{labels.get(col, col.upper())} ({units.get(col, '')})"
        for col in valid_cols
    ]
    severity_colors = {"critical": "#ef4444", "warning": "#f59e0b", "normal": "#94a3b8"}

    fig = make_subplots(
        rows=n_rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06 if n_rows > 1 else 0,
        subplot_titles=subplot_titles,
    )

    line_colors = _resolve_line_colors(
        valid_cols,
        color_manager=color_manager,
        tab_context=tab_context,
    )

    for row_idx, col in enumerate(valid_cols, start=1):
        series = pd.to_numeric(df[col], errors="coerce")
        unit = units.get(col, "")
        label = labels.get(col, col.upper())
        line_color = line_colors[col]

        fig.add_trace(
            go.Scatter(
                x=df["timestamp"],
                y=series,
                mode="lines",
                name=label,
                line=dict(width=1.5, color=line_color),
                hovertemplate=(
                    f"<b>{label}</b><br>Value: %{{y:.4f}} {unit}<br>Time: %{{x}}<extra></extra>"
                ),
            ),
            row=row_idx,
            col=1,
        )

        for ap in (p for p in anomaly_points if p.get("column") == col):
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
                        color=severity_colors.get(ap.get("severity", ""), "#ef4444"),
                        size=9,
                        symbol="circle",
                        line=dict(color="white", width=1),
                    ),
                    showlegend=False,
                    hovertemplate=(
                        f"<b>⚠ {label}</b><br>"
                        f"Value: {raw_val:.4f} {unit}<br>"
                        f"Z-score: {ap.get('z_score', 0):.2f}<br>"
                        f"Severity: {ap.get('severity', 'normal')}<br>"
                        "Time: %{x}<extra></extra>"
                    ),
                ),
                row=row_idx,
                col=1,
            )

        fig.update_yaxes(title_text=unit, row=row_idx, col=1)

    fig.update_layout(
        height=max(500, 250 * n_rows),
        dragmode="pan",
        showlegend=False,
        xaxis=dict(type="date"),
    )
    _apply_chart_theme(fig, bottom_margin=80)
    return fig


def plot_gas_combined(
    df: pd.DataFrame,
    columns: Sequence[str],
    labels: Mapping[str, str],
    *,
    color_manager=None,
    tab_context: str = "gas",
) -> go.Figure:
    """All gas sensors on one normalized chart with legend."""
    display_columns = [
        col for col in columns
        if col in df.columns and not pd.to_numeric(df[col], errors="coerce").isna().all()
    ]
    if not display_columns:
        return go.Figure()

    return plot_combined_sensors(
        df,
        list(display_columns),
        color_manager=color_manager,
        tab_context=tab_context,
        normalized=True,
        labels=labels,
    )


def _timeline_message_figure(message: str, *, title: str = "Anomaly Score Timeline") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        title=title,
        xaxis_title="Time",
        yaxis_title="Anomaly score",
        height=420,
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        annotations=[
            dict(
                text=message,
                xref="paper",
                yref="paper",
                x=0.5,
                y=0.5,
                showarrow=False,
                font=dict(size=13, color="#94a3b8"),
            )
        ],
    )
    return _apply_chart_theme(fig, bottom_margin=60)


def plot_anomaly_timeline(
    df: pd.DataFrame,
    threshold: float = DEFAULT_ANOMALY_THRESHOLD,
    *,
    anomalies_only: bool = True,
    x_range: tuple[pd.Timestamp, pd.Timestamp] | None = None,
):
    """Scatter timeline with discrete dots at each anomaly score.

    Only non-null scored rows are drawn. Marker colour reflects threshold
    bands; size is modest so dense periods do not form a solid stripe.
    """
    df = downsample_for_chart(df)
    if df.empty or "anomaly_score" not in df.columns:
        return _timeline_message_figure("No scored readings in the selected date range.")

    from src.dashboard.services.anomaly_events import flagged_anomaly_mask

    scored = df.copy()
    scored["anomaly_score"] = pd.to_numeric(scored["anomaly_score"], errors="coerce")
    scored = scored.dropna(subset=["anomaly_score"])
    if scored.empty:
        return _timeline_message_figure("No scored readings in the selected date range.")
    if anomalies_only:
        scored = scored[flagged_anomaly_mask(scored["anomaly_score"], threshold)]
    if scored.empty:
        return _timeline_message_figure(
            f"No anomalies at or above threshold ({threshold:.3f}) in the selected range.",
            title="Anomaly Score Timeline — no anomalies in range",
        )

    scored["timestamp"] = pd.to_datetime(scored["timestamp"])

    def _severity(score: float) -> str:
        if score >= threshold:
            return "Critical"
        if score >= threshold * 0.5:
            return "Warning"
        return "Below threshold"

    scored["severity"] = scored["anomaly_score"].apply(_severity)

    color_map = {
        "Below threshold": "#64748b",
        "Warning": "#f59e0b",
        "Critical": "#ef4444",
    }
    size_map = {
        "Below threshold": 7,
        "Warning": 9,
        "Critical": 11,
    }

    fig = go.Figure()
    for severity in ("Below threshold", "Warning", "Critical"):
        subset = scored[scored["severity"] == severity]
        if subset.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=subset["timestamp"],
                y=subset["anomaly_score"],
                mode="markers",
                name=severity,
                marker=dict(
                    size=size_map[severity],
                    color=color_map[severity],
                    opacity=0.75,
                    line=dict(width=0),
                ),
                hovertemplate=(
                    "Time: %{x}<br>"
                    "Score: %{y:.4f}<br>"
                    f"Severity: {severity}<extra></extra>"
                ),
            )
        )

    fig.add_hline(
        y=threshold,
        line_dash="dash",
        line_color="#ef4444",
        annotation_text=f"Threshold ({threshold})",
    )
    y_max = max(float(scored["anomaly_score"].max()), threshold * 1.1, 0.1)
    fig.update_layout(
        title="Anomaly Score Timeline",
        xaxis_title="Time",
        yaxis_title="Anomaly score",
        yaxis=dict(range=[0, min(1.05, y_max * 1.15)]),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        height=420,
    )
    fig = _apply_x_range(fig, x_range)
    _apply_chart_theme(fig, bottom_margin=60)
    return fig
