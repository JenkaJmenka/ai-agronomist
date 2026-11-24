"""Tests for ``replay_series`` -- the read-only Drift Watch chart replay (C4).

``replay_series`` shares its per-hour scoring body with ``DriftWatch.update``
(``_maybe_rolling_refit`` / ``_residual_with_reference`` / ``_score_hours``),
so on the same data it must reproduce exactly the alarms that hour-by-hour
``update`` calls emit, refits included.
"""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from src.models.drift_watch import (
    REPLAY_COLUMNS,
    CalibrationResult,
    DriftInputSpec,
    DriftWatch,
    InputKind,
    InputStatus,
    _tetens_hd,
    replay_series,
)

_T0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")
_STEP_C = 3.0
_PAIRED = DriftInputSpec(
    input_id="temp_paired",
    kind=InputKind.PAIRED_PROBES,
    target_channel="temp_a",
    channels=["temp_b", "outdoor"],
    min_shift=0.5,
)


def _paired_frame(weeks: float, seed: int) -> pd.DataFrame:
    """Seeded paired-probe frame: temp_a tracks temp_b plus an outdoor term."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(_T0, periods=int(weeks * 7 * 24), freq="1h")
    hours = idx.hour.to_numpy()
    outdoor = 10.0 + 6.0 * np.sin(2 * np.pi * (hours - 9) / 24.0) + rng.normal(0, 1.0, len(idx))
    temp_b = 22.0 + 3.0 * np.sin(2 * np.pi * hours / 24.0) + rng.normal(0, 0.4, len(idx))
    temp_a = temp_b + 0.05 * outdoor + rng.normal(0, 0.2, len(idx))
    df = pd.DataFrame({"temp_a": temp_a, "temp_b": temp_b, "outdoor": outdoor}, index=idx)
    df["hour"] = df.index.hour
    return df


@pytest.fixture(scope="module")
def paired_case() -> tuple[pd.DataFrame, CalibrationResult, pd.Timestamp]:
    """Calibrate on weeks 0-16, replay weeks 14-17.5 with a +3 C step at hour 480."""
    full = _paired_frame(17.5, seed=7)
    cal = DriftWatch.calibrate(full[full.index < _T0 + pd.Timedelta(weeks=16, days=1)], _PAIRED)
    assert cal.status != InputStatus.DERIVED.value
    frame = full[full.index >= _T0 + pd.Timedelta(weeks=14)].copy()
    step_ts = frame.index[480]
    frame.loc[frame.index >= step_ts, "temp_a"] += _STEP_C
    return frame, cal, step_ts


def test_replay_reproduces_update_alarms_and_statistics(
    paired_case: tuple[pd.DataFrame, CalibrationResult, pd.Timestamp],
) -> None:
    """Same alarm hours and onsets as hourly update; dev ~ +3; s_pos crosses h; no mutation."""
    frame, cal, step_ts = paired_case
    frame_before = frame.copy(deep=True)
    cal_before = copy.deepcopy(cal.to_dict())

    out = replay_series(frame, _PAIRED, cal)

    # Read-only: inputs unchanged.
    pd.testing.assert_frame_equal(frame, frame_before)
    assert cal.to_dict() == cal_before
    assert list(out.columns) == list(REPLAY_COLUMNS)

    # Reference column is defined so target - reference == residual.
    np.testing.assert_allclose(out["target"] - out["reference"], out["residual"], atol=1e-9)

    # Drive update hour by hour on the same data -- the cadence replay emulates.
    state = DriftWatch.init_state(cal)
    events = []
    for i in range(len(frame)):
        events.extend(DriftWatch.update(state, frame.iloc[: i + 1], _PAIRED))
    assert state.last_refit_ts is not None  # refits did run on this span

    upd_alarms = [(e.last_confirmed_ts, e.direction) for e in events]
    rep_alarms = [(r.ts, r.alarm_dir) for r in out.itertuples() if r.alarm_dir is not None]
    assert rep_alarms == upd_alarms
    onsets = [(e.first_detected_ts, e.direction) for e in events if e.is_new]
    step_onsets = [o for o in onsets if o[0] >= step_ts and o[1] == "+"]
    assert step_onsets, "the +3 C step must raise a positive episode"
    onset_ts = step_onsets[0][0]

    # dev ~ +3 after onset (mu in force that hour, refits included).
    after = out[out["ts"] >= onset_ts]
    assert after["dev"].median() == pytest.approx(_STEP_C, abs=0.3)

    # s_pos crosses h exactly at the alarm hour (pre-reset value).
    pos = out.index[out["ts"] == onset_ts][0]
    assert out.loc[pos, "s_pos"] > cal.h
    assert out.loc[pos - 1, "s_pos"] <= cal.h
    assert out.loc[pos, "alarm_dir"] == "+"

    # start/end trim the output without changing the replayed values.
    window = replay_series(frame, _PAIRED, cal, start=step_ts, end=frame.index[-1])
    assert window["ts"].iloc[0] == step_ts
    pd.testing.assert_frame_equal(window, out[out["ts"] >= step_ts].reset_index(drop=True))


def _manual_cal(input_id: str, kind: InputKind, **model: object) -> CalibrationResult:
    """Hand-built armed calibration (k, h, mu, sigma fixed)."""
    return CalibrationResult(
        input_id=input_id, kind=kind.value, status=InputStatus.ARMED.value,
        k=0.5, h=5.0, scale_mu=0.0, scale_sigma=1.0,
        fit_window_start=str(_T0), fit_window_end=str(_T0),
        model_coefficients=model.get("coefs"),  # type: ignore[arg-type]
        model_intercept=model.get("intercept"),  # type: ignore[arg-type]
        model_feature_names=model.get("feats"),  # type: ignore[arg-type]
        model_fill_values=model.get("fill"),  # type: ignore[arg-type]
        measured_fa_per_month=0.0, self_test=[],
    )


def test_physics_and_virtual_sensor_return_reference() -> None:
    """physics -> Tetens HD; virtual_sensor -> the ridge prediction."""
    rng = np.random.default_rng(3)
    idx = pd.date_range(_T0, periods=48, freq="1h")
    t = 22.0 + rng.normal(0, 1.0, len(idx))
    rh = 70.0 + rng.normal(0, 3.0, len(idx))
    df = pd.DataFrame({"t": t, "rh": rh, "x": rng.normal(0, 1.0, len(idx))}, index=idx)
    df["hd"] = _tetens_hd(t, rh) + rng.normal(0, 0.3, len(idx))
    df["y"] = 2.0 * df["x"] + 0.5 + rng.normal(0, 0.1, len(idx))

    phys = DriftInputSpec("hd_physics", InputKind.PHYSICS, "hd", ["t", "rh", "hd"], 0.3)
    out_p = replay_series(df, phys, _manual_cal("hd_physics", InputKind.PHYSICS))
    np.testing.assert_allclose(out_p["reference"], _tetens_hd(t, rh))
    np.testing.assert_allclose(out_p["residual"], df["hd"].to_numpy() - _tetens_hd(t, rh))

    vs = DriftInputSpec("y_virtual", InputKind.VIRTUAL_SENSOR, "y", ["x"], 0.5)
    vs_cal = _manual_cal(
        "y_virtual", InputKind.VIRTUAL_SENSOR, coefs=[2.0, 0.0, 0.0], intercept=0.5,
        feats=["x", "hour_sin", "hour_cos"], fill=[0.0, 0.0, 0.0],
    )
    out_v = replay_series(df, vs, vs_cal)
    np.testing.assert_allclose(out_v["reference"], 2.0 * df["x"].to_numpy() + 0.5)
    np.testing.assert_allclose(out_v["target"] - out_v["reference"], out_v["residual"], atol=1e-12)
    assert len(out_p) == len(out_v) == len(df)
