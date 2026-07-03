"""Queue navigation from upload/anomaly tables to the Monitoring page."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src.dashboard.components.charts import apply_datetime_filter
from src.dashboard.components.global_sidebar import queue_sidebar_selection
from src.dashboard.services.diagnosis_enricher import canonical_slot_for_column

_PENDING_JUMP_KEY = "_pending_monitoring_jump"
_MONITORING_PREFETCH_KEY = "_monitoring_prefetch"


def resolve_highlight_columns(row: dict[str, Any], available_columns: list[str]) -> list[str]:
    """Map an anomaly row to dataframe column names for chart selection."""
    column = str(row.get("column") or "").strip()
    if column:
        if column in available_columns:
            return [column]
        col_lower = column.lower()
        for name in available_columns:
            if name.lower() == col_lower:
                return [name]
        slot = canonical_slot_for_column(column)
        if slot:
            for name in available_columns:
                if canonical_slot_for_column(name) == slot:
                    return [name]
        return []

    detector = str(row.get("detector", "lstm"))
    if "lstm" in detector:
        return list(available_columns)
    return []


def peek_pending_monitoring_jump() -> dict[str, Any] | None:
    """Return a queued jump without consuming it."""
    pending = st.session_state.get(_PENDING_JUMP_KEY)
    if not pending:
        return None
    return dict(pending)


def clear_monitoring_prefetch() -> None:
    """Drop session handoff cache (e.g. after refresh or tenant change)."""
    st.session_state.pop(_MONITORING_PREFETCH_KEY, None)


def _consume_monitoring_prefetch(
    tenant_id: str,
    greenhouse_id: str,
    time_start: pd.Timestamp,
    time_end: pd.Timestamp,
) -> dict[str, Any] | None:
    prefetch = st.session_state.pop(_MONITORING_PREFETCH_KEY, None)
    if not prefetch:
        return None
    if prefetch.get("tenant_id") != tenant_id or prefetch.get("greenhouse_id") != greenhouse_id:
        return None
    if pd.Timestamp(prefetch["time_start"]) != time_start:
        return None
    if pd.Timestamp(prefetch["time_end"]) != time_end:
        return None
    return prefetch


def load_monitoring_readings(
    tenant_id: str,
    greenhouse_id: str,
) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    """Load sensor data for Monitoring; prefer session prefetch or window query on jump."""
    from src.dashboard.components.charts import (
        query_sensor_readings,
        query_sensor_readings_window,
        query_sensor_timestamp_bounds,
    )

    pending = peek_pending_monitoring_jump()
    if pending:
        time_start = pd.Timestamp(pending["time_start"])
        time_end = pd.Timestamp(pending["time_end"])

        prefetch = _consume_monitoring_prefetch(
            tenant_id, greenhouse_id, time_start, time_end
        )
        if prefetch is not None and not prefetch["df"].empty:
            return (
                prefetch["df"].copy(),
                pd.Timestamp(prefetch["data_min"]),
                pd.Timestamp(prefetch["data_max"]),
            )

        df = query_sensor_readings_window(
            tenant_id,
            greenhouse_id,
            time_start.isoformat(),
            time_end.isoformat(),
        )
        min_iso, max_iso = query_sensor_timestamp_bounds(tenant_id, greenhouse_id)
        if min_iso and max_iso:
            data_min = pd.Timestamp(min_iso)
            data_max = pd.Timestamp(max_iso)
        elif not df.empty:
            ts = pd.to_datetime(df["timestamp"])
            data_min, data_max = ts.min(), ts.max()
        else:
            data_min, data_max = time_start, time_end
        return df, data_min, data_max

    # Landing load is a bounded recent window (C2); the timeline bounds come
    # from the cheap min/max endpoint so navigation still spans full history.
    df = query_sensor_readings(tenant_id, greenhouse_id)
    if df.empty:
        now = pd.Timestamp.utcnow().tz_localize(None)
        return df, now, now
    min_iso, max_iso = query_sensor_timestamp_bounds(tenant_id, greenhouse_id)
    ts = pd.to_datetime(df["timestamp"])
    data_min = pd.Timestamp(min_iso) if min_iso else ts.min()
    data_max = pd.Timestamp(max_iso) if max_iso else ts.max()
    return df, data_min, data_max


def _sync_monitor_timeline_pickers(start: pd.Timestamp, end: pd.Timestamp) -> None:
    prefix = "monitor"
    st.session_state[f"{prefix}_from_date"] = start.date()
    st.session_state[f"{prefix}_from_time"] = start.time().replace(microsecond=0)
    st.session_state[f"{prefix}_to_date"] = end.date()
    st.session_state[f"{prefix}_to_time"] = end.time().replace(microsecond=0)
    st.session_state[f"{prefix}_past_select"] = "__please_select__"


def apply_sensor_checkbox_selection(
    columns_to_show: list[str],
    all_columns: list[str],
) -> None:
    """Set checkbox widget keys before Monitoring checkboxes render."""
    show_set = set(columns_to_show)
    if not show_set and all_columns:
        show_set = set(all_columns[:2])
    for col in all_columns:
        st.session_state[f"cb_{col}"] = col in show_set


def queue_monitoring_jump(
    row: dict[str, Any],
    tenant_id: str,
    greenhouse_id: str,
    *,
    available_columns: list[str] | None = None,
    source_df: pd.DataFrame | None = None,
) -> None:
    """Stage Monitoring navigation; apply on the Monitoring page before widgets mount."""
    ts_str = row.get("timestamp", "")
    try:
        ts = pd.Timestamp(ts_str).tz_localize(None)
    except Exception:
        raise ValueError(f"Cannot parse timestamp: {ts_str}") from None

    day_start = ts.normalize()
    day_end = day_start + pd.Timedelta(hours=23, minutes=59, seconds=59)
    highlight_cols = (
        resolve_highlight_columns(row, available_columns)
        if available_columns
        else []
    )

    st.session_state[_PENDING_JUMP_KEY] = {
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "time_start": day_start,
        "time_end": day_end,
        "highlights": [dict(row)],
        "highlight_columns": highlight_cols,
    }

    if source_df is not None and not source_df.empty:
        ts_series = pd.to_datetime(source_df["timestamp"])
        day_df = apply_datetime_filter(source_df, day_start, day_end)
        st.session_state[_MONITORING_PREFETCH_KEY] = {
            "tenant_id": tenant_id,
            "greenhouse_id": greenhouse_id,
            "time_start": day_start,
            "time_end": day_end,
            "data_min": ts_series.min(),
            "data_max": ts_series.max(),
            "df": day_df.copy(),
        }
    else:
        clear_monitoring_prefetch()

    queue_sidebar_selection(tenant=tenant_id, greenhouse=greenhouse_id)


def apply_pending_monitoring_jump(all_columns: list[str] | None = None) -> bool:
    """Apply a queued jump after session init, before timeline/checkbox widgets."""
    pending = st.session_state.pop(_PENDING_JUMP_KEY, None)
    if not pending:
        return False

    st.session_state["time_start"] = pending["time_start"]
    st.session_state["time_end"] = pending["time_end"]
    st.session_state["date_range_mode"] = "custom"
    st.session_state["_last_apply_source"] = "custom"
    st.session_state["monitoring_anomaly_highlights"] = pending["highlights"]
    _sync_monitor_timeline_pickers(pending["time_start"], pending["time_end"])

    columns = all_columns or []
    highlight_cols = pending.get("highlight_columns") or []
    if columns:
        if highlight_cols:
            apply_sensor_checkbox_selection(highlight_cols, columns)
        else:
            row = pending["highlights"][0] if pending["highlights"] else {}
            resolved = resolve_highlight_columns(row, columns)
            apply_sensor_checkbox_selection(resolved or columns[:2], columns)
    return True


_DISPLAY_COLUMN_ORDER = (
    "timestamp",
    "detector",
    "score",
    "column",
    "severity",
    "z_score",
    "method",
    "value",
    "agreement",
)

_DISPLAY_COLUMN_LABELS = {
    "timestamp": "Timestamp",
    "detector": "Detector",
    "score": "Score",
    "column": "Column",
    "severity": "Severity",
    "z_score": "Z-score",
    "method": "Method",
    "value": "Value",
    "agreement": "Agreement",
}


def anomaly_display_columns(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    available = set(pd.DataFrame(rows).columns)
    return [col for col in _DISPLAY_COLUMN_ORDER if col in available]


def format_anomaly_cell(column: str, value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    if column == "timestamp":
        return str(value)[:19].replace("T", " ")
    if column == "score":
        try:
            return f"{float(value):.3f}"
        except (TypeError, ValueError):
            return "—"
    if column == "z_score":
        try:
            return f"{float(value):.1f}"
        except (TypeError, ValueError):
            return "—"
    if column == "agreement":
        return "✓" if value else "—"
    return str(value)


def anomalies_to_display_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalize anomaly dicts for a consistent dataframe table."""
    if not rows:
        return pd.DataFrame()
    columns = anomaly_display_columns(rows)
    records = [
        {col: format_anomaly_cell(col, row.get(col)) for col in columns}
        for row in rows
    ]
    return pd.DataFrame(records, columns=columns)
