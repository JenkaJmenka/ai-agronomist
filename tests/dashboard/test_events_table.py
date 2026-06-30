"""Tests for wrapped dashboard event tables."""

from src.dashboard.components.events_table import wrap_cell_text


def test_wrap_cell_text_splits_long_reasoning():
    text = (
        "Statistical cold-run detector flagged measured_greenhouse_temp with "
        "z-score 8.2; value 52.1°C exceeds rolling baseline."
    )
    wrapped = wrap_cell_text(text, width=40)
    assert "\n" in wrapped
    assert all(len(line) <= 40 for line in wrapped.split("\n"))


def test_wrap_cell_text_preserves_short_and_placeholder():
    assert wrap_cell_text("Short note") == "Short note"
    assert wrap_cell_text("—") == "—"
    assert wrap_cell_text(None) == "—"
