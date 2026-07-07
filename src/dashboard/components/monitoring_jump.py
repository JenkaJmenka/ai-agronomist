"""Queue navigation from upload/anomaly tables to the Monitoring page."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src.dashboard.components.charts import apply_datetime_filter
from src.dashboard.components.global_sidebar import queue_sidebar_selection
from src.dashboard.services.diagnosis_enricher import canonical_slot_for_column
from src.dashboard.services.tz_normalize import strip_tz

_PENDING_JUMP_KEY = "_pending_monitoring_jump"
_PENDING_JUMP_COLUMNS_KEY = "_pending_monitoring_jump_columns"
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
    pass
def load_monitoring_window(
    tenant_id: str,
    greenhouse_id: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Load sensor readings for [start, end].

    Uses resolve_window_dataframe (which is @st.cache_data cached) to fetch
    the exact selected window, guaranteeing sub-second reads for jumps and
    incorporating unpersisted upload data natively.
    """
    from src.dashboard.services.anomalies_page_data import resolve_window_dataframe
    from src.dashboard.components.inference_results import resolve_upload_results

    inf_results = resolve_upload_results(None)
    
    df, _ = resolve_window_dataframe(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
        window_start=start,
        window_end=end,
        upload_job_id=str(inf_results.get("job_id")) if inf_results else None,
        _inf_results=inf_results,
    )
    return df


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

    # Prefetch the 24-hour dataframe right now using the cached resolve_window_dataframe.
    # This ensures it's instantly available when the Monitoring page calls load_monitoring_window.
    from src.dashboard.services.anomalies_page_data import resolve_window_dataframe
    from src.dashboard.components.inference_results import resolve_upload_results

    inf_results = resolve_upload_results(None)
    resolve_window_dataframe(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start_iso=day_start.isoformat(),
        end_iso=day_end.isoformat(),
        window_start=day_start,
        window_end=day_end,
        upload_job_id=str(inf_results.get("job_id")) if inf_results else None,
        _inf_results=inf_results,
    )

    queue_sidebar_selection(tenant=tenant_id, greenhouse=greenhouse_id)


def apply_pending_jump_window() -> bool:
    """Apply a queued jump's WINDOW after session init, before the timeline widgets.

    Sets the applied time range + anomaly highlights and syncs the pickers so
    the timeline controls render the jump window. The checkbox/column selection
    is deferred to apply_pending_jump_columns, which runs after the window data
    (and thus the available sensor columns) is loaded.
    """
    pending = st.session_state.pop(_PENDING_JUMP_KEY, None)
    if not pending:
        return False

    st.session_state["time_start"] = pending["time_start"]
    st.session_state["time_end"] = pending["time_end"]
    st.session_state["date_range_mode"] = "custom"
    st.session_state["_last_apply_source"] = "custom"
    st.session_state["monitoring_anomaly_highlights"] = pending["highlights"]
    _sync_monitor_timeline_pickers(pending["time_start"], pending["time_end"])

    st.session_state[_PENDING_JUMP_COLUMNS_KEY] = {
        "highlight_columns": pending.get("highlight_columns") or [],
        "highlights": pending["highlights"],
    }
    return True


def apply_pending_jump_columns(all_columns: list[str]) -> None:
    """Apply a queued jump's channel selection once the sensor columns are known.

    Must run before the Monitoring checkboxes render so the `cb_<col>` keys are
    seeded. No-op when there is no pending jump.
    """
    stashed = st.session_state.pop(_PENDING_JUMP_COLUMNS_KEY, None)
    if not stashed or not all_columns:
        return

    highlight_cols = stashed.get("highlight_columns") or []
    if highlight_cols:
        apply_sensor_checkbox_selection(highlight_cols, all_columns)
        return
    highlights = stashed.get("highlights") or []
    row = highlights[0] if highlights else {}
    resolved = resolve_highlight_columns(row, all_columns)
    apply_sensor_checkbox_selection(resolved or all_columns[:2], all_columns)


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
