"""Gap handling for present optional sensor columns in CSVAdapter.normalize."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.config import UPLOAD_SPIKE_REVERT_HORIZON
from src.vendors.csv_adapter import (
    OPTIONAL_GAP_FFILL_LIMIT,
    SENSOR_DEFAULTS,
    CSVAdapter,
)

_N = OPTIONAL_GAP_FFILL_LIMIT
_DEFAULT_CO2 = SENSOR_DEFAULTS["co2"]


def _frame(co2: list[float]) -> pd.DataFrame:
    n = len(co2)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-07-01", periods=n, freq="5min").astype(str),
            "temperature": [24.0] * n,
            "humidity": [65.0] * n,
            "co2": co2,
        }
    )


def _co2(values: list[float]) -> list[float]:
    return CSVAdapter({}).normalize(_frame(values))["co2"].tolist()


def test_gap_limit_matches_spike_revert_horizon() -> None:
    assert OPTIONAL_GAP_FFILL_LIMIT == UPLOAD_SPIKE_REVERT_HORIZON


@pytest.mark.parametrize(
    ("gap_len", "expected_fill"),
    [
        (1, 800.0),
        (_N, 800.0),
        (_N + 1, _DEFAULT_CO2),
    ],
)
def test_interior_gap_rule_is_on_whole_run_length(gap_len: int, expected_fill: float) -> None:
    nan = float("nan")
    trace = [800.0, 800.0] + [nan] * gap_len + [810.0, 810.0]
    out = _co2(trace)
    assert out[2 : 2 + gap_len] == [expected_fill] * gap_len
    assert out[:2] == [800.0, 800.0] and out[-2:] == [810.0, 810.0]
    if gap_len == 1:
        # No reverting -400 ppm spike from a single missing sample.
        assert _DEFAULT_CO2 not in out


def test_leading_nan_defaults_and_absent_column_is_constant_default() -> None:
    out = _co2([float("nan"), float("nan"), 800.0, 800.0])
    assert out == [_DEFAULT_CO2, _DEFAULT_CO2, 800.0, 800.0]

    absent = _frame([800.0] * 4).drop(columns=["co2"])
    result = CSVAdapter({}).normalize(absent)
    assert (result["co2"] == _DEFAULT_CO2).all()


def test_single_missing_co2_sample_leaves_adapter_output_spike_free() -> None:
    # CO2 has no UPLOAD_SPIKE_MAD_FLOOR / ENRICHMENT_SPIKE_CONFIRMED_PAIRS key,
    # so the upload MAD spike path does not score it; assert on adapter output.
    rng = np.random.default_rng(0)
    trace = (800.0 + rng.normal(0.0, 2.0, 60)).tolist()
    trace[30] = float("nan")
    out = np.asarray(_co2(trace))
    assert np.abs(np.diff(out)).max() < 20.0
