"""Unit tests for the shared window-data loader and slider scaling helpers."""

from unittest.mock import patch

import pandas as pd
import pytest

from src.dashboard.services import window_data
from src.dashboard.services.window_data import (
    load_page_window,
    lookback_hours_for_window,
    threshold_slider_range,
)


def _window_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-12-01", periods=5, freq="h"),
            "anomaly_score": [0.05, 0.08, 0.12, 0.15, 0.18],
        }
    )


class TestThresholdSliderRange:
    def test_scores_in_005_018_band_scale_to_headroom_not_full_range(self):
        low, high, step = threshold_slider_range(
            pd.Series([0.05, 0.10, 0.15, 0.18]), saved=0.12
        )
        assert low == 0.0
        # max = max(0.18*1.5, 0.12, 0.2) = 0.27; far below the old 1.0 ceiling.
        assert high == pytest.approx(0.27)
        assert step == 0.01

    def test_empty_scores_fall_back_to_default_span(self):
        low, high, step = threshold_slider_range(pd.Series([], dtype=float), saved=0.1)
        assert (low, step) == (0.0, 0.01)
        assert high == pytest.approx(0.3)  # 0.2 * 1.5

    def test_saved_threshold_never_exceeds_slider_max(self):
        _, high, _ = threshold_slider_range(pd.Series([0.02]), saved=0.4)
        assert high >= 0.4

    def test_wide_range_uses_coarse_step_and_caps_at_one(self):
        _, high, step = threshold_slider_range(pd.Series([0.9]), saved=0.1)
        assert high == 1.0
        assert step == 0.05


class TestLookbackHours:
    def test_seven_day_window_is_168_hours(self):
        assert lookback_hours_for_window(
            pd.Timestamp("2026-12-01"), pd.Timestamp("2026-12-08")
        ) == 168

    def test_sub_hour_window_rounds_up_to_one(self):
        assert lookback_hours_for_window(
            pd.Timestamp("2026-12-01 00:00"), pd.Timestamp("2026-12-01 00:30")
        ) == 1


class TestLoadPageWindow:
    def test_require_scores_uses_resolver_and_returns_fallback_flag(self):
        df = _window_df()
        start, end = df["timestamp"].min(), df["timestamp"].max()
        with patch.object(
            window_data, "resolve_window_dataframe", return_value=(df, True)
        ) as resolver, patch.object(window_data, "load_monitoring_window") as monitor:
            out, fallback = load_page_window(
                "t", "g", start, end, require_scores=True, inf_results={"job_id": "j1"}
            )
        assert fallback is True
        assert resolver.called
        assert not monitor.called
        assert "anomaly_score" in out.columns
        assert len(out) == 5

    def test_no_scores_uses_monitoring_window(self):
        df = _window_df()
        start, end = df["timestamp"].min(), df["timestamp"].max()
        with patch.object(
            window_data, "load_monitoring_window", return_value=df
        ) as monitor, patch.object(window_data, "resolve_window_dataframe") as resolver:
            out, fallback = load_page_window("t", "g", start, end)
        assert fallback is False
        assert monitor.called
        assert not resolver.called
        assert len(out) == 5


class TestAnomaliesPageRegression:
    """The Thresholds/Metrics refactor must not remove resolve_window_dataframe
    from the anomaly page's own data path (it still drives the scored window)."""

    def test_load_scored_window_routes_through_resolve_window_dataframe(self):
        from src.dashboard.services import anomalies_page_data as apd

        df = _window_df()
        with patch.object(
            apd, "resolve_window_dataframe", return_value=(df, False)
        ) as resolver:
            out, fallback = apd.load_scored_window(
                "tenant-regression",
                "gh-regression",
                df["timestamp"].min().isoformat(),
                df["timestamp"].max().isoformat(),
                None,
                "token-regression",
                None,
            )
        assert resolver.called
        assert fallback is False
        assert len(out) == 5
