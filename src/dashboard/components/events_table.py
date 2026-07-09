"""Event tables with wrapped text columns for dashboard pages."""

from __future__ import annotations

import textwrap
from typing import Sequence

import pandas as pd

_DEFAULT_WRAP_WIDTH = 72


def wrap_cell_text(value: object, width: int = _DEFAULT_WRAP_WIDTH) -> str:
    """Split long cell text into multiple lines at word boundaries."""
    if value is None:
        return "—"
    text = str(value).strip()
    if not text or text == "—":
        return text or "—"
    return "\n".join(textwrap.wrap(text, width=width, break_long_words=False))


def render_events_table(
    df: pd.DataFrame,
    *,
    wrap_columns: Sequence[str] = ("Reasoning",),
    wrap_width: int = _DEFAULT_WRAP_WIDTH,
    table_key: str = "events",
    export_file_name: str | None = None,
    title: str = "",
) -> None:
    """Render a dataframe as a sticky table with wrapped text in selected columns."""
    from src.dashboard.components.data_table import render

    if df.empty:
        return

    render(
        df,
        title=title,
        table_key=table_key,
        wrap_columns=wrap_columns,
        wrap_width=wrap_width,
        max_height=400,
        export_file_name=export_file_name or f"{table_key}.csv",
    )
