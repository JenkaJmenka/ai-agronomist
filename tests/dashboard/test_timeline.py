from datetime import date, time

import pandas as pd

from src.dashboard.components.timeline_utils import (
    clamp_timeline_bounds,
    combine_date_and_time,
    default_seven_day_window,
    window_from_past_preset,
)


def test_combine_date_and_time():
    result = combine_date_and_time(date(2026, 6, 1), time(8, 30))
    assert result == pd.Timestamp("2026-06-01 08:30:00").to_pydatetime()


def test_clamp_timeline_bounds_swaps_and_clips():
    data_min = pd.Timestamp("2026-06-01")
    data_max = pd.Timestamp("2026-06-20")
    start, end = clamp_timeline_bounds(
        pd.Timestamp("2026-06-25"),
        pd.Timestamp("2026-06-10"),
        data_min,
        data_max,
    )
    assert start == pd.Timestamp("2026-06-10")
    assert end == pd.Timestamp("2026-06-20")


def test_clamp_timeline_bounds_keeps_valid_selection():
    data_min = pd.Timestamp("2026-06-01")
    data_max = pd.Timestamp("2026-06-20")
    start, end = clamp_timeline_bounds(
        pd.Timestamp("2026-06-05"),
        pd.Timestamp("2026-06-15"),
        data_min,
        data_max,
    )
    assert start == pd.Timestamp("2026-06-05")
    assert end == pd.Timestamp("2026-06-15")


def test_default_seven_day_window_ends_at_2355():
    data_min = pd.Timestamp("2026-05-01")
    data_max = pd.Timestamp("2026-06-20 18:42:00")
    start, end = default_seven_day_window(data_min, data_max)
    assert start == pd.Timestamp("2026-06-14 00:00:00")
    assert end == pd.Timestamp("2026-06-20 18:42:00")


def test_past_24h_window_anchors_to_data_max():
    data_min = pd.Timestamp("2026-06-17 00:00:00")
    data_max = pd.Timestamp("2026-06-18 23:55:00")
    start, end = window_from_past_preset("24h", data_min, data_max)
    assert end == data_max
    assert start == pd.Timestamp("2026-06-17 23:55:00")
