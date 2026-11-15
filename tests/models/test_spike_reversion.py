"""Reversion confirmation for MAD spikes.

A spike comes back to the pre-event level; a step does not. These tests pin
``SpikeMADDetector.reversion_offsets`` / ``detect_confirmed``, the sample the
lag-d enrichment vote reads, and the upload helper's handling of rows it
cannot confirm.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.api.services.upload_portions import _detect_upload_mad_spikes
from src.config import (
    SPIKE_MAD_WINDOW_K,
    UPLOAD_SPIKE_CONFIRMED_FLOOR,
    UPLOAD_SPIKE_CONFIRMED_TUNING_C,
    UPLOAD_SPIKE_REVERT_HORIZON,
)
from src.models.spike_mad_detector import SpikeMADDetector

_C = 5.0
_FLOOR = 0.5
_LEVEL = 24.0
_N = 40
_AT = 20


def _flat() -> np.ndarray:
    return np.full(_N, _LEVEL, dtype=float)


def _detector() -> SpikeMADDetector:
    return SpikeMADDetector(tuning_c=_C)


def test_reverting_single_sample_spike_is_confirmed_at_offset_one() -> None:
    series = _flat()
    series[_AT] += 4.0
    offsets = _detector().reversion_offsets(series, _FLOOR, horizon=3)
    assert offsets[_AT] == 1
    assert [i for i, o in enumerate(offsets) if o is not None] == [_AT]


def test_step_that_does_not_revert_is_flagged_but_never_confirmed() -> None:
    series = _flat()
    series[_AT:] -= 5.0
    detector = _detector()
    assert detector.detect(series, _FLOOR)[_AT] is True
    assert not any(detector.detect_confirmed(series, _FLOOR, horizon=3))


@pytest.mark.parametrize("hold", [2, 3])
def test_hold_within_horizon_is_confirmed_from_its_first_sample(hold: int) -> None:
    series = _flat()
    series[_AT : _AT + hold] += 4.0
    offsets = _detector().reversion_offsets(series, _FLOOR, horizon=3)
    assert offsets[_AT] == hold
    assert offsets[_AT + hold - 1] == 1
    assert offsets[_AT + hold] is None


def test_hold_longer_than_horizon_is_not_confirmed_at_its_first_sample() -> None:
    series = _flat()
    series[_AT : _AT + 3] += 4.0
    offsets = _detector().reversion_offsets(series, _FLOOR, horizon=2)
    assert offsets[_AT] is None
    assert offsets[_AT + 1] == 2


def test_negative_spike_is_confirmed() -> None:
    series = _flat()
    series[_AT] -= 4.0
    confirmed = _detector().detect_confirmed(series, _FLOOR, horizon=1)
    assert [i for i, flag in enumerate(confirmed) if flag] == [_AT]


def test_partial_return_beyond_the_fraction_does_not_confirm() -> None:
    series = _flat()
    series[_AT] += 4.0
    series[_AT + 1 :] += 2.4  # keeps 60 % of the deviation; the limit is 50 %
    assert _detector().reversion_offsets(series, _FLOOR, horizon=3)[_AT] is None
    series = _flat()
    series[_AT] += 4.0
    series[_AT + 1 :] += 1.6  # keeps 40 %
    assert _detector().reversion_offsets(series, _FLOOR, horizon=3)[_AT] == 1


def test_spike_on_the_last_sample_is_flagged_but_unconfirmable() -> None:
    series = _flat()
    series[-1] += 6.0
    detector = _detector()
    assert detector.detect(series, _FLOOR)[-1] is True
    assert detector.reversion_offsets(series, _FLOOR, horizon=3)[-1] is None


@pytest.mark.parametrize("lag", [1, 2, 3])
def test_lag_vote_reads_sample_t_minus_lag(lag: int) -> None:
    """The enrichment vote is ``offsets[-1 - lag]`` with ``horizon=lag``."""
    window = np.full(20, _LEVEL, dtype=float)
    window[-1 - lag] += 4.0
    offsets = _detector().reversion_offsets(window, _FLOOR, horizon=lag)
    assert offsets[-1 - lag] == 1
    assert all(o is None for i, o in enumerate(offsets) if i != len(window) - 1 - lag)
    assert len(window) - 1 - lag >= SPIKE_MAD_WINDOW_K - 1


def test_invalid_horizon_and_fraction_are_rejected() -> None:
    with pytest.raises(ValueError):
        _detector().reversion_offsets(_flat(), _FLOOR, horizon=0)
    with pytest.raises(ValueError):
        _detector().reversion_offsets(_flat(), _FLOOR, horizon=1, fraction=1.0)


def _frame(values: np.ndarray) -> pd.DataFrame:
    stamps = pd.date_range("2026-07-01", periods=len(values), freq="5min", tz="UTC")
    return pd.DataFrame({"timestamp": stamps, "temperature": values})


def test_upload_drops_step_and_unconfirmable_tail_and_reports_boundary_spike_once() -> None:
    assert UPLOAD_SPIKE_REVERT_HORIZON == 3
    delta = 2.0 * UPLOAD_SPIKE_CONFIRMED_TUNING_C * UPLOAD_SPIKE_CONFIRMED_FLOOR["temperature"]
    values = np.full(120, _LEVEL, dtype=float)
    values[30] += delta        # reverting spike
    values[59] += delta        # last row of the first portion: confirmed by row 60
    values[80:100] -= delta    # step down, stays for 20 rows
    values[119] += delta       # last row of the file: nothing after it
    frame = _frame(values)
    labels = frame["timestamp"].tolist()

    whole = [hit.timestamp for hit in _detect_upload_mad_spikes(frame, new_row_start=0)]
    assert whole == [labels[30].isoformat(), labels[59].isoformat()]

    first = _detect_upload_mad_spikes(frame.iloc[:60].reset_index(drop=True), new_row_start=0)
    overlap = 19
    second = _detect_upload_mad_spikes(
        frame.iloc[60 - overlap :].reset_index(drop=True), new_row_start=overlap
    )
    assert [hit.timestamp for hit in first] == [labels[30].isoformat()]
    assert [hit.timestamp for hit in second] == [labels[59].isoformat()]
