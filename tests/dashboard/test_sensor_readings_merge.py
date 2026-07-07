"""Tests for sensor reading / anomaly score merge logic."""

import pandas as pd

from src.dashboard.components.charts import merge_anomaly_scores_into_readings


def test_merge_deduplicates_duplicate_raw_rows():
    raw = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 00:00:00"] * 4),
            "measured_greenhouse_temp": [17.0] * 4,
        }
    )
    scores = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 00:00:00"] * 2),
            "anomaly_score": [1.0, None],
        }
    )

    merged = merge_anomaly_scores_into_readings(raw, scores, "tenant_1", "gh_A")
    assert len(merged) == 1
    assert merged.iloc[0]["anomaly_score"] == 1.0


def test_merge_does_not_cross_tenant_scores():
    raw = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 00:00:00"]),
            "temperature": [20.0],
        }
    )
    scores = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 00:00:00"]),
            "tenant_id": ["tenant_2"],
            "greenhouse_id": ["gh_B"],
            "anomaly_score": [0.99],
        }
    )

    merged = merge_anomaly_scores_into_readings(raw, scores, "tenant_1", "gh_A")
    assert pd.isna(merged.iloc[0]["anomaly_score"])


def test_merge_aligns_naive_readings_with_utc_upload_scores():
    raw = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 12:00:00"]),
            "temperature": [20.0],
        }
    )
    scores = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"]),
            "anomaly_score": [0.91],
        }
    )

    merged = merge_anomaly_scores_into_readings(raw, scores, "tenant_1", "gh_A")
    assert float(merged.iloc[0]["anomaly_score"]) == 0.91
