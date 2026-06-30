"""Tests for dashboard diagnosis enricher."""

import pandas as pd

from src.dashboard.services.diagnosis_enricher import (
    columns_refer_to_same_sensor,
    enrich_dataframe,
    enrich_row,
    infer_affected_sensors_from_row,
)


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
    assert result["fault_type"] == "spike_fault"
    assert result["reasoning"]


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
    assert result["fault_type"] == "spike_fault"
    assert "temperature" in result["reasoning"].lower()


def test_enrich_row_uses_incident_fault_type_and_root_cause():
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
        "id": "inc-2",
        "fault_type": "calibration_drift",
        "root_cause": "heating_controller_drift",
        "root_cause_details": {
            "pattern_type": "drift",
            "pattern_explanation": "Gradual upward drift in heating loop",
        },
    }
    result = enrich_row(row, incident)
    assert result["fault_type"] == "calibration_drift"
    assert "heating_controller_drift" in result["reasoning"]


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
                "fault_type": "calibration_drift",
            },
        }
    ]
    enriched = enrich_dataframe(df, incidents)
    assert "pattern_type" in enriched.columns
    assert "hint_summary" in enriched.columns
    assert "fault_type" in enriched.columns
    assert "reasoning" in enriched.columns
    assert enriched.iloc[0]["fault_type"] == "calibration_drift"
    assert "_enrichment" in enriched.columns


def test_enrich_row_resolves_raw_column_aliases():
    row = pd.Series(
        {
            "timestamp": pd.Timestamp("2026-06-08 12:00:00"),
            "measured_greenhouse_temp": 50.0,
            "measured_relative_humidity": 65.0,
            "co2": 400.0,
            "illuminance": 500.0,
            "anomaly_columns": ["measured_greenhouse_temp"],
            "anomaly_detectors": ["statistical"],
        }
    )
    result = enrich_row(row, None)
    assert result["pattern_type"] == "statistical"
    assert "measured_greenhouse_temp" in result["affected_sensors"]
    assert result["flags"]["temperature"] is True
    assert result["fault_type"] == "spike_fault"
    assert "z-score" in result["reasoning"].lower()


def test_infer_affected_sensors_without_metadata():
    row = pd.Series(
        {
            "timestamp": pd.Timestamp("2026-06-08 12:00:00"),
            "measured_greenhouse_temp": 52.0,
            "outdoor_temp": 18.0,
        }
    )
    affected = infer_affected_sensors_from_row(row)
    assert "measured_greenhouse_temp" in affected


def test_columns_refer_to_same_sensor_temp_aliases():
    assert columns_refer_to_same_sensor("measured_greenhouse_temp", "outdoor_temp")
    assert not columns_refer_to_same_sensor("humidity_deficit", "outdoor_temp")


def test_enrich_dataframe_fills_canonical_sensor_columns():
    df = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-06-08 12:00:00"),
                "anomaly_score": 0.9,
                "measured_greenhouse_temp": 45.0,
                "measured_relative_humidity": 65.0,
            }
        ]
    )
    enriched = enrich_dataframe(df, [])
    assert enriched.iloc[0]["temperature"] == 45.0
    assert enriched.iloc[0]["humidity"] == 65.0
