"""Sticky dataframe tables matching the anomaly-detection table style."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from src.dashboard.components.events_table import wrap_cell_text
from src.dashboard.components.sticky_table import render_streamlit_sticky_table

_WIDE_COLUMN_HINTS = frozenset(
    {
        "reasoning",
        "description",
        "root cause",
        "root_cause",
        "hint_summary",
        "pattern_explanation",
    }
)


def _column_widths(headers: Sequence[str]) -> list[float]:
    widths: list[float] = []
    for header in headers:
        normalized = str(header).strip().lower()
        if normalized in _WIDE_COLUMN_HINTS:
            widths.append(2.5)
        elif normalized in {"timestamp", "time", "detected"}:
            widths.append(1.4)
        elif normalized in {"sensors", "sensor type", "source"}:
            widths.append(1.2)
        elif normalized in {"fault type", "pattern", "severity", "method", "detector"}:
            widths.append(1.0)
        else:
            widths.append(0.9)
    return widths


def render(
    df: pd.DataFrame,
    *,
    title: str = "",
    table_key: str,
    wrap_columns: Sequence[str] = (),
    wrap_width: int = 72,
    max_height: int = 320,
    empty_message: str = "No rows to display.",
    show_export: bool = True,
    export_file_name: str | None = None,
    export_label: str = "Export CSV",
) -> None:
    """Render a dataframe in the shared sticky table layout."""
    headers = [str(col) for col in df.columns]
    widths = _column_widths(headers)
    wrap_set = {str(col) for col in wrap_columns}
    row_list = [row for _, row in df.iterrows()]

    def _render_row(row_index: int, row_data: pd.Series, cells: list[Any]) -> None:
        for col_index, column in enumerate(df.columns):
            raw = row_data[column]
            if pd.isna(raw):
                display = "—"
            elif column in wrap_set:
                display = wrap_cell_text(raw, width=wrap_width)
            else:
                display = str(raw)
            cells[col_index].markdown(display)

    render_streamlit_sticky_table(
        title,
        headers,
        widths,
        table_key,
        row_list,
        _render_row,
        max_height=max_height,
        empty_message=empty_message,
        export_df=df.copy() if show_export and not df.empty else None,
        export_file_name=export_file_name or f"{table_key}.csv",
        export_label=export_label,
    )
