"""Tests for anomaly table styling helpers."""

import pandas as pd

from src.dashboard.components.anomaly_table import (
    _cell_style,
    build_html_table,
)


def test_cell_style_strong_attribution():
    enrichment = {
        "flags": {"temperature": True},
        "hints": {"temperature": "Strong temperature attribution"},
        "attribution": {"temperature": 0.3},
    }
    style = _cell_style("temperature", enrichment)
    assert "rgba(251, 146, 60" in style


def test_cell_style_statistical_red():
    enrichment = {
        "flags": {"temperature": True},
        "hints": {"temperature": "temperature outside normal range"},
        "attribution": {},
    }
    style = _cell_style("temperature", enrichment)
    assert "rgba(239, 68, 68" in style


def test_cell_style_no_flag():
    enrichment = {"flags": {"temperature": False}, "hints": {}}
    assert "ef4444" not in _cell_style("temperature", enrichment)


def test_build_html_table_includes_red_background():
    display = pd.DataFrame(
        [
            {
                "timestamp": "2026-06-08 12:00:00",
                "anomaly_score": "0.950",
                "pattern_type": "",
                "affected_sensors": "temperature",
                "hint_summary": "temperature outside normal range",
                "temperature": "45.0",
                "humidity": "65.0",
                "co2": "400.0",
                "light": "500.0",
            }
        ]
    )
    enrichments = [
        {
            "flags": {"temperature": True, "humidity": False, "co2": False, "light": False},
            "hints": {"temperature": "temperature outside normal range"},
            "attribution": {},
            "affected_sensors": ["temperature"],
        }
    ]
    html_out = build_html_table(display, enrichments)
    assert "rgba(239, 68, 68" in html_out
    # ℹ info column has been replaced by hover tooltip — verify tooltip is present instead
    assert "ag-tip" in html_out
    assert "temperature outside normal range" in html_out
    assert "45.0" in html_out
