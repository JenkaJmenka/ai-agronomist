"""Scrollable tables with sticky headers and Streamlit action buttons."""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from typing import Any

import streamlit as st

_TABLE_ROWS_PREFIX = "_sticky_table_rows_"
_DEFAULT_MAX_HEIGHT = 400


def _scroll_height(row_count: int, max_height: int) -> int:
    if row_count <= 6:
        return min(max_height, max(160, 48 + row_count * 36))
    return max_height


def _table_css(table_key: str) -> str:
    safe_key = html.escape(table_key.replace("_", "-"))
    return f"""
<style>
.ag-sticky-block-{safe_key} {{ margin-bottom: 1rem; }}
.ag-sticky-title-{safe_key} {{
  font-size: 1.05rem;
  font-weight: 600;
  margin: 0 0 6px 0;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ag-sticky-table-{safe_key}) {{
  font-size: 0.82rem;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ag-sticky-table-{safe_key})
  > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"]:first-of-type {{
  background-color: #31333d;
  color: #fafafa;
  padding: 8px 0 6px 0;
  border-bottom: 1px solid rgba(128, 128, 128, 0.45);
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ag-sticky-table-{safe_key})
  div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stHorizontalBlock"] {{
  border-bottom: 1px solid rgba(128, 128, 128, 0.18);
  padding: 2px 0;
  align-items: center;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ag-sticky-table-{safe_key}) button {{
  min-height: 1.55rem !important;
  padding: 0.1rem 0.5rem !important;
  font-size: 0.72rem !important;
  line-height: 1.2 !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ag-sticky-table-{safe_key})
  [data-testid="column"] [data-testid="stHorizontalBlock"] {{
  gap: 0.25rem;
  align-items: center;
  flex-wrap: nowrap;
}}
.ag-th-{safe_key} {{
  color: #fafafa;
  font-weight: 600;
  white-space: nowrap;
  display: inline-block;
}}
</style>
"""


def store_table_rows(table_key: str, rows: list[dict[str, Any]]) -> None:
    st.session_state[f"{_TABLE_ROWS_PREFIX}{table_key}"] = rows


def get_table_row(table_key: str, row_index: int) -> dict[str, Any] | None:
    rows = st.session_state.get(f"{_TABLE_ROWS_PREFIX}{table_key}")
    if not isinstance(rows, list) or row_index < 0 or row_index >= len(rows):
        return None
    row = rows[row_index]
    return row if isinstance(row, dict) else None


def render_compact_buttons(
    parent_column: Any,
    table_key: str,
    row_index: int,
    buttons: Sequence[dict[str, Any]],
) -> bool:
    """Render a horizontal row of compact secondary buttons; returns True if any clicked."""
    if not buttons:
        parent_column.markdown("—")
        return False

    clicked = False
    btn_cols = parent_column.columns(len(buttons), gap="small")
    for col, spec in zip(btn_cols, buttons):
        label = str(spec["label"])
        suffix = str(spec["suffix"])
        if col.button(
            label,
            key=f"{table_key}_{suffix}_{row_index}",
            type="secondary",
            help=spec.get("help", ""),
            use_container_width=True,
        ):
            handler = spec.get("on_click")
            if callable(handler):
                handler()
            clicked = True
    return clicked


def _render_header_row(headers: Sequence[str], widths: list[float], table_key: str) -> None:
    safe_key = table_key.replace("_", "-")
    header = st.columns(widths)
    for index, label in enumerate(headers):
        header[index].markdown(
            f"<span class='ag-th-{safe_key}'>{html.escape(str(label))}</span>",
            unsafe_allow_html=True,
        )


def render_streamlit_sticky_table(
    title: str,
    headers: Sequence[str],
    column_widths: Sequence[float],
    table_key: str,
    rows: Sequence[Any],
    render_row: Callable[[int, Any, list[Any]], None],
    *,
    row_records: list[dict[str, Any]] | None = None,
    max_height: int = _DEFAULT_MAX_HEIGHT,
    empty_message: str = "No rows to display.",
) -> None:
    """Header fixed above a scrollable body; column titles stay visible while rows scroll."""
    safe_key = table_key.replace("_", "-")
    if row_records is not None:
        store_table_rows(table_key, row_records)

    st.markdown(f'<div class="ag-sticky-block-{safe_key}">', unsafe_allow_html=True)
    if title:
        st.markdown(
            f'<p class="ag-sticky-title-{safe_key}">{html.escape(title)}</p>',
            unsafe_allow_html=True,
        )

    st.markdown(_table_css(table_key), unsafe_allow_html=True)
    heights = _scroll_height(len(rows), max_height) if rows else min(max_height, 160)
    widths = list(column_widths)
    body_height = max(96, heights - 44)

    with st.container(border=True):
        st.markdown(
            f'<span class="ag-sticky-table-{safe_key}" style="display:none"></span>',
            unsafe_allow_html=True,
        )
        _render_header_row(headers, widths, table_key)

        if not rows:
            st.info(empty_message)
        else:
            with st.container(height=body_height):
                for row_index, row_data in enumerate(rows):
                    cells = st.columns(widths)
                    render_row(row_index, row_data, cells)

    st.markdown("</div>", unsafe_allow_html=True)
