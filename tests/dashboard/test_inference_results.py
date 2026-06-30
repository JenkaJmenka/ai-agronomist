"""Unit tests for cold-run LSTM chart helpers."""

import pandas as pd

from src.dashboard.components.inference_results import (
    _looks_like_upload_score_frame,
    build_sensor_dataframe_from_upload,
    plot_upload_lstm_chart,
    resolve_upload_results,
)


def _sample_results() -> dict:
    return {
        "sequences_processed": 5,
        "anomaly_scores": [0.1, 0.2, 0.3, 0.4, 0.5],
        "sequence_timestamps": [
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T01:00:00+00:00",
            "2026-01-01T02:00:00+00:00",
            "2026-01-01T03:00:00+00:00",
            "2026-01-01T04:00:00+00:00",
        ],
        "threshold_used": 0.8,
        "threshold_method": "pot",
    }


def test_resolve_upload_results_unwraps_session_wrapper():
    inner = _sample_results()
    wrapper = {
        "results": inner,
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_A",
        "filename": "test.csv",
    }
    assert resolve_upload_results(wrapper) == inner


def test_resolve_upload_results_accepts_inner_payload():
    inner = _sample_results()
    assert resolve_upload_results(inner) == inner


def test_resolve_upload_results_none_when_missing():
    assert resolve_upload_results(None) is None


def test_plot_upload_lstm_chart_returns_trace_for_scores():
    fig = plot_upload_lstm_chart(_sample_results())
    assert fig is not None
    assert len(fig.data) == 1
    assert fig.layout.xaxis.title.text == "Time"
    assert fig.layout.title.text is None


def test_plot_upload_lstm_chart_none_when_scores_missing():
    assert plot_upload_lstm_chart({"sequences_processed": 10}) is None


def test_plot_upload_lstm_chart_uses_sequence_index_for_unknown_timestamps():
    results = {
        "anomaly_scores": [0.2, 0.3],
        "sequence_timestamps": ["unknown", "unknown"],
        "threshold_used": 0.8,
    }
    fig = plot_upload_lstm_chart(results)
    assert fig is not None
    assert fig.layout.xaxis.title.text == "Sequence index"


def test_build_sensor_dataframe_from_upload_uses_sequence_timestamps():
    df = build_sensor_dataframe_from_upload(_sample_results())
    assert len(df) == 5
    assert "timestamp" in df.columns
    assert "anomaly_score" in df.columns
    assert float(df.iloc[0]["anomaly_score"]) == 0.1


def test_build_sensor_dataframe_from_upload_empty_without_timestamps():
    assert build_sensor_dataframe_from_upload({"anomaly_scores": [0.1]}).empty


def test_looks_like_upload_score_frame():
    upload_df = build_sensor_dataframe_from_upload(_sample_results())
    assert _looks_like_upload_score_frame(upload_df)
    db_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"]),
            "anomaly_score": [0.9],
            "temperature": [22.0],
        }
    )
    assert not _looks_like_upload_score_frame(db_df)
