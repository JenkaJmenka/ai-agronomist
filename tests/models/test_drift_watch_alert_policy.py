"""Unit tests for the ADR-013 drift-watch alert policy pieces in src/models.

Covers ADR-013 §8 step 2 (ml-engineer):

- ``CalibrationResult.calibration_window_end`` is set by calibrate(), is
  never changed by the rolling refit, and old payloads load as ``None``.
- ``DriftEpisode.offset_robust`` is the median of ``residual - mu`` from the
  episode's first onset to the current confirmation.
- The pure §3 gate: OOS count rules and the off/eligible/enabled/suspended
  transition table, including the 3/4 and 6/7 hysteresis edges.

The broader behavioural suite (alert writer, backfill, feedback survival) is
qa-engineer's step 8.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from src.config import (
    DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT,
    DRIFT_WATCH_ALERT_MIN_OFFSET_MULT,
    DRIFT_WATCH_ALERT_MIN_OOS_WEEKS,
    DRIFT_WATCH_ALERT_RATE_WINDOW_WEEKS,
    DRIFT_WATCH_ALERT_RECENCY_DAYS,
    DRIFT_WATCH_ALERT_SUSPEND_MIN_COUNT,
    DRIFT_WATCH_ALERTS_ENABLED,
    DRIFT_WATCH_BASELINE_WEEKS,
    DRIFT_WATCH_GAP_WEEKS,
)
from src.models.drift_watch import (
    AlertState,
    CalibrationResult,
    DriftEpisodeKey,
    DriftInputSpec,
    DriftInputState,
    DriftWatch,
    FeedbackLabel,
    GateEpisode,
    InputKind,
    InputStatus,
    _rolling_refit,
    count_oos_false_alarms,
    episode_natural_key,
    evaluate_alert_gate,
    passes_alert_min_offset,
)

# ── Fixtures / helpers ───────────────────────────────────────────────────

_T0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")
_MIN_SHIFT = 0.5
_SPEC = DriftInputSpec(
    input_id="vs_test",
    kind=InputKind.VIRTUAL_SENSOR,
    target_channel="target",
    channels=["x"],
    min_shift=_MIN_SHIFT,
)


def _hourly_frame(weeks: int, seed: int = 0, start: pd.Timestamp = _T0) -> pd.DataFrame:
    """Synthetic hourly frame: target = 2*x + 1 + small noise."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=weeks * 7 * 24, freq="1h")
    hours = idx.hour.to_numpy()
    x = 20.0 + 3.0 * np.sin(2 * np.pi * hours / 24.0) + rng.normal(0, 0.5, len(idx))
    target = 2.0 * x + 1.0 + rng.normal(0, 0.2, len(idx))
    df = pd.DataFrame({"x": x, "target": target}, index=idx)
    df["hour"] = df.index.hour
    return df


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    """28 weeks of synthetic data: 16 for calibration, 12 for scoring."""
    return _hourly_frame(28)


@pytest.fixture(scope="module")
def calibration(frame: pd.DataFrame) -> CalibrationResult:
    """Calibrate on the first 16 weeks plus a day (calibrate needs >= 16 full weeks)."""
    cal_df = frame[frame.index < _T0 + pd.Timedelta(weeks=16, days=1)]
    return DriftWatch.calibrate(cal_df, _SPEC)


# ── calibration_window_end ───────────────────────────────────────────────


class TestCalibrationWindowEnd:
    """ADR-013 §3: an immutable OOS boundary on CalibrationResult."""

    def test_calibrate_sets_last_hour_of_calibration_window(
        self, frame: pd.DataFrame, calibration: CalibrationResult,
    ) -> None:
        cal_start = _T0 + pd.Timedelta(weeks=DRIFT_WATCH_BASELINE_WEEKS + DRIFT_WATCH_GAP_WEEKS)
        cal_end_excl = cal_start + pd.Timedelta(weeks=DRIFT_WATCH_BASELINE_WEEKS)
        expected = frame.index[frame.index < cal_end_excl][-1]
        assert calibration.calibration_window_end is not None
        assert pd.Timestamp(calibration.calibration_window_end) == expected
        # It is distinct from the fit window, which ends 8 weeks earlier.
        assert pd.Timestamp(calibration.fit_window_end) < expected

    def test_derived_physics_result_also_carries_it(self) -> None:
        idx = pd.date_range(_T0, periods=(16 * 7 + 1) * 24, freq="1h")
        rng = np.random.default_rng(1)
        t = 22.0 + rng.normal(0, 1, len(idx))
        rh = 70.0 + rng.normal(0, 3, len(idx))
        from src.models.drift_watch import _tetens_hd
        df = pd.DataFrame({"t": t, "rh": rh, "hd": _tetens_hd(t, rh)}, index=idx)
        spec = DriftInputSpec("hd", InputKind.PHYSICS, "hd", ["t", "rh", "hd"], 0.3)
        res = DriftWatch.calibrate(df, spec)
        assert res.status == InputStatus.DERIVED.value
        cal_end_excl = _T0 + pd.Timedelta(weeks=2 * DRIFT_WATCH_BASELINE_WEEKS)
        assert res.calibration_window_end == str(idx[idx < cal_end_excl][-1])

    def test_round_trip(self, calibration: CalibrationResult) -> None:
        again = CalibrationResult.from_dict(calibration.to_dict())
        assert again.calibration_window_end == calibration.calibration_window_end

    def test_old_payload_without_field_loads_as_none(
        self, calibration: CalibrationResult,
    ) -> None:
        d = calibration.to_dict()
        del d["calibration_window_end"]
        assert CalibrationResult.from_dict(d).calibration_window_end is None

    def test_refit_does_not_touch_calibration_window_end(
        self, frame: pd.DataFrame, calibration: CalibrationResult,
    ) -> None:
        """Property: across many refits the boundary is byte-identical,
        while the fit window it used to be confused with does move."""
        state = DriftWatch.init_state(calibration)
        original = calibration.calibration_window_end
        start = _T0 + pd.Timedelta(weeks=16)
        fit_ends = set()
        for week in range(12):
            chunk_end = start + pd.Timedelta(weeks=week + 1)
            DriftWatch.update(state, frame[frame.index < chunk_end], _SPEC)
            assert state.calibration["calibration_window_end"] == original
            fit_ends.add(state.calibration["fit_window_end"])
        assert state.last_refit_ts is not None
        assert len(fit_ends) > 1, "refit never ran; the property was not exercised"

    def test_direct_refit_preserves_field(
        self, frame: pd.DataFrame, calibration: CalibrationResult,
    ) -> None:
        state = DriftWatch.init_state(calibration)
        state.last_processed_ts = str(_T0 + pd.Timedelta(weeks=20))
        before = copy.deepcopy(state.calibration)
        _rolling_refit(state, frame, _SPEC)
        assert state.calibration["fit_window_end"] != before["fit_window_end"]
        assert state.calibration["calibration_window_end"] == before["calibration_window_end"]

    def test_refit_on_legacy_state_keeps_none(
        self, frame: pd.DataFrame, calibration: CalibrationResult,
    ) -> None:
        """A legacy payload must not acquire a derived boundary from a refit."""
        state = DriftWatch.init_state(calibration)
        del state.calibration["calibration_window_end"]
        state.last_processed_ts = str(_T0 + pd.Timedelta(weeks=20))
        _rolling_refit(state, frame, _SPEC)
        assert state.calibration.get("calibration_window_end") is None


# ── Robust offset ─────────────────────────────────────────────────────────


class TestRobustOffset:
    """ADR-013 §5: median of (residual - mu) from onset to now, signed."""

    def _faulted_episodes(
        self, frame: pd.DataFrame, calibration: CalibrationResult, bias: float,
    ) -> List:
        df = frame.copy()
        fault_start = _T0 + pd.Timedelta(weeks=18)
        df.loc[df.index >= fault_start, "target"] += bias
        state = DriftWatch.init_state(calibration)
        # Warm the state up to just before the fault so last_processed_ts is set.
        DriftWatch.update(state, df[df.index < fault_start], _SPEC)
        eps = []
        for week in range(3):
            chunk_end = fault_start + pd.Timedelta(weeks=week + 1)
            eps.extend(DriftWatch.update(state, df[df.index < chunk_end], _SPEC))
        return eps

    @pytest.mark.parametrize("bias", [2.0, -2.0])
    def test_sign_and_magnitude_track_sustained_bias(
        self, frame: pd.DataFrame, calibration: CalibrationResult, bias: float,
    ) -> None:
        eps = self._faulted_episodes(frame, calibration, bias)
        direction = "+" if bias > 0 else "-"
        mine = [e for e in eps if e.direction == direction]
        assert mine, "the injected bias was not detected"
        last = mine[-1]
        assert last.offset_robust is not None
        assert np.sign(last.offset_robust) == np.sign(bias)
        # Robust median over weeks of a step is close to the step.
        assert abs(last.offset_robust - bias) < 0.25 * abs(bias)
        # current_offset_native keeps its old convention (magnitude-signed by direction).
        assert last.current_offset_native > 0

    def test_continuations_share_one_buffer(
        self, frame: pd.DataFrame, calibration: CalibrationResult,
    ) -> None:
        eps = self._faulted_episodes(frame, calibration, 2.0)
        pos = [e for e in eps if e.direction == "+"]
        assert pos[0].is_new and all(not e.is_new for e in pos[1:])
        assert len({e.first_detected_ts for e in pos}) == 1

    def test_median_is_exact_over_onset_to_now(self) -> None:
        """Hand-built CUSUM: k=0, h=2.5, mu=0, sigma=1; residual = the series."""
        idx = pd.date_range(_T0, periods=6, freq="1h")
        series = [0.0, -1.0, 1.0, 1.0, 1.0, 9.0]
        cal = CalibrationResult(
            input_id="p", kind=InputKind.PHYSICS.value, status="armed",
            k=0.0, h=2.5, scale_mu=0.0, scale_sigma=1.0,
            fit_window_start=str(idx[0]), fit_window_end=str(idx[0]),
            model_coefficients=None, model_intercept=None,
            model_feature_names=None, model_fill_values=None,
            measured_fa_per_month=0.0, self_test=[],
            calibration_window_end=str(idx[0]),
        )
        # Physics residual = hd - tetens(t, rh); feed hd = tetens + series.
        from src.models.drift_watch import _tetens_hd
        t = np.full(6, 20.0)
        rh = np.full(6, 60.0)
        df = pd.DataFrame(
            {"t": t, "rh": rh, "hd": _tetens_hd(t, rh) + np.array(series)},
            index=idx,
        )
        spec = DriftInputSpec("p", InputKind.PHYSICS, "hd", ["t", "rh", "hd"], 0.3)
        state = DriftWatch.init_state(cal)
        eps = DriftWatch.update(state, df, spec)
        # s_pos: 0 (zero), 0 (zero, onset), 1, 2, 3 -> alarm at idx[4].
        assert eps[0].direction == "+" and eps[0].last_confirmed_ts == idx[4]
        assert eps[0].onset_estimate == idx[1]
        # devs from onset idx[1]..idx[4] = [-1, 1, 1, 1] -> median 1.0
        assert eps[0].offset_robust == pytest.approx(1.0)
        # idx[5]: s_pos = 9 > h -> continuation; buffer [-1,1,1,1,9] -> median 1.0,
        # whereas current_offset_native is the single hour (9).
        assert not eps[1].is_new
        assert eps[1].offset_robust == pytest.approx(1.0)
        assert eps[1].current_offset_native == pytest.approx(9.0)

    def test_state_round_trip_keeps_buffers(
        self, frame: pd.DataFrame, calibration: CalibrationResult,
    ) -> None:
        state = DriftWatch.init_state(calibration)
        state.pos_episode_devs = [1.0, 2.0]
        again = DriftInputState.from_dict(state.to_dict())
        assert again.pos_episode_devs == [1.0, 2.0]

    def test_legacy_state_without_buffers_loads(
        self, calibration: CalibrationResult,
    ) -> None:
        d = DriftWatch.init_state(calibration).to_dict()
        for k in ("pos_pending_devs", "pos_episode_devs",
                  "neg_pending_devs", "neg_episode_devs"):
            del d[k]
        st = DriftInputState.from_dict(d)
        assert st.pos_episode_devs == [] and st.neg_pending_devs == []


# ── Gate helpers ──────────────────────────────────────────────────────────

_AS_OF = pd.Timestamp("2026-10-01 00:00", tz="UTC")
_CAL_END = _AS_OF - pd.Timedelta(weeks=DRIFT_WATCH_ALERT_MIN_OOS_WEEKS + 10)
_BIG = DRIFT_WATCH_ALERT_MIN_OFFSET_MULT * _MIN_SHIFT * 2


def _key(ts: pd.Timestamp, direction: str = "+", input_id: str = "vs_test") -> DriftEpisodeKey:
    return episode_natural_key("t1", "gh", input_id, direction, ts)


def _eps(n: int, *, offset: Optional[float] = _BIG, start_days_ago: int = 1) -> List[GateEpisode]:
    """*n* distinct OOS episodes inside the trailing window."""
    return [
        GateEpisode(key=_key(_AS_OF - pd.Timedelta(days=start_days_ago + i)), offset_robust=offset)
        for i in range(n)
    ]


def _gate(
    n: int,
    state: AlertState,
    *,
    status: str = "armed",
    cal_end: Optional[pd.Timestamp] = _CAL_END,
    enabled: bool = True,
    labels: Optional[Dict[DriftEpisodeKey, str]] = None,
):
    return evaluate_alert_gate(
        _eps(n), labels or {},
        current_state=state,
        input_status=status,
        calibration_window_end=cal_end,
        as_of=_AS_OF,
        min_shift=_MIN_SHIFT,
        alerts_enabled=enabled,
    )


class TestConfig:
    """Step 1 constants hold the ADR values."""

    def test_values(self) -> None:
        assert DRIFT_WATCH_ALERTS_ENABLED is False
        assert DRIFT_WATCH_ALERT_MIN_OFFSET_MULT == 2.0
        assert DRIFT_WATCH_ALERT_MIN_OOS_WEEKS == 12
        assert DRIFT_WATCH_ALERT_RATE_WINDOW_WEEKS == 13
        assert DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT == 3
        assert DRIFT_WATCH_ALERT_SUSPEND_MIN_COUNT == 7
        assert DRIFT_WATCH_ALERT_RECENCY_DAYS == 14


class TestMinOffset:
    def test_boundary_inclusive(self) -> None:
        limit = DRIFT_WATCH_ALERT_MIN_OFFSET_MULT * _MIN_SHIFT
        assert passes_alert_min_offset(limit, _MIN_SHIFT)
        assert passes_alert_min_offset(-limit, _MIN_SHIFT)
        assert not passes_alert_min_offset(limit * 0.99, _MIN_SHIFT)

    def test_unknown_offset_passes(self) -> None:
        assert passes_alert_min_offset(None, _MIN_SHIFT)
        assert passes_alert_min_offset(float("nan"), _MIN_SHIFT)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_bad_min_shift(self, bad: float) -> None:
        with pytest.raises(ValueError):
            passes_alert_min_offset(1.0, bad)


class TestOosCount:
    def _count(self, eps, labels=None, cal_end=_CAL_END):
        return count_oos_false_alarms(
            eps, labels or {},
            calibration_window_end=cal_end, as_of=_AS_OF, min_shift=_MIN_SHIFT,
        )

    def test_unlabelled_counts(self) -> None:
        assert self._count(_eps(3)) == (3, 3)

    def test_false_alarm_label_counts(self) -> None:
        eps = _eps(2)
        labels = {eps[0].key: FeedbackLabel.FALSE_ALARM.value}
        assert self._count(eps, labels).count == 2

    def test_only_confirmed_drift_removes(self) -> None:
        eps = _eps(3)
        labels = {
            eps[0].key: FeedbackLabel.CONFIRMED_DRIFT.value,
            eps[1].key: "something_else",
        }
        assert self._count(eps, labels) == (2, 3)

    def test_enum_label_value_is_honoured(self) -> None:
        """A FeedbackLabel member passed as the label must match by value."""
        eps = _eps(2)
        labels = {eps[0].key: FeedbackLabel.CONFIRMED_DRIFT}
        assert self._count(eps, labels).count == 1  # type: ignore[arg-type]

    def test_label_matches_across_timestamp_representations(self) -> None:
        eps = _eps(1)
        naive = eps[0].key.first_detected_ts.tz_localize(None).to_pydatetime()
        labels = {DriftEpisodeKey("t1", "gh", "vs_test", "+", naive): "confirmed_drift"}
        assert self._count(eps, labels).count == 0

    def test_label_on_other_direction_does_not_match(self) -> None:
        eps = _eps(1)
        other = eps[0].key._replace(direction="-")
        assert self._count(eps, {other: "confirmed_drift"}).count == 1

    def test_in_sample_never_counted(self) -> None:
        cal_end = _AS_OF - pd.Timedelta(days=2)
        eps = [
            GateEpisode(_key(cal_end), _BIG),                          # == boundary: in-sample
            GateEpisode(_key(cal_end - pd.Timedelta(hours=1)), _BIG),  # before: in-sample
            GateEpisode(_key(cal_end + pd.Timedelta(hours=1)), _BIG),  # OOS
        ]
        assert self._count(eps, cal_end=cal_end) == (1, 1)

    def test_unknown_boundary_counts_nothing(self) -> None:
        assert self._count(_eps(5), cal_end=None) == (0, 0)

    def test_window_edges(self) -> None:
        w = pd.Timedelta(weeks=DRIFT_WATCH_ALERT_RATE_WINDOW_WEEKS)
        eps = [
            GateEpisode(_key(_AS_OF - w), _BIG),                           # excluded
            GateEpisode(_key(_AS_OF - w + pd.Timedelta(hours=1)), _BIG),   # included
            GateEpisode(_key(_AS_OF), _BIG),                               # included
        ]
        assert self._count(eps).count == 2

    def test_filters_affect_count_not_raw(self) -> None:
        small = DRIFT_WATCH_ALERT_MIN_OFFSET_MULT * _MIN_SHIFT * 0.5
        eps = [
            GateEpisode(_key(_AS_OF - pd.Timedelta(days=1)), small),
            GateEpisode(_key(_AS_OF - pd.Timedelta(days=2)), _BIG, suppressed=True),
            GateEpisode(_key(_AS_OF - pd.Timedelta(days=3)), None),
        ]
        assert self._count(eps) == (1, 3)

    def test_continuation_events_dedupe(self) -> None:
        ep = _eps(1)[0]
        assert self._count([ep, ep, ep]).count == 1

    def test_mixed_inputs_rejected(self) -> None:
        eps = [GateEpisode(_key(_AS_OF, input_id="a")), GateEpisode(_key(_AS_OF, input_id="b"))]
        with pytest.raises(ValueError, match="more than one"):
            self._count(eps)

    def test_episode_after_as_of_rejected(self) -> None:
        with pytest.raises(ValueError, match="after as_of"):
            self._count([GateEpisode(_key(_AS_OF + pd.Timedelta(hours=1)))])

    def test_bad_direction_rejected(self) -> None:
        with pytest.raises(ValueError):
            episode_natural_key("t", "g", "i", "up", _AS_OF)


# ── Transition table ──────────────────────────────────────────────────────


class TestTransitions:
    """ADR-013 §3 table, with the 3/4 and 6/7 edges."""

    # off -> eligible edge at 3/4
    @pytest.mark.parametrize("n,expected", [
        (0, AlertState.ELIGIBLE),
        (3, AlertState.ELIGIBLE),
        (4, AlertState.OFF),
    ])
    def test_off_edge(self, n: int, expected: AlertState) -> None:
        assert _gate(n, AlertState.OFF).next_state is expected

    # enabled -> suspended edge at 6/7 (4..6 is hysteresis: stays enabled)
    @pytest.mark.parametrize("n,expected", [
        (3, AlertState.ENABLED),
        (4, AlertState.ENABLED),
        (6, AlertState.ENABLED),
        (7, AlertState.SUSPENDED),
        (12, AlertState.SUSPENDED),
    ])
    def test_enabled_edge(self, n: int, expected: AlertState) -> None:
        d = _gate(n, AlertState.ENABLED)
        assert d.next_state is expected
        assert d.count == n

    # suspended -> eligible edge at 3/4 (never straight back to enabled)
    @pytest.mark.parametrize("n,expected", [
        (3, AlertState.ELIGIBLE),
        (4, AlertState.SUSPENDED),
        (6, AlertState.SUSPENDED),
        (7, AlertState.SUSPENDED),
    ])
    def test_suspended_edge(self, n: int, expected: AlertState) -> None:
        assert _gate(n, AlertState.SUSPENDED).next_state is expected

    @pytest.mark.parametrize("n", [0, 3, 4, 7])
    def test_never_auto_enabled(self, n: int) -> None:
        for state in (AlertState.OFF, AlertState.ELIGIBLE, AlertState.SUSPENDED):
            assert _gate(n, state).next_state is not AlertState.ENABLED

    def test_eligible_stays_while_ready(self) -> None:
        assert _gate(3, AlertState.ELIGIBLE).next_state is AlertState.ELIGIBLE

    def test_eligible_lapses_to_off(self) -> None:
        """Safer reading (ADR silent): eligibility lapses when it stops holding."""
        assert _gate(4, AlertState.ELIGIBLE).next_state is AlertState.OFF
        assert _gate(0, AlertState.ELIGIBLE, status="shadow").next_state is AlertState.OFF

    @pytest.mark.parametrize("status", ["shadow", "derived", "uncalibrated"])
    def test_not_armed(self, status: str) -> None:
        assert _gate(0, AlertState.OFF, status=status).next_state is AlertState.OFF
        d = _gate(0, AlertState.ENABLED, status=status)
        assert d.next_state is AlertState.SUSPENDED and "armed" in d.reason
        # Suspended does not become eligible while unarmed, even at count 0.
        assert _gate(0, AlertState.SUSPENDED, status=status).next_state is AlertState.SUSPENDED

    def test_min_oos_weeks_edge(self) -> None:
        w = pd.Timedelta(weeks=DRIFT_WATCH_ALERT_MIN_OOS_WEEKS)
        assert _gate(0, AlertState.OFF, cal_end=_AS_OF - w).next_state is AlertState.ELIGIBLE
        short = _AS_OF - w + pd.Timedelta(hours=1)
        d = _gate(0, AlertState.OFF, cal_end=short)
        assert d.next_state is AlertState.OFF and "out-of-sample" in d.reason

    def test_unknown_boundary_blocks_eligibility_and_suspends_enabled(self) -> None:
        assert _gate(0, AlertState.OFF, cal_end=None).next_state is AlertState.OFF
        assert _gate(0, AlertState.ENABLED, cal_end=None).next_state is AlertState.SUSPENDED

    def test_confirmed_drift_labels_bring_count_under_budget(self) -> None:
        eps = _eps(5)
        labels = {e.key: "confirmed_drift" for e in eps[:2]}
        d = evaluate_alert_gate(
            eps, labels, current_state=AlertState.OFF, input_status="armed",
            calibration_window_end=_CAL_END, as_of=_AS_OF,
            min_shift=_MIN_SHIFT, alerts_enabled=True,
        )
        assert d == (3, AlertState.ELIGIBLE, d.reason)

    @pytest.mark.parametrize("state", list(AlertState))
    def test_global_flag_off_forces_off(self, state: AlertState) -> None:
        d = _gate(0, state, enabled=False)
        assert d.next_state is AlertState.OFF and "globally" in d.reason

    def test_reason_always_present(self) -> None:
        for state in AlertState:
            for n in (0, 3, 4, 6, 7):
                assert _gate(n, state).reason

    def test_accepts_plain_string_state(self) -> None:
        assert _gate(0, "off").next_state is AlertState.ELIGIBLE  # type: ignore[arg-type]

    def test_rejects_no_hysteresis(self) -> None:
        with pytest.raises(ValueError):
            evaluate_alert_gate(
                [], {}, current_state=AlertState.OFF, input_status="armed",
                calibration_window_end=_CAL_END, as_of=_AS_OF,
                min_shift=_MIN_SHIFT, alerts_enabled=True,
                enable_max_count=5, suspend_min_count=5,
            )

