"""Tests for unified dashboard anomaly events."""

import pandas as pd

from src.dashboard.services.anomaly_events import (
    build_unified_anomaly_events,
    count_anomalies_at_threshold,
    count_events_for_column,
    flagged_anomaly_mask,
)


def test_flagged_anomaly_mask_sparse_cold_run_scores():
    scores = pd.Series([None, None, None, 0.95, None, None, None, None, None, None])
    mask = flagged_anomaly_mask(scores, threshold=0.8)
    assert mask.sum() == 1


def test_flagged_anomaly_mask_sparse_respects_higher_threshold():
    scores = pd.Series([None, None, None, 0.95, None])
    assert flagged_anomaly_mask(scores, threshold=0.99).sum() == 0
    assert flagged_anomaly_mask(scores, threshold=0.8).sum() == 1


def test_count_anomalies_at_threshold():
    scores = pd.Series([0.1, 0.85, 0.95, None])
    assert count_anomalies_at_threshold(scores, 0.8) == 2
    assert count_anomalies_at_threshold(scores, 0.9) == 1


def test_flagged_anomaly_mask_dense_pipeline_scores():
    scores = pd.Series([0.1, 0.5, 0.9, 0.2])
    mask = flagged_anomaly_mask(scores, threshold=0.8)
    assert mask.tolist() == [False, False, True, False]


def test_build_unified_anomaly_events_from_persisted_scores():
    readings = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-06-01 10:00:00", "2026-06-01 11:00:00"], utc=True
            ),
            "temperature": [20.0, 50.0],
            "humidity": [60.0, 61.0],
            "anomaly_score": [None, 0.91],
            "anomaly_columns": [None, ["measured_greenhouse_temp"]],
        }
    )
    events = build_unified_anomaly_events(
        readings,
        pd.DataFrame(),
        start=pd.Timestamp("2026-06-01", tz="UTC"),
        end=pd.Timestamp("2026-06-02", tz="UTC"),
        threshold=0.8,
    )
    assert len(events) == 1
    assert events.iloc[0]["source"] == "reading"
    assert count_events_for_column(events, "measured_greenhouse_temp") == 1
    assert count_events_for_column(events, "outdoor_temp") == 1


def test_build_unified_events_populates_fault_and_sensors_without_metadata():
    readings = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-01 11:00:00"], utc=True),
            "measured_greenhouse_temp": [52.0],
            "outdoor_temp": [18.0],
            "anomaly_score": [1.0],
            "anomaly_detectors": ["statistical"],
        }
    )
    events = build_unified_anomaly_events(
        readings,
        pd.DataFrame(),
        start=pd.Timestamp("2026-06-01", tz="UTC"),
        end=pd.Timestamp("2026-06-02", tz="UTC"),
        threshold=0.8,
    )
    assert len(events) == 1
    assert events.iloc[0]["fault_type"] == "spike_fault"
    assert events.iloc[0]["affected_sensors"]
    assert events.iloc[0]["reasoning"] != "Anomaly score exceeded threshold — no detailed diagnosis available yet"
