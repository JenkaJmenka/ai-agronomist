"""Tests for anomaly page data helpers."""

import pandas as pd

from src.dashboard.components.charts import downsample_for_chart
from src.dashboard.services.anomalies_page_data import (
    filter_anomalies,
    trim_cold_run_rows,
)


def test_downsample_for_chart_reduces_large_frames():
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=5000, freq="min"),
            "anomaly_score": [0.1] * 5000,
        }
    )
    sampled = downsample_for_chart(df, max_points=500)
    assert len(sampled) <= 500


def test_filter_anomalies_respects_threshold():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"] * 2),
            "anomaly_score": [0.5, 0.95],
        }
    )
    flagged = filter_anomalies(df, 0.8)
    assert len(flagged) == 1


def test_clear_anomaly_page_cache_import():
    from src.dashboard.services.anomalies_page_data import clear_anomaly_page_cache

    clear_anomaly_page_cache()


def test_rows_in_window_filters_by_timestamp():
    from src.dashboard.services.anomalies_page_data import rows_in_window

    rows = [
        {"timestamp": "2026-06-10T12:00:00+00:00", "score": 0.9},
        {"timestamp": "2026-06-20T12:00:00+00:00", "score": 0.8},
    ]
    start = pd.Timestamp("2026-06-15T00:00:00+00:00")
    end = pd.Timestamp("2026-06-25T23:59:59+00:00")
    kept = rows_in_window(rows, start, end)
    assert len(kept) == 1
    assert kept[0]["score"] == 0.8


def test_resolve_window_dataframe_merges_upload_scores_when_db_lacks_them(monkeypatch):
    from src.dashboard.services import anomalies_page_data as mod

    mod.resolve_window_dataframe.clear()
    db_frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"]),
            "temperature": [22.0],
        }
    )
    upload_frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"]),
            "anomaly_score": [0.95],
        }
    )
    inf_results = {
        "sequences_processed": 1,
        "anomaly_scores": [0.95],
        "sequence_timestamps": ["2026-06-17T12:00:00+00:00"],
        "data_persisted": True,
    }

    monkeypatch.setattr(mod, "load_readings_window", lambda *a, **k: db_frame)
    monkeypatch.setattr(
        mod,
        "_upload_score_frame_for_window",
        lambda *a, **k: upload_frame,
    )

    window_start = pd.Timestamp("2026-06-16T00:00:00+00:00")
    window_end = pd.Timestamp("2026-06-18T00:00:00+00:00")
    df, using_fallback = mod.resolve_window_dataframe(
        tenant_id="tenant_1",
        greenhouse_id="gh_A",
        start_iso=window_start.isoformat(),
        end_iso=window_end.isoformat(),
        window_start=window_start,
        window_end=window_end,
        upload_job_id=None,
        _inf_results=inf_results,
    )

    assert not using_fallback
    assert len(df) == 1
    assert float(df.iloc[0]["anomaly_score"]) == 0.95


def test_resolve_window_dataframe_uses_upload_when_db_empty(monkeypatch):
    from src.dashboard.services import anomalies_page_data as mod

    mod.resolve_window_dataframe.clear()
    upload_frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"]),
            "anomaly_score": [0.88],
        }
    )
    inf_results = {"sequences_processed": 1, "anomaly_scores": [0.88]}

    monkeypatch.setattr(
        mod, "load_readings_window", lambda *a, **k: pd.DataFrame()
    )
    monkeypatch.setattr(
        mod,
        "_upload_score_frame_for_window",
        lambda *a, **k: upload_frame,
    )

    window_start = pd.Timestamp("2026-06-16T00:00:00+00:00")
    window_end = pd.Timestamp("2026-06-18T00:00:00+00:00")
    df, using_fallback = mod.resolve_window_dataframe(
        tenant_id="tenant_1",
        greenhouse_id="gh_A",
        start_iso=window_start.isoformat(),
        end_iso=window_end.isoformat(),
        window_start=window_start,
        window_end=window_end,
        upload_job_id=None,
        _inf_results=inf_results,
    )

    assert using_fallback
    assert len(df) == 1


def test_resolve_data_bounds_unions_db_and_upload(monkeypatch):
    from src.dashboard.services import anomalies_page_data as mod

    monkeypatch.setattr(
        mod,
        "load_timestamp_bounds",
        lambda *a, **k: ("2026-06-10T00:00:00+00:00", "2026-06-15T00:00:00+00:00"),
    )
    inf_results = {
        "anomaly_scores": [0.5, 0.6],
        "sequence_timestamps": [
            "2026-06-14T00:00:00+00:00",
            "2026-06-20T00:00:00+00:00",
        ],
        "data_persisted": True,
    }

    bounds = mod.resolve_data_bounds("tenant_1", "gh_A", inf_results)
    assert bounds is not None
    start, end = bounds
    # Bounds are normalized to tz-naive so the page's tz-naive date pickers and
    # filters compare against them without raising.
    assert start == pd.Timestamp("2026-06-10T00:00:00")
    assert end == pd.Timestamp("2026-06-20T00:00:00")
    assert start.tzinfo is None and end.tzinfo is None


def test_resolve_data_bounds_unions_naive_db_and_aware_upload(monkeypatch):
    """DB bounds come back tz-naive while upload bounds are tz-aware (UTC).

    Comparing the two directly raised TypeError and blanked the whole page;
    resolve_data_bounds must strip tz first and still union correctly.
    """
    from src.dashboard.services import anomalies_page_data as mod

    monkeypatch.setattr(
        mod,
        "load_timestamp_bounds",
        lambda *a, **k: ("2026-06-23T21:00:00", "2026-07-07T20:00:00"),  # tz-naive
    )
    inf_results = {
        "anomaly_scores": [0.5, 0.6],
        "sequence_timestamps": [
            "2026-01-01T00:00:00+00:00",  # tz-aware
            "2026-12-31T00:00:00+00:00",
        ],
        "data_persisted": True,
    }

    bounds = mod.resolve_data_bounds("tenant_1", "gh_A", inf_results)
    assert bounds is not None
    start, end = bounds
    assert start == pd.Timestamp("2026-01-01T00:00:00")
    assert end == pd.Timestamp("2026-12-31T00:00:00")
    assert start.tzinfo is None and end.tzinfo is None


def test_resolve_window_dataframe_skips_db_for_upload_only(monkeypatch):
    from src.dashboard.services import anomalies_page_data as mod

    mod.resolve_window_dataframe.clear()
    upload_frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"]),
            "anomaly_score": [0.91],
        }
    )
    inf_results = {
        "sequences_processed": 1,
        "anomaly_scores": [0.91],
        "data_persisted": False,
    }

    def _fail_db_load(*_args, **_kwargs):
        raise AssertionError("DB load should be skipped for upload-only results")

    monkeypatch.setattr(mod, "load_readings_window", _fail_db_load)
    monkeypatch.setattr(
        mod,
        "_upload_score_frame_for_window",
        lambda *a, **k: upload_frame,
    )

    window_start = pd.Timestamp("2026-06-16T00:00:00+00:00")
    window_end = pd.Timestamp("2026-06-18T00:00:00+00:00")
    df, using_fallback = mod.resolve_window_dataframe(
        tenant_id="tenant_1",
        greenhouse_id="gh_A",
        start_iso=window_start.isoformat(),
        end_iso=window_end.isoformat(),
        window_start=window_start,
        window_end=window_end,
        upload_job_id=None,
        _inf_results=inf_results,
    )

    assert using_fallback
    assert len(df) == 1


def test_resolve_data_bounds_skips_db_for_upload_only(monkeypatch):
    from src.dashboard.services import anomalies_page_data as mod

    def _fail_bounds(*_args, **_kwargs):
        raise AssertionError("DB bounds should be skipped for upload-only results")

    monkeypatch.setattr(mod, "load_timestamp_bounds", _fail_bounds)
    inf_results = {
        "anomaly_scores": [0.5],
        "sequence_timestamps": ["2026-06-17T12:00:00+00:00"],
        "data_persisted": False,
    }

    bounds = mod.resolve_data_bounds("tenant_1", "gh_A", inf_results)
    assert bounds is not None
    start, end = bounds
    assert start == pd.Timestamp("2026-06-17T12:00:00")
    assert end == pd.Timestamp("2026-06-17T12:00:00")
    assert start.tzinfo is None and end.tzinfo is None


def test_trim_cold_run_rows_limits_and_keeps_highest_scores():
    rows = [{"score": index / 100.0, "timestamp": f"2026-01-01T{index:02d}:00:00"} for index in range(100)]
    trimmed = trim_cold_run_rows(rows, limit=10)
    assert len(trimmed) == 10
    assert trimmed[0]["score"] >= trimmed[-1]["score"]
