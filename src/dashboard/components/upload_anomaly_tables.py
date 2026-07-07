"""Upload page helpers: anomaly tables and Monitoring navigation."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src.dashboard.components.monitoring_jump import (
    _DISPLAY_COLUMN_LABELS,
    anomaly_display_columns,
    format_anomaly_cell,
    queue_monitoring_jump,
)
from src.dashboard.constants import MONITORING_PAGE
from src.dashboard.components.sticky_table import (
    render_compact_buttons,
    render_streamlit_sticky_table,
)

_TABLE_MAX_HEIGHT = 400
_ACTION_WIDTH = 1.0
_MONITOR_WIDTH = 1.4

_COLUMN_WEIGHTS = {
    "timestamp": 1.1,
    "detector": 1.3,
    "score": 1.0,
    "column": 1.5,
    "severity": 1.1,
    "z_score": 1.0,
    "method": 1.3,
    "value": 1.0,
    "agreement": 1.0,
}


def _column_weights(columns: list[str]) -> list[float]:
    return (
        [_COLUMN_WEIGHTS.get(col, 1.0) for col in columns]
        + [_ACTION_WIDTH, _MONITOR_WIDTH]
    )


def _monitor_jump(
    row: dict[str, Any],
    tenant_id: str,
    greenhouse_id: str,
    *,
    source_df: pd.DataFrame | None = None,
    available_columns: list[str] | None = None,
) -> None:
    jump_row = {k: v for k, v in row.items() if not k.startswith("_")}
    try:
        queue_monitoring_jump(
            jump_row,
            tenant_id,
            greenhouse_id,
            available_columns=available_columns,
            source_df=source_df,
        )
        st.switch_page(MONITORING_PAGE)
    except ValueError as exc:
        st.error(str(exc))


_EMPTY_UPLOAD_COLUMNS = [
    "timestamp",
    "detector",
    "score",
    "column",
    "severity",
    "z_score",
    "method",
    "value",
]


def render_anomaly_table_with_monitoring_jump(
    rows: list[dict[str, Any]],
    tenant_id: str,
    greenhouse_id: str,
    *,
    table_key: str,
    title: str,
    show_agreement: bool = False,
    empty_message: str = "No anomalies in this section.",
    source_df: pd.DataFrame | None = None,
    available_columns: list[str] | None = None,
) -> None:
    """Sticky table with Action + Monitoring columns (empty state keeps layout)."""
    columns = anomaly_display_columns(rows) if rows else list(_EMPTY_UPLOAD_COLUMNS)
    if show_agreement and "agreement" not in columns:
        columns = [*columns, "agreement"]
    if not columns:
        columns = list(_EMPTY_UPLOAD_COLUMNS)

    headers = [_DISPLAY_COLUMN_LABELS.get(col, col) for col in columns]
    headers.extend(["Action", "Monitoring"])
    widths = _column_weights(columns)

    if not rows:
        def _empty_row(_index: int, _row: Any, _cells: list[Any]) -> None:
            return

        render_streamlit_sticky_table(
            title,
            headers,
            widths,
            table_key,
            [],
            _empty_row,
            empty_message=empty_message,
            max_height=_TABLE_MAX_HEIGHT,
        )
        return

    row_records = [
        dict(row, _tenant_id=tenant_id, _greenhouse_id=greenhouse_id) for row in rows
    ]

    def _render_row(row_index: int, row: dict[str, Any], cells: list[Any]) -> None:
        for col_index, column in enumerate(columns):
            if column == "agreement":
                cells[col_index].markdown(
                    "✓" if row.get("agreement") else "—"
                )
            else:
                cells[col_index].markdown(
                    format_anomaly_cell(column, row.get(column))
                )

        cells[-2].markdown("—")

        def _on_monitor() -> None:
            _monitor_jump(
                row,
                tenant_id,
                greenhouse_id,
                source_df=source_df,
                available_columns=available_columns,
            )

        render_compact_buttons(
            cells[-1],
            table_key,
            row_index,
            [
                {
                    "label": "Monitor",
                    "suffix": "monitor",
                    "help": "Open Monitoring for this anomaly's date and sensors",
                    "on_click": _on_monitor,
                }
            ],
        )

    render_streamlit_sticky_table(
        title,
        headers,
        widths,
        table_key,
        rows,
        _render_row,
        row_records=row_records,
        max_height=_TABLE_MAX_HEIGHT,
    )
