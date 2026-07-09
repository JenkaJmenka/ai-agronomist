"""CSV export helpers for dashboard tables."""

from __future__ import annotations

import pandas as pd
import streamlit as st


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Serialize a dataframe to UTF-8 CSV bytes."""
    return df.to_csv(index=False).encode("utf-8")


def render_export_button(
    df: pd.DataFrame,
    *,
    file_name: str,
    label: str = "Export CSV",
    key: str | None = None,
    caption: str | None = None,
) -> None:
    """Render a download button for tabular data."""
    if df.empty:
        return

    dl_col, cap_col = st.columns([1, 4])
    dl_col.download_button(
        label,
        data=df_to_csv_bytes(df),
        file_name=file_name,
        mime="text/csv",
        key=key,
    )
    if caption:
        cap_col.caption(caption)
    else:
        cap_col.caption(f"{len(df):,} row{'s' if len(df) != 1 else ''}")
