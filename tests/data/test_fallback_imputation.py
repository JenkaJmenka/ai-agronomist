"""Tests for fallback gap-filling of sensor time series."""

import pandas as pd
import pytest

from src.data.fallback_imputation import (
    SENSOR_COLS,
    _baseline_value,
    fallback_detect_gaps,
    fallback_fill_from_baseline,
    fallback_fill_gaps,
    fallback_infer_interval,
    fallback_interpolate,
)

FIVE_MIN = pd.Timedelta(minutes=5)


def _frame(timestamps: list[str]) -> pd.DataFrame:
    rows = []
    for i, ts in enumerate(timestamps):
        rows.append(
            {
                "timestamp": ts,
                "temperature": 20.0 + i,
                "humidity": 60.0 + i,
                "co2": 400.0 + i,
                "light": 100.0 + i,
            }
        )
    return pd.DataFrame(rows)


class TestInferInterval:
    def test_most_common_delta(self):
        df = _frame(
            ["2026-06-01 10:00", "2026-06-01 10:05", "2026-06-01 10:10", "2026-06-01 10:20"]
        )
        assert fallback_infer_interval(df) == FIVE_MIN

    def test_short_frame_defaults_to_five_minutes(self):
        assert fallback_infer_interval(_frame(["2026-06-01 10:00"])) == FIVE_MIN


class TestDetectGaps:
    def test_reports_gap_bounds_and_missing_count(self):
        df = _frame(["2026-06-01 10:00", "2026-06-01 10:05", "2026-06-01 10:25"])
        gaps = fallback_detect_gaps(df, FIVE_MIN)
        assert len(gaps) == 1
        gap = gaps.iloc[0]
        assert gap["gap_start"] == pd.Timestamp("2026-06-01 10:05")
        assert gap["gap_end"] == pd.Timestamp("2026-06-01 10:25")
        assert gap["missing_count"] == 3

    def test_no_gaps_returns_empty(self):
        df = _frame(["2026-06-01 10:00", "2026-06-01 10:05"])
        assert fallback_detect_gaps(df, FIVE_MIN).empty

    def test_empty_frame_returns_empty_report(self):
        report = fallback_detect_gaps(pd.DataFrame(), FIVE_MIN)
        assert report.empty
        assert list(report.columns) == ["gap_start", "gap_end", "duration", "missing_count"]


class TestInterpolate:
    def test_short_gap_filled_linearly_and_tagged(self):
        df = _frame(["2026-06-01 10:00", "2026-06-01 10:15"])
        filled = fallback_interpolate(df, max_gap=pd.Timedelta(hours=1), expected_interval=FIVE_MIN)
        assert len(filled) == 4
        synth = filled[filled.get("_synthetic") == True]  # noqa: E712
        assert len(synth) == 2
        # temperature endpoints 20 → 21 interpolated at 1/3 and 2/3
        assert synth["temperature"].tolist() == pytest.approx([20 + 1 / 3, 20 + 2 / 3])

    def test_gap_larger_than_max_left_untouched(self):
        df = _frame(["2026-06-01 10:00", "2026-06-01 14:00"])
        filled = fallback_interpolate(df, max_gap=pd.Timedelta(hours=1), expected_interval=FIVE_MIN)
        assert len(filled) == 2

    def test_empty_frame_passthrough(self):
        df = pd.DataFrame()
        assert fallback_interpolate(df, max_gap=FIVE_MIN) is df


class TestBaselineFill:
    def test_long_gap_filled_from_daily_profile(self):
        df = _frame(["2026-06-01 10:00", "2026-06-01 10:20"])
        filled = fallback_fill_from_baseline(df, expected_interval=FIVE_MIN)
        assert len(filled) == 5
        synth = filled[filled.get("_synthetic") == True]  # noqa: E712
        assert len(synth) == 3
        for col in SENSOR_COLS:
            assert synth[col].notna().all()

    def test_no_gap_passthrough(self):
        df = _frame(["2026-06-01 10:00", "2026-06-01 10:05"])
        assert len(fallback_fill_from_baseline(df, expected_interval=FIVE_MIN)) == 2

    def test_empty_frame_passthrough(self):
        df = pd.DataFrame()
        assert fallback_fill_from_baseline(df) is df

    def test_light_is_zero_at_night(self):
        assert _baseline_value("light", 2.0) == 0.0
        assert _baseline_value("light", 12.0) > 0.0

    def test_baseline_clamped_to_profile_range(self):
        for hour in range(24):
            v = _baseline_value("temperature", float(hour))
            assert 19.0 <= v <= 28.0


class TestFillGaps:
    def test_mixed_strategies_reported(self):
        df = _frame(
            [
                "2026-06-01 10:00",
                "2026-06-01 10:15",  # 15-min gap → interpolate
                "2026-06-01 14:00",  # ~4h gap → baseline
            ]
        )
        filled, report = fallback_fill_gaps(
            df, expected_interval=FIVE_MIN, interpolate_threshold=pd.Timedelta(hours=1)
        )
        assert set(report["strategy"]) == {"interpolate", "baseline"}
        # Every 5-min slot between first and last timestamp exists
        assert len(filled) == 49
        assert filled["timestamp"].is_monotonic_increasing

    def test_no_gaps_short_circuit(self):
        df = _frame(["2026-06-01 10:00", "2026-06-01 10:05"])
        filled, report = fallback_fill_gaps(df, expected_interval=FIVE_MIN)
        assert report.empty
        assert len(filled) == 2

    def test_empty_frame_short_circuit(self):
        df = pd.DataFrame()
        filled, report = fallback_fill_gaps(df)
        assert filled is df
        assert report.empty
