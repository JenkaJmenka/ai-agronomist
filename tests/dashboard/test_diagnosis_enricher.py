"""Tests for dashboard diagnosis enricher."""

import pandas as pd

from src.dashboard.services.diagnosis_enricher import enrich_dataframe, enrich_row


def test_enrich_row_with_attribution():
    row = pd.Series(
        {
            "timestamp": pd.Timestamp("2026-06-08 12:00:00"),
            "temperature": 35.0,
            "humidity": 65.0,
            "co2": 400.0,
            "light": 500.0,
        }
    )
    incident = {
        "id": "inc-1",
        "root_cause_details": {
            "attribution": {"temperature": 0.3, "humidity": 0.05},
            "affected_sensors": ["temperature"],
            "pattern_type": "spike",
        },
    }
    result = enrich_row(row, incident)
    assert result["flags"]["temperature"] is True
    assert "temperature" in result["hint_summary"]
    assert result["pattern_type"] == "spike"


def test_enrich_row_zscore_fallback():
    row = pd.Series(
        {
            "timestamp": pd.Timestamp("2026-06-08 12:00:00"),
            "temperature": 50.0,
            "humidity": 65.0,
            "co2": 400.0,
            "light": 500.0,
        }
    )
    result = enrich_row(row, None)
    assert result["flags"]["temperature"] is True


def test_enrich_dataframe_adds_columns():
    df = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-06-08 12:00:00"),
                "anomaly_score": 0.9,
                "temperature": 45.0,
                "humidity": 65.0,
                "co2": 400.0,
                "light": 500.0,
            }
        ]
    )
    incidents = [
        {
            "id": "inc-1",
            "detected_at": pd.Timestamp("2026-06-08 12:05:00"),
            "root_cause_details": {
                "attribution": {"temperature": 0.4},
                "affected_sensors": ["temperature"],
                "pattern_type": "drift",
            },
        }
    ]
    enriched = enrich_dataframe(df, incidents)
    assert "pattern_type" in enriched.columns
    assert "hint_summary" in enriched.columns
    assert "_enrichment" in enriched.columns
