"""Tests for shared sticky data tables."""

from src.dashboard.components.data_table import _column_widths


def test_column_widths_widen_reasoning_columns():
    widths = _column_widths(["Time", "Reasoning", "Score"])
    assert widths[1] > widths[0]
    assert widths[1] > widths[2]
