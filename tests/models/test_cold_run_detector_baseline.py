"""Tests for ColdRunStatisticalDetector's baseline fit/detect API and helpers."""

import numpy as np
import pytest

from src.models.cold_run_detector import (
    ColdRunStatisticalDetector,
    _effective_rolling_window,
    is_cold_run_stat_column,
    split_baseline_readings,
)


def _rows(n: int, temp_fn=lambda i: 20.0 + (i % 5) * 0.3) -> list[dict]:
    return [
        {
            "timestamp": f"2026-06-09T{10 + i // 60:02d}:{i % 60:02d}:00Z",
            "temperature": temp_fn(i),
            "humidity": 55.0 + (i % 4),
            "light": 900.0,  # excluded column
            "co2": 400.0,  # gas column — excluded
        }
        for i in range(n)
    ]


class TestColumnSelection:
    def test_climate_columns_accepted(self):
        assert is_cold_run_stat_column("temperature")
        assert is_cold_run_stat_column("measured_relative_humidity")
        assert is_cold_run_stat_column("wind_speed")

    def test_metadata_gas_and_actuators_excluded(self):
        for name in ("timestamp", "source", "co2", "light", "vent_position", "rain"):
            assert not is_cold_run_stat_column(name)

    def test_discover_columns_sorted(self):
        det = ColdRunStatisticalDetector()
        assert det._discover_columns(_rows(3)) == ["humidity", "temperature"]


class TestFitDetect:
    def test_fit_builds_baselines_for_stat_columns_only(self):
        det = ColdRunStatisticalDetector()
        baselines = det.fit(_rows(60))
        assert set(baselines) == {"humidity", "temperature"}
        temp = baselines["temperature"]
        assert temp.mean == pytest.approx(np.mean([20.0 + (i % 5) * 0.3 for i in range(60)]))
        assert temp.n == 60
        assert temp.iqr == pytest.approx(temp.q75 - temp.q25)

    def test_fit_skips_columns_below_min_samples(self):
        det = ColdRunStatisticalDetector()
        assert det.fit(_rows(2)) == {}

    def test_fit_ignores_non_numeric_and_non_finite(self):
        rows = _rows(60)
        rows[0]["temperature"] = "not-a-number"
        rows[1]["temperature"] = float("inf")
        det = ColdRunStatisticalDetector()
        baselines = det.fit(rows)
        assert baselines["temperature"].n == 58

    def test_detect_flags_outlier_with_severity(self):
        det = ColdRunStatisticalDetector(z_min=3.0)
        baselines = det.fit(_rows(60))
        results = det.detect({"temperature": 95.0, "humidity": 56.0}, baselines)
        assert results["temperature"].is_anomaly
        assert results["temperature"].method == "z_score"
        assert results["temperature"].severity == "critical"
        assert not results["humidity"].is_anomaly

    def test_detect_skips_missing_and_invalid_values(self):
        det = ColdRunStatisticalDetector()
        baselines = det.fit(_rows(60))
        results = det.detect({"temperature": "bad", "humidity": None}, baselines)
        assert results == {}

    def test_zero_std_baseline_scores_normal(self):
        det = ColdRunStatisticalDetector()
        baselines = det.fit(_rows(60, temp_fn=lambda i: 21.0))
        result = det.detect({"temperature": 40.0}, baselines)["temperature"]
        assert not result.is_anomaly
        assert result.method == "none"

    def test_iqr_fence_flags_when_z_does_not(self):
        det = ColdRunStatisticalDetector(z_min=1e9, use_iqr=True)
        baselines = det.fit(_rows(60))
        result = det.detect({"temperature": 60.0}, baselines)["temperature"]
        assert result.is_anomaly
        assert result.method == "iqr"


class TestDetectRollingEdges:
    def test_too_few_rows_returns_empty(self):
        det = ColdRunStatisticalDetector()
        assert det.detect_rolling(_rows(10)) == []

    def test_missing_column_ignored(self):
        det = ColdRunStatisticalDetector()
        assert det.detect_rolling(_rows(60), columns=["nonexistent_temp"]) == []

    def test_column_series_maps_invalid_to_nan(self):
        det = ColdRunStatisticalDetector()
        rows = _rows(3)
        rows[1]["temperature"] = "oops"
        values, timestamps = det._column_series(rows, "temperature")
        assert np.isnan(values[1])
        assert len(timestamps) == 3


class TestHelpers:
    def test_effective_window_scaling(self):
        assert _effective_rolling_window(10, 288) == 0
        assert _effective_rolling_window(100, 288) == 50
        assert _effective_rolling_window(576, 288) == 288
        assert _effective_rolling_window(5000, 288) == 288
        assert _effective_rolling_window(100_000, 288) == 288

    def test_split_baseline_readings_fraction(self):
        rows = _rows(10)
        baseline, scoring = split_baseline_readings(rows, fraction=0.3)
        assert len(baseline) == 3
        assert len(scoring) == 7

    def test_split_empty(self):
        assert split_baseline_readings([]) == ([], [])
