"""Tests for LSTM/statistical anomaly merge on cold-run upload."""

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.api.services.cold_run_upload import _merge_by_timestamp


def _lstm(ts: str, score: float = 0.9) -> UploadHighAnomalyDetail:
    return UploadHighAnomalyDetail(
        timestamp=ts,
        score=score,
        detector="lstm",
        sequence_index=0,
    )


def _stat(ts: str, z_score: float = 5.0) -> UploadHighAnomalyDetail:
    return UploadHighAnomalyDetail(
        timestamp=ts,
        score=min(1.0, z_score / 5.0),
        detector="statistical",
        column="measured_greenhouse_temp",
        z_score=z_score,
        severity="warning",
        method="rolling",
        value=30.0,
    )


def test_merge_sets_agreement_when_timestamps_differ_only_by_format():
    lstm_ts = "2026-06-17T12:00:00+00:00"
    stat_ts = "2026-06-17T12:00:00Z"

    merged = _merge_by_timestamp([_lstm(lstm_ts)], [_stat(stat_ts)])

    assert len(merged) == 1
    assert merged[0].agreement is True
    assert merged[0].detector == "lstm+statistical"


def test_merge_keeps_lstm_only_without_agreement():
    merged = _merge_by_timestamp(
        [_lstm("2026-06-17T12:00:00+00:00")],
        [_stat("2026-06-18T12:00:00+00:00")],
    )

    assert len(merged) == 2
    assert {row.detector for row in merged} == {"lstm", "statistical"}
    assert all(not row.agreement for row in merged)
