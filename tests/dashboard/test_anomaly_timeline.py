"""Tests for anomaly score timeline chart."""

import pandas as pd

from src.dashboard.components.charts import plot_anomaly_timeline


def test_plot_anomaly_timeline_empty_df_shows_message():
    fig = plot_anomaly_timeline(pd.DataFrame(), threshold=0.8)
    assert fig.layout.annotations
    assert "No scored readings" in fig.layout.annotations[0].text
    assert fig.layout.xaxis.title.text == "Time"


def test_plot_anomaly_timeline_anomalies_only_empty_shows_threshold_message():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"]),
            "anomaly_score": [0.2],
        }
    )
    fig = plot_anomaly_timeline(df, threshold=0.8, anomalies_only=True)
    assert "no anomalies in range" in (fig.layout.title.text or "").lower()
    assert fig.layout.annotations


def test_plot_anomaly_timeline_shows_all_scores_when_not_anomalies_only():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-06-17T12:00:00+00:00", "2026-06-17T13:00:00+00:00"]
            ),
            "anomaly_score": [0.2, 0.9],
        }
    )
    fig = plot_anomaly_timeline(df, threshold=0.8, anomalies_only=False)
    assert len(fig.data) >= 1
