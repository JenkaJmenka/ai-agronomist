import math
from typing import List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import select

from src.db.models import RawReading, SensorReading
from src.db.session import AsyncSessionLocal


def _run_async(coro):
    """Run an async coroutine from Streamlit's sync context."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@st.cache_data(ttl=60)
def query_sensor_readings(tenant_id: str, greenhouse_id: str) -> pd.DataFrame:
    async def fetch():
        async with AsyncSessionLocal() as session:
            stmt = (
                select(SensorReading)
                .where(
                    SensorReading.tenant_id == tenant_id,
                    SensorReading.greenhouse_id == greenhouse_id,
                )
                .order_by(SensorReading.timestamp)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()

            data = [
                {
                    "timestamp": r.timestamp,
                    "temperature": r.temperature,
                    "humidity": r.humidity,
                    "co2": r.co2,
                    "light": r.light,
                    "anomaly_score": r.anomaly_score,
                }
                for r in records
            ]
            return pd.DataFrame(data)

    return _run_async(fetch())


@st.cache_data(ttl=60)
def get_data_date_bounds(tenant_id: str, greenhouse_id: str):
    """Return (min_date, max_date) for available readings, or (None, None)."""
    df = query_sensor_readings(tenant_id, greenhouse_id)
    if df.empty:
        return None, None
    ts = pd.to_datetime(df["timestamp"])
    return ts.min().date(), ts.max().date()


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
    from datetime import datetime as _datetime

    if df.empty or start_date is None or end_date is None:
        return df

    work = df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])

    # datetime is a subclass of date, so check for datetime first.
    if isinstance(start_date, _datetime):
        start = pd.to_datetime(start_date)
    else:
        start = pd.Timestamp(start_date)

    if isinstance(end_date, _datetime):
        end = pd.to_datetime(end_date)
    else:
        # date-only: extend to end of day so the full calendar day is included.
        end = pd.Timestamp(end_date) + pd.Timedelta(hours=23, minutes=59, seconds=59)

    if work["timestamp"].dt.tz is not None:
        start = start.tz_localize("UTC") if start.tzinfo is None else start
        end = end.tz_localize("UTC") if end.tzinfo is None else end

    filtered = work[(work["timestamp"] >= start) & (work["timestamp"] <= end)]
    if filtered.empty and not work.empty:
        data_start = work["timestamp"].min()
        data_end = work["timestamp"].max()
        if show_warning:
            st.warning(
                "No readings in the selected date range. Showing all available data "
                f"({data_start.strftime('%Y-%m-%d')} → {data_end.strftime('%Y-%m-%d')}). "
                "Adjust **Date Range** in the sidebar or re-seed with `venv/bin/python run_seeds.py`."
            )
        return work
    return filtered


def clear_raw_cache() -> None:
    """Invalidate the raw readings time-window cache."""
    query_raw_readings.clear()


def clear_sensor_cache() -> None:
    """Invalidate cached sensor queries (call from Refresh Data)."""
    query_sensor_readings.clear()
    get_data_date_bounds.clear()
    query_anomaly_readings.clear()
    query_raw_readings.clear()


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


@st.cache_data(ttl=120)
def query_anomaly_readings(
    tenant_id: str,
    greenhouse_id: str,
    hours_back: int,
) -> pd.DataFrame:
    """Fetch only the rows within *hours_back* hours of now.

    The cache key is (tenant_id, greenhouse_id, hours_back) — stable across
    reruns — so the 2-minute TTL is the only thing that forces a refresh.
    """
    async def fetch():
        end_dt = pd.Timestamp.utcnow()
        start_dt = end_dt - pd.Timedelta(hours=hours_back)
        async with AsyncSessionLocal() as session:
            stmt = (
                select(SensorReading)
                .where(
                    SensorReading.tenant_id == tenant_id,
                    SensorReading.greenhouse_id == greenhouse_id,
                    SensorReading.timestamp >= start_dt,
                    SensorReading.timestamp <= end_dt,
                )
                .order_by(SensorReading.timestamp)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            data = [
                {
                    "timestamp": r.timestamp,
                    "temperature": r.temperature,
                    "humidity": r.humidity,
                    "co2": r.co2,
                    "light": r.light,
                    "anomaly_score": r.anomaly_score,
                }
                for r in records
            ]
            return pd.DataFrame(data)

    return _run_async(fetch())


def clear_anomaly_cache() -> None:
    """Invalidate the anomaly time-window cache."""
    query_anomaly_readings.clear()


# ---------------------------------------------------------------------------
# Raw readings: all CSV columns, time-windowed
# ---------------------------------------------------------------------------


@st.cache_data(ttl=120)
def query_raw_readings(
    tenant_id: str,
    greenhouse_id: str,
    hours_back: int,
) -> pd.DataFrame:
    """Fetch RawReading rows for the given time window, flatten JSON data into columns."""
    async def fetch():
        end_dt = pd.Timestamp.utcnow()
        start_dt = end_dt - pd.Timedelta(hours=hours_back)
        async with AsyncSessionLocal() as session:
            stmt = (
                select(RawReading)
                .where(
                    RawReading.tenant_id == tenant_id,
                    RawReading.greenhouse_id == greenhouse_id,
                    RawReading.timestamp >= start_dt,
                    RawReading.timestamp <= end_dt,
                )
                .order_by(RawReading.timestamp)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            if not records:
                return pd.DataFrame()
            rows = []
            for r in records:
                row = {"timestamp": r.timestamp, "source": r.source}
                row.update(r.data or {})
                rows.append(row)
            return pd.DataFrame(rows)

    return _run_async(fetch())


def has_raw_readings(tenant_id: str, greenhouse_id: str) -> bool:
    """Return True if any RawReading exists for this tenant/greenhouse (any time)."""
    async def check():
        from sqlalchemy import func as sqlfunc
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(sqlfunc.count()).select_from(RawReading).where(
                    RawReading.tenant_id == tenant_id,
                    RawReading.greenhouse_id == greenhouse_id,
                )
            )
            count = result.scalar()
            return (count if count is not None else 0) > 0

    return _run_async(check())


def has_raw_readings_in_window(
    tenant_id: str, greenhouse_id: str, hours_back: int
) -> bool:
    """Return True if RawReadings exist within the last hours_back hours."""
    async def check():
        from sqlalchemy import func as sqlfunc
        end_dt = pd.Timestamp.utcnow()
        start_dt = end_dt - pd.Timedelta(hours=hours_back)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(sqlfunc.count()).select_from(RawReading).where(
                    RawReading.tenant_id == tenant_id,
                    RawReading.greenhouse_id == greenhouse_id,
                    RawReading.timestamp >= start_dt,
                    RawReading.timestamp <= end_dt,
                )
            )
            count = result.scalar()
            return (count if count is not None else 0) > 0

    return _run_async(check())


@st.cache_data(ttl=60)
def get_available_greenhouses(tenant_id: str) -> List[str]:
    """Return distinct greenhouse IDs that have any data for this tenant."""
    async def fetch():
        from sqlalchemy import distinct
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(distinct(RawReading.greenhouse_id))
                .where(RawReading.tenant_id == tenant_id)
            )).scalars().all()
            if not rows:
                rows = (await session.execute(
                    select(distinct(SensorReading.greenhouse_id))
                    .where(SensorReading.tenant_id == tenant_id)
                )).scalars().all()
            return sorted(rows) if rows else ["gh_A", "gh_B", "gh_C", "default"]

    return _run_async(fetch())


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
}
_SENSOR_LABELS = SENSOR_LABELS

_DEFAULT_CHART_COLORS: List[str] = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA",
    "#FFA15A", "#19D3F3", "#FF6692", "#B6E880",
]


def get_sensor_label(col: str) -> str:
    """Human-readable label for any column, falling back to title-cased column name."""
    return SENSOR_LABELS.get(col, col.replace("_", " ").title())


def get_sensor_color(col: str) -> str:
    """Consistent color for any column; uses palette hash for unknown columns."""
    if col in _SENSOR_COLORS:
        return _SENSOR_COLORS[col]
    return _DEFAULT_CHART_COLORS[hash(col) % len(_DEFAULT_CHART_COLORS)]


def _sensor_trace(df: pd.DataFrame, sensor: str) -> go.Scatter:
    """Return a Plotly Scatter trace for a single sensor column."""
    return go.Scatter(
        x=df["timestamp"],
        y=df[sensor],
        mode="lines",
        name=get_sensor_label(sensor),
        line=dict(color=get_sensor_color(sensor)),
    )


def _apply_time_axis_style(fig: go.Figure) -> go.Figure:
    """Apply consistent time-axis formatting to any single-chart figure.

    Tick density and format are determined automatically by Plotly based on
    the visible range — no hardcoded dtick so the first tick aligns with the
    actual data start.  Y-axis is locked (fixedrange=True) so drag selects
    only the x-range.  A thin rangeslider below the chart lets the operator
    scroll across the full loaded dataset.
    """
    fig.update_xaxes(
        title_text="",
        rangeslider=dict(visible=True, thickness=0.04),
    )
    fig.update_yaxes(title_text="", fixedrange=True)
    return fig


def apply_datetime_filter(
    df: pd.DataFrame,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> pd.DataFrame:
    """Filter by inclusive start/end datetime; returns full df if result would be empty."""
    if df.empty:
        return df
    work = df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    data_is_tz_aware = work["timestamp"].dt.tz is not None
    start_is_tz_aware = getattr(start_dt, "tzinfo", None) is not None
    end_is_tz_aware = getattr(end_dt, "tzinfo", None) is not None
    if data_is_tz_aware:
        # Data has timezone → make bounds tz-aware (UTC)
        if not start_is_tz_aware:
            start_dt = pd.Timestamp(start_dt).tz_localize("UTC")
        if not end_is_tz_aware:
            end_dt = pd.Timestamp(end_dt).tz_localize("UTC")
    else:
        # Data is tz-naive → strip timezone from bounds
        if start_is_tz_aware:
            start_dt = pd.Timestamp(start_dt).tz_localize(None)
        if end_is_tz_aware:
            end_dt = pd.Timestamp(end_dt).tz_localize(None)
    filtered = work[(work["timestamp"] >= start_dt) & (work["timestamp"] <= end_dt)]
    return filtered if not filtered.empty else work


def plot_time_series(df: pd.DataFrame, column: str, title: str, color: str = "blue"):
    if df.empty:
        return go.Figure()
    fig = px.line(df, x="timestamp", y=column, title=title)
    fig.update_traces(line_color=color)
    fig.update_layout(
        dragmode="pan",
        margin=dict(t=40, b=20),
    )
    return _apply_time_axis_style(fig)


def plot_synced_sensor_grid(df: pd.DataFrame, columns: List[str]) -> go.Figure:
    """Dynamic 2-column subplot grid with synced x-axis zoom across all panels.

    Accepts any list of column names from df. Panels fill left-to-right, top-to-bottom.
    All x-axes reference xaxis via ``matches='x'`` so zooming or panning any panel
    instantly updates the others. ``yaxis.fixedrange=True`` locks y-axes so drag
    gestures only change the x-range.
    """
    n = len(columns)
    if n == 0:
        return go.Figure()

    n_rows = math.ceil(n / 2)
    subtitles = [get_sensor_label(c) for c in columns]
    if n % 2 == 1:
        subtitles.append("")  # placeholder so subplot_titles length matches grid

    fig = make_subplots(
        rows=n_rows,
        cols=2,
        shared_xaxes=True,
        subplot_titles=subtitles,
        vertical_spacing=0.1,
        horizontal_spacing=0.08,
    )

    xaxis_updates: dict = {}
    for idx, col in enumerate(columns):
        row_pos = idx // 2 + 1
        col_pos = idx % 2 + 1
        if col in df.columns:
            fig.add_trace(_sensor_trace(df, col), row=row_pos, col=col_pos)
        axis_num = (row_pos - 1) * 2 + col_pos
        if axis_num > 1:
            xaxis_updates[f"xaxis{axis_num}"] = dict(matches="x")

    if xaxis_updates:
        fig.update_layout(**xaxis_updates)

    fig.update_xaxes(showticklabels=True, title_text="")
    fig.update_yaxes(fixedrange=True, title_text="")
    fig.update_layout(
        xaxis=dict(rangeslider=dict(visible=True, thickness=0.06)),
        height=max(420, n_rows * max(160, 320 - n_rows * 8)),
        showlegend=False,
        margin=dict(t=40, b=20),
        dragmode="pan",
    )
    fig.update_annotations(font_size=14)
    return fig


def plot_combined_sensors(df: pd.DataFrame, columns: List[str]) -> go.Figure:
    """Single chart with one line per column, each normalized to 0–100 % of its visible range.

    Different physical units (°C, %, ppm, lux) would make a shared raw-value axis
    unreadable. Normalizing lets all sensors coexist on one axis while the hover
    tooltip still shows the actual value.
    """
    fig = go.Figure()
    for col in columns:
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        if series.isna().all():
            continue
        col_min = series.min()
        col_max = series.max()
        span = col_max - col_min
        normalized = (series - col_min) / span * 100 if span > 0 else pd.Series(50.0, index=df.index)
        fig.add_trace(go.Scatter(
            x=df["timestamp"],
            y=normalized,
            mode="lines",
            name=get_sensor_label(col),
            line=dict(color=get_sensor_color(col)),
            hovertemplate=(
                f"<b>{get_sensor_label(col)}</b><br>"
                "Value: %{customdata}<br>"
                "Time: %{x}<extra></extra>"
            ),
            customdata=series.round(3),
        ))
    fig.update_layout(
        dragmode="pan",
        height=540,
        margin=dict(t=20, b=140),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.18,
            xanchor="center",
            x=0.5,
        ),
        yaxis=dict(
            title="% of range",
            fixedrange=True,
            range=[0, 100],
            dtick=10,
            ticksuffix="%",
        ),
    )
    return _apply_time_axis_style(fig)


def plot_anomaly_timeline(df: pd.DataFrame, threshold: float = 0.8):
    if df.empty or "anomaly_score" not in df.columns:
        return go.Figure()

    anomalies = df.copy()
    anomalies["anomaly_score"] = pd.to_numeric(
        anomalies["anomaly_score"], errors="coerce"
    )

    def assign_severity(score):
        if pd.isna(score):
            return "Unknown"
        if score >= threshold:
            return "Critical"
        elif score >= threshold * 0.5:
            return "Warning"
        return "Normal"

    anomalies["severity"] = anomalies["anomaly_score"].apply(assign_severity)

    color_map = {
        "Normal": "green",
        "Warning": "orange",
        "Critical": "red",
        "Unknown": "gray",
    }
    fig = px.scatter(
        anomalies,
        x="timestamp",
        y="anomaly_score",
        color="severity",
        color_discrete_map=color_map,
        title="Anomaly Score Timeline",
    )
    fig.add_hline(
        y=threshold,
        line_dash="dash",
        line_color="red",
        annotation_text=f"Threshold ({threshold})",
    )
    return fig
