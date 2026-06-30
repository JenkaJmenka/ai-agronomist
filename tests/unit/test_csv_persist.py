"""Unit tests for CSV persistence helpers."""

from src.api.services.cold_run_upload import build_anomaly_scores_by_timestamp
from src.api.services.csv_persist import lookup_anomaly_score, normalize_timestamp_key
from src.api.schemas.sensor_data import UploadHighAnomalyDetail


def test_normalize_timestamp_key_handles_z_suffix():
    assert normalize_timestamp_key("2026-06-09T11:00:00Z") == "2026-06-09T11:00:00+00:00"


def test_build_anomaly_scores_merges_lstm_and_statistical():
    scores = build_anomaly_scores_by_timestamp(
        high_anomalies=[
            UploadHighAnomalyDetail(
                score=0.95,
                timestamp="2026-06-09T11:00:00Z",
                detector="lstm",
            ),
            UploadHighAnomalyDetail(
                score=0.95,
                timestamp="2026-06-09T11:05:00Z",
                detector="statistical",
            ),
        ],
    )
    assert scores["2026-06-09T11:00:00+00:00"] == 0.95
    assert scores["2026-06-09T11:05:00+00:00"] == 0.95


def test_build_anomaly_scores_ignores_sub_threshold_sequences():
    scores = build_anomaly_scores_by_timestamp([])
    assert scores == {}


def test_lookup_anomaly_score_matches_normalized_timestamp():
    score_map = {"2026-06-09T11:00:00+00:00": 0.88}
    assert lookup_anomaly_score("2026-06-09T11:00:00Z", score_map) == 0.88
