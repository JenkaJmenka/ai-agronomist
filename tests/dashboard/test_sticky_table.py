"""Tests for sticky table helpers."""

from src.dashboard.components.sticky_table import get_table_row, store_table_rows


def test_store_and_get_table_rows():
    rows = [{"timestamp": "2026-06-17T12:00:00+00:00", "detector": "lstm"}]
    store_table_rows("test_table", rows)
    assert get_table_row("test_table", 0) == rows[0]
    assert get_table_row("test_table", 1) is None
