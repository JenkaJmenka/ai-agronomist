"""HTML event tables with wrapped text columns for dashboard pages."""

from __future__ import annotations

import html
import textwrap
from typing import Sequence

import pandas as pd

_DEFAULT_WRAP_WIDTH = 72

_TABLE_CSS = """
<style>
.ag-events-table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
.ag-events-table th, .ag-events-table td {
  border: 1px solid rgba(128, 128, 128, 0.35);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}
.ag-events-table th {
  background-color: #31333d;
  color: #fafafa;
  font-weight: 600;
}
.ag-events-table td.wrap {
  white-space: normal;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  line-height: 1.45;
  max-width: 520px;
}
</style>
"""


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
) -> None:
    """Render a dataframe as an HTML table with wrapped text in selected columns."""
    import streamlit as st

    if df.empty:
        return

    wrap_set = set(wrap_columns)
    parts = [_TABLE_CSS, '<div style="overflow-x:auto;"><table class="ag-events-table"><thead><tr>']
    for col in df.columns:
        parts.append(f"<th>{html.escape(str(col))}</th>")
    parts.append("</tr></thead><tbody>")

    for _, row in df.iterrows():
        parts.append("<tr>")
        for col in df.columns:
            raw = row[col]
            if col in wrap_set:
                display = wrap_cell_text(raw, width=wrap_width)
                cell_class = ' class="wrap"'
                body = html.escape(display).replace("\n", "<br>")
            else:
                cell_class = ""
                body = html.escape(str(raw))
            parts.append(f"<td{cell_class}>{body}</td>")
        parts.append("</tr>")

    parts.append("</tbody></table></div>")
    st.markdown("".join(parts), unsafe_allow_html=True)
