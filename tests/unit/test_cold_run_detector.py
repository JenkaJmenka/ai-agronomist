"""Tests for statistical cold-run detector."""

import numpy as np

from src.models.cold_run_detector import (
    ColdRunStatisticalDetector,
    _effective_rolling_window,
    is_cold_run_stat_column,
    split_baseline_readings,
)


def test_detects_outdoor_temp_spike_with_rolling_window():
    readings = [
        {"timestamp": f"t{i}", "outdoor_temp": 3.0 + (i % 10) * 0.01}
        for i in range(40)
    ]
    readings.extend(
        {"timestamp": f"t_spike_{i}", "outdoor_temp": -15.0} for i in range(3)
    )
    detector = ColdRunStatisticalDetector(z_min=3.0, min_consecutive=1)
    hits = detector.detect_rolling(readings, window=12)
    outdoor_hits = [h for h in hits if h.column == "outdoor_temp"]
    assert outdoor_hits
    assert max(h.z_score for h in outdoor_hits) >= 3.0


def test_actuator_columns_excluded_from_stat_detection():
    assert not is_cold_run_stat_column("measured_leeward_vent_2")
    assert not is_cold_run_stat_column("light_intensity")
    assert is_cold_run_stat_column("outdoor_temp")


def test_persistence_filter_requires_consecutive_hits():
    readings = [
        {"timestamp": f"t{i}", "outdoor_temp": 3.0 + (i % 10) * 0.01}
        for i in range(40)
    ]
    readings.append({"timestamp": "t_spike", "outdoor_temp": -15.0})
    detector = ColdRunStatisticalDetector(z_min=3.0, min_consecutive=3)
    hits = detector.detect_rolling(readings, window=12)
    assert not [h for h in hits if h.column == "outdoor_temp"]


def test_gradual_seasonal_shift_not_flagged():
    readings = [
        {"timestamp": f"t{i}", "outdoor_temp": 3.0 + i * 0.001}
        for i in range(500)
    ]
    detector = ColdRunStatisticalDetector(z_min=3.5)
    hits = detector.detect_rolling(readings, window=24)
    outdoor_hits = [h for h in hits if h.column == "outdoor_temp"]
    assert len(outdoor_hits) < 5


def test_split_baseline_readings():
    rows = [{"v": i} for i in range(10)]
    baseline, scoring = split_baseline_readings(rows, fraction=0.2)
    assert len(baseline) == 2
    assert len(scoring) == 8


def test_effective_window_scales_for_short_files():
    assert _effective_rolling_window(150, 288) == 12
    assert _effective_rolling_window(100_000, 288) == 288
