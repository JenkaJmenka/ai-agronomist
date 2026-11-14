"""Vendor-agnostic drift-watch: CUSUM on weather-removed residuals.

Inputs are canonical channel names (no vendor column names). Three residual
types are supported:

- **physics** -- humidity deficit vs Tetens(temperature, RH). Auto-classifies
  as *derived* (HD deterministically follows T+RH) or *independent* (genuine
  second measurement).
- **paired_probes** -- two channels measuring the same quantity, with optional
  covariates removed by a Ridge model.
- **virtual_sensor** -- a target channel predicted from a caller-supplied list
  of other channels plus hour of day.

The CUSUM accumulator tracks each direction independently: a negative
excursion before a positive fault does not mask the alarm. The existing
``CUSUMDriftDetector`` in this package couples directions (any alarm blocks
both until reset) and has no re-arm or onset estimation, so the accumulator
is reimplemented here.

All constants are imported from ``src.config`` (``DRIFT_WATCH_*``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from src.config import (
    DRIFT_WATCH_BASELINE_WEEKS,
    DRIFT_WATCH_GAP_WEEKS,
    DRIFT_WATCH_MIN_HISTORY_WEEKS,
    DRIFT_WATCH_FA_BUDGET_PER_MONTH,
    DRIFT_WATCH_DETECTION_RATE_THRESHOLD,
    DRIFT_WATCH_CALIBRATION_H_MAX,
    DRIFT_WATCH_REARM_FRACTION,
    DRIFT_WATCH_PHYSICS_DERIVED_SIGMA_CEIL,
    DRIFT_WATCH_SCHEMA_VERSION,
    DRIFT_WATCH_DEFAULT_SHIFT_TEMP_C,
    DRIFT_WATCH_DEFAULT_SHIFT_RH_PCT,
    DRIFT_WATCH_DEFAULT_SHIFT_HD_GM3,
    DRIFT_WATCH_INJECT_BIAS_LEVELS,
    DRIFT_WATCH_INJECT_DRIFT_RATE,
    DRIFT_WATCH_INJECT_DRIFT_WEEKS,
    DRIFT_WATCH_INJECT_N_STARTS,
)


# ── Public data classes ──────────────────────────────────────────────────

class InputKind(str, Enum):
    """Residual computation strategy."""
    PHYSICS = "physics"
    PAIRED_PROBES = "paired_probes"
    VIRTUAL_SENSOR = "virtual_sensor"


class InputStatus(str, Enum):
    """Readiness state after calibration."""
    ARMED = "armed"
    SHADOW = "shadow"
    DERIVED = "derived"


@dataclass(frozen=True)
class DriftInputSpec:
    """Caller-supplied specification for one drift input.

    Attributes:
        input_id: Unique identifier (e.g. ``"hd_physics"``).
        kind: Residual computation strategy.
        target_channel: The channel whose drift is being monitored.
        channels: Additional channels consumed by the residual model.
            - *physics*: ``[temperature_channel, rh_channel, hd_channel]``.
            - *paired_probes*: ``[second_probe_channel]``, with optional
              covariate channels appended.
            - *virtual_sensor*: predictor channels (target excluded).
        min_shift: Smallest shift worth detecting, in native units.
    """
    input_id: str
    kind: InputKind
    target_channel: str
    channels: List[str]
    min_shift: float


@dataclass
class DriftAlarm:
    """One alarm emitted by :meth:`DriftWatch.update`.

    Attributes:
        input_id: Which drift input fired.
        target_channel: The monitored channel.
        direction: ``"+"`` (positive shift) or ``"-"`` (negative shift).
        onset_estimate: Timestamp of the last time the CUSUM statistic was
            at zero before the alarm.
        detection_ts: Timestamp when the alarm fired.
        offset_native: Estimated offset in physical units at detection.
        peak_statistic: CUSUM statistic value at detection.
    """
    input_id: str
    target_channel: str
    direction: str
    onset_estimate: pd.Timestamp
    detection_ts: pd.Timestamp
    offset_native: float
    peak_statistic: float


@dataclass
class SelfTestResult:
    """Per-fault self-test outcome recorded during calibration."""
    fault_name: str
    detection_rate: float
    median_delay_hours: float


@dataclass
class CalibrationResult:
    """Serialisable outcome of :meth:`DriftWatch.calibrate`.

    Attributes:
        input_id: Identifier of the calibrated input.
        kind: Residual type.
        status: ``ARMED``, ``SHADOW``, or ``DERIVED``.
        k: CUSUM allowance parameter (sigma units).
        h: CUSUM decision interval (sigma units).
        scale_mu: Baseline residual mean.
        scale_sigma: Baseline residual standard deviation.
        fit_window_start: ISO timestamp of the fit window start.
        fit_window_end: ISO timestamp of the fit window end.
        model_coefficients: Ridge model weights (JSON-safe list), or ``None``
            for physics inputs.
        model_intercept: Ridge model intercept, or ``None``.
        model_feature_names: Ordered feature names for the Ridge model.
        model_fill_values: Per-feature fill values (training-set means) for
            NaN imputation during scoring, or ``None`` for physics inputs.
        measured_fa_per_month: False-alarm rate on calibration period.
        self_test: List of per-fault detection results.
        derived_evidence: If ``DERIVED``, the measured sigma and the ceiling.
    """
    input_id: str
    kind: str
    status: str
    k: float
    h: float
    scale_mu: float
    scale_sigma: float
    fit_window_start: str
    fit_window_end: str
    model_coefficients: Optional[List[float]]
    model_intercept: Optional[float]
    model_feature_names: Optional[List[str]]
    model_fill_values: Optional[List[float]]
    measured_fa_per_month: float
    self_test: List[Dict[str, Any]]
    derived_evidence: Optional[Dict[str, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CalibrationResult":
        """Reconstruct from a dictionary."""
        d = dict(d)
        d["self_test"] = [dict(s) for s in d.get("self_test", [])]
        # Backward compatibility: fill_values may be absent in old
        # serialised results.
        if "model_fill_values" not in d:
            d["model_fill_values"] = None
        return cls(**d)


@dataclass
class DriftInputState:
    """Serialisable per-input CUSUM state for :meth:`DriftWatch.update`.

    Attributes:
        schema_version: Format version for forward compatibility.
        input_id: Which input this state belongs to.
        s_pos: Positive-direction CUSUM accumulator.
        s_neg: Negative-direction CUSUM accumulator.
        pos_armed: Whether the positive direction can fire.
        neg_armed: Whether the negative direction can fire.
        pos_active: Whether a positive alarm is currently active.
        neg_active: Whether a negative alarm is currently active.
        last_zero_pos_ts: ISO timestamp of the last time ``s_pos`` was zero.
        last_zero_neg_ts: ISO timestamp of the last time ``s_neg`` was zero.
        last_processed_ts: ISO timestamp of the last hourly mean processed.
        calibration: The calibration result governing this input.
    """
    schema_version: int
    input_id: str
    s_pos: float = 0.0
    s_neg: float = 0.0
    pos_armed: bool = True
    neg_armed: bool = True
    pos_active: bool = False
    neg_active: bool = False
    last_zero_pos_ts: Optional[str] = None
    last_zero_neg_ts: Optional[str] = None
    last_processed_ts: Optional[str] = None
    calibration: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DriftInputState":
        """Reconstruct from a dictionary."""
        return cls(**d)


# ── Internal helpers ─────────────────────────────────────────────────────

def _tetens_hd(t_air: np.ndarray, rh: np.ndarray) -> np.ndarray:
    """Humidity deficit via the Tetens equation (vectorised).

    Uses the same coefficient (2167.0) as
    ``src.models.psychrometric_validator.calculate_humidity_deficit``.
    """
    e_s = 0.61078 * np.exp((17.27 * t_air) / (t_air + 237.3))
    e_a = e_s * (rh / 100.0)
    return (2167.0 * (e_s - e_a)) / (t_air + 273.15)


def _add_hour_features(df: pd.DataFrame) -> pd.DataFrame:
    """Append ``hour_sin`` and ``hour_cos`` columns derived from ``hour``."""
    out = df.copy()
    out["hour_sin"] = np.sin(2.0 * np.pi * out["hour"] / 24.0)
    out["hour_cos"] = np.cos(2.0 * np.pi * out["hour"] / 24.0)
    return out


def _fit_ridge(
    train: pd.DataFrame,
    features: List[str],
    target: str,
) -> Tuple[Ridge, List[str]]:
    """Fit a Ridge model and return (model, feature_names_with_hour)."""
    feat_cols = features + ["hour"]
    X = _add_hour_features(train[feat_cols])
    use_cols = features + ["hour_sin", "hour_cos"]
    X = X[use_cols]
    y = train[target]
    mask = y.notna() & X.notna().all(axis=1)
    model = Ridge(alpha=1.0)
    model.fit(X[mask], y[mask])
    return model, use_cols


def _predict_ridge(
    model: Ridge,
    df: pd.DataFrame,
    feature_names: List[str],
    fill_values: Optional[pd.Series] = None,
) -> pd.Series:
    """Predict using a fitted Ridge model."""
    feat_cols_input = [f for f in feature_names if f not in ("hour_sin", "hour_cos")]
    X = _add_hour_features(df[feat_cols_input + ["hour"]] if "hour" not in feat_cols_input else df[feat_cols_input])
    X = X[feature_names]
    if fill_values is not None:
        X = X.fillna(fill_values)
    else:
        X = X.fillna(0.0)
    return pd.Series(model.predict(X), index=df.index)


def _compute_residual_physics(
    df: pd.DataFrame,
    temp_col: str,
    rh_col: str,
    hd_col: str,
) -> pd.Series:
    """R1: measured HD minus Tetens-derived HD."""
    return df[hd_col] - _tetens_hd(df[temp_col].values, df[rh_col].values)


def _compute_residual_paired(
    df: pd.DataFrame,
    target_col: str,
    second_col: str,
    covariates: List[str],
    model: Ridge,
    feature_names: List[str],
    fill_values: Optional[pd.Series] = None,
) -> pd.Series:
    """R2: (target - second) minus Ridge prediction from covariates."""
    delta = df[target_col] - df[second_col]
    pred = _predict_ridge(model, df, feature_names, fill_values)
    return delta - pred


def _compute_residual_virtual(
    df: pd.DataFrame,
    target_col: str,
    model: Ridge,
    feature_names: List[str],
    fill_values: Optional[pd.Series] = None,
) -> pd.Series:
    """R3/R4: measured target minus Ridge prediction from other channels."""
    pred = _predict_ridge(model, df, feature_names, fill_values)
    return df[target_col] - pred


# ── Main class ───────────────────────────────────────────────────────────

class DriftWatch:
    """Vendor-agnostic drift detector driven by CUSUM on weather-removed residuals.

    Lifecycle:
        1. ``calibrate(hourly_df, spec)`` -- fit residual model, choose h,
           self-test, return ``CalibrationResult``.
        2. ``init_state(calibration)`` -- create a fresh ``DriftInputState``.
        3. ``update(state, hourly_df)`` -- score new data, return alarms.
        4. Persist ``state.to_dict()`` between calls.

    All time columns are expected to be timezone-aware UTC.
    """

    # ── Calibration ──────────────────────────────────────────────────────

    @staticmethod
    def calibrate(
        hourly_df: pd.DataFrame,
        spec: DriftInputSpec,
        *,
        baseline_weeks: int = DRIFT_WATCH_BASELINE_WEEKS,
        gap_weeks: int = DRIFT_WATCH_GAP_WEEKS,
        fa_budget: float = DRIFT_WATCH_FA_BUDGET_PER_MONTH,
        detection_threshold: float = DRIFT_WATCH_DETECTION_RATE_THRESHOLD,
    ) -> CalibrationResult:
        """Fit residual model, calibrate CUSUM threshold, and self-test.

        Args:
            hourly_df: DataFrame indexed by timezone-aware UTC timestamps at
                hourly cadence, containing all channels named in *spec* plus
                an ``hour`` column (0-23). Must span at least
                ``DRIFT_WATCH_MIN_HISTORY_WEEKS`` weeks.
            spec: Input specification.
            baseline_weeks: Weeks of data for model fitting.
            gap_weeks: Gap between fit window and calibration window.
            fa_budget: Target false alarms per month on the calibration window.
            detection_threshold: Minimum detection rate to arm.

        Returns:
            ``CalibrationResult`` with status ARMED, SHADOW, or DERIVED.

        Raises:
            ValueError: If the data is too short.
        """
        hourly_df = hourly_df.copy()
        if "hour" not in hourly_df.columns:
            hourly_df["hour"] = hourly_df.index.hour

        total_weeks = (hourly_df.index[-1] - hourly_df.index[0]).days / 7.0
        min_weeks = DRIFT_WATCH_MIN_HISTORY_WEEKS
        if total_weeks < min_weeks:
            raise ValueError(
                f"Need at least {min_weeks} weeks of hourly data, got {total_weeks:.1f}"
            )

        t0 = hourly_df.index[0]
        fit_end = t0 + pd.Timedelta(weeks=baseline_weeks)
        cal_start = fit_end + pd.Timedelta(weeks=gap_weeks)
        cal_end = cal_start + pd.Timedelta(weeks=baseline_weeks)

        fit_mask = hourly_df.index < fit_end
        cal_mask = (hourly_df.index >= cal_start) & (hourly_df.index < cal_end)
        fit_data = hourly_df[fit_mask]
        cal_data = hourly_df[cal_mask]

        if len(fit_data) < 100 or len(cal_data) < 100:
            raise ValueError("Fit or calibration window too short after filtering")

        # ── Compute residuals ────────────────────────────────────────────
        model: Optional[Ridge] = None
        feat_names: Optional[List[str]] = None
        fill_vals: Optional[pd.Series] = None

        if spec.kind == InputKind.PHYSICS:
            # channels: [temp, rh, hd]
            temp_c, rh_c, hd_c = spec.channels[0], spec.channels[1], spec.channels[2]
            r_fit = _compute_residual_physics(fit_data, temp_c, rh_c, hd_c)
            r_cal = _compute_residual_physics(cal_data, temp_c, rh_c, hd_c)

        elif spec.kind == InputKind.PAIRED_PROBES:
            second_col = spec.channels[0]
            covariates = spec.channels[1:]
            # Target for ridge: target_channel - second_col
            fit_aug = fit_data.copy()
            fit_aug["__delta"] = fit_aug[spec.target_channel] - fit_aug[second_col]
            model, feat_names = _fit_ridge(fit_aug, covariates, "__delta")
            # Compute fill values from training data
            feat_input = [f for f in feat_names if f not in ("hour_sin", "hour_cos")]
            X_fit = _add_hour_features(fit_aug[feat_input + ["hour"]] if "hour" not in feat_input else fit_aug[feat_input])
            fill_vals = X_fit[feat_names].mean()

            r_fit = _compute_residual_paired(
                fit_data, spec.target_channel, second_col,
                covariates, model, feat_names, fill_vals,
            )
            r_cal = _compute_residual_paired(
                cal_data, spec.target_channel, second_col,
                covariates, model, feat_names, fill_vals,
            )

        elif spec.kind == InputKind.VIRTUAL_SENSOR:
            model, feat_names = _fit_ridge(fit_data, spec.channels, spec.target_channel)
            feat_input = [f for f in feat_names if f not in ("hour_sin", "hour_cos")]
            X_fit = _add_hour_features(fit_data[feat_input + ["hour"]] if "hour" not in feat_input else fit_data[feat_input])
            fill_vals = X_fit[feat_names].mean()

            r_fit = _compute_residual_virtual(
                fit_data, spec.target_channel, model, feat_names, fill_vals,
            )
            r_cal = _compute_residual_virtual(
                cal_data, spec.target_channel, model, feat_names, fill_vals,
            )
        else:
            raise ValueError(f"Unknown input kind: {spec.kind}")

        # ── Physics independence test ────────────────────────────────────
        derived_evidence: Optional[Dict[str, float]] = None
        if spec.kind == InputKind.PHYSICS:
            sigma = r_fit.dropna().std()
            ceil = DRIFT_WATCH_PHYSICS_DERIVED_SIGMA_CEIL
            if sigma <= ceil:
                derived_evidence = {"measured_sigma": float(sigma), "ceiling": ceil}
                return CalibrationResult(
                    input_id=spec.input_id,
                    kind=spec.kind.value,
                    status=InputStatus.DERIVED.value,
                    k=0.0, h=0.0,
                    scale_mu=float(r_fit.dropna().mean()),
                    scale_sigma=float(sigma),
                    fit_window_start=str(fit_data.index[0]),
                    fit_window_end=str(fit_data.index[-1]),
                    model_coefficients=None,
                    model_intercept=None,
                    model_feature_names=None,
                    model_fill_values=None,
                    measured_fa_per_month=0.0,
                    self_test=[],
                    derived_evidence=derived_evidence,
                )

        # ── Scale from fit window ────────────────────────────────────────
        mu = float(r_fit.dropna().mean())
        sigma = float(r_fit.dropna().std())
        if sigma < 1e-9:
            sigma = 1e-9

        k = (spec.min_shift / sigma) / 2.0

        # ── Calibrate h ──────────────────────────────────────────────────
        z_cal = ((r_cal.dropna() - mu) / sigma).values
        cal_months = max((cal_data.index[-1] - cal_data.index[0]).days / 30.44, 0.5)

        lo, hi = 1.0, DRIFT_WATCH_CALIBRATION_H_MAX
        best_h = hi
        for _ in range(50):
            mid = (lo + hi) / 2.0
            n_fa = len(_run_cusum_alarms(z_cal, k, mid))
            if n_fa / cal_months > fa_budget:
                lo = mid
            else:
                hi = mid
                best_h = mid
        h = best_h

        measured_fa = len(_run_cusum_alarms(z_cal, k, h))
        fa_per_month = measured_fa / cal_months

        # ── Self-test (inject into calibration period) ───────────────────
        self_test_results: List[SelfTestResult] = []
        test_status = InputStatus.ARMED

        # Determine injection channel and fault list
        faults = _build_fault_list(spec)
        n_starts = DRIFT_WATCH_INJECT_N_STARTS
        inject_starts = _spread_starts(cal_data.index, n_starts)

        for fault_name, channel, ftype, magnitude, dur_wk in faults:
            detections = 0
            delays: List[float] = []
            for start in inject_starts:
                df_inj = _inject_fault(cal_data, channel, ftype, magnitude, start, dur_wk)
                r_inj = _recompute_residual(
                    df_inj, spec, model, feat_names, fill_vals,
                )
                z_inj = ((r_inj.dropna() - mu) / sigma).values
                z_idx = r_inj.dropna().index
                alarms = _run_cusum_alarms(z_inj, k, h)
                # Find first alarm after injection start
                start_pos = _find_index_at_or_after(z_idx, start)
                if start_pos is not None:
                    for a in alarms:
                        if a["idx"] >= start_pos:
                            detections += 1
                            delay_h = (z_idx[a["idx"]] - start).total_seconds() / 3600
                            delays.append(delay_h)
                            break

            det_rate = detections / max(len(inject_starts), 1)
            med_delay = float(np.median(delays)) if delays else float("nan")
            self_test_results.append(SelfTestResult(
                fault_name=fault_name,
                detection_rate=det_rate,
                median_delay_hours=med_delay,
            ))
            if det_rate < detection_threshold:
                test_status = InputStatus.SHADOW

        if fa_per_month > fa_budget:
            test_status = InputStatus.SHADOW

        # ── Build result ─────────────────────────────────────────────────
        coefs: Optional[List[float]] = None
        intercept: Optional[float] = None
        feat_list: Optional[List[str]] = None
        fill_list: Optional[List[float]] = None
        if model is not None:
            coefs = model.coef_.tolist()
            intercept = float(model.intercept_)
            feat_list = feat_names
        if fill_vals is not None:
            fill_list = fill_vals.tolist()

        return CalibrationResult(
            input_id=spec.input_id,
            kind=spec.kind.value,
            status=test_status.value,
            k=k, h=h,
            scale_mu=mu, scale_sigma=sigma,
            fit_window_start=str(fit_data.index[0]),
            fit_window_end=str(fit_data.index[-1]),
            model_coefficients=coefs,
            model_intercept=intercept,
            model_feature_names=feat_list,
            model_fill_values=fill_list,
            measured_fa_per_month=fa_per_month,
            self_test=[asdict(s) for s in self_test_results],
            derived_evidence=derived_evidence,
        )

    # ── State management ─────────────────────────────────────────────────

    @staticmethod
    def init_state(calibration: CalibrationResult) -> DriftInputState:
        """Create a fresh CUSUM state from a calibration result.

        Args:
            calibration: The calibration governing this input.

        Returns:
            A zeroed ``DriftInputState`` ready for :meth:`update`.
        """
        return DriftInputState(
            schema_version=DRIFT_WATCH_SCHEMA_VERSION,
            input_id=calibration.input_id,
            calibration=calibration.to_dict(),
        )

    # ── Scoring ──────────────────────────────────────────────────────────

    @staticmethod
    def update(
        state: DriftInputState,
        hourly_df: pd.DataFrame,
        spec: DriftInputSpec,
    ) -> List[DriftAlarm]:
        """Score new hourly data and return any alarms.

        Consecutive calls with adjacent time ranges continue the CUSUM
        seamlessly. Rows at or before ``state.last_processed_ts`` are
        silently skipped (idempotent on overlapping uploads).

        Args:
            state: Mutable state object (updated in place).
            hourly_df: New hourly-mean data (same schema as calibrate).
            spec: The input specification.

        Returns:
            List of ``DriftAlarm`` objects (may be empty).
        """
        cal = CalibrationResult.from_dict(state.calibration)  # type: ignore[arg-type]
        if cal.status == InputStatus.DERIVED.value:
            return []

        hourly_df = hourly_df.copy()
        if "hour" not in hourly_df.columns:
            hourly_df["hour"] = hourly_df.index.hour

        # Skip already-processed rows
        if state.last_processed_ts is not None:
            cutoff = pd.Timestamp(state.last_processed_ts)
            hourly_df = hourly_df[hourly_df.index > cutoff]
        if len(hourly_df) == 0:
            return []

        # Reconstruct model if needed
        model: Optional[Ridge] = None
        feat_names: Optional[List[str]] = None
        fill_vals: Optional[pd.Series] = None
        if cal.model_coefficients is not None:
            model = Ridge(alpha=1.0)
            model.coef_ = np.array(cal.model_coefficients)
            model.intercept_ = cal.model_intercept
            model.n_features_in_ = len(cal.model_coefficients)
            feat_names = cal.model_feature_names
            # Use stored fill values from calibration (training-set means)
            if cal.model_fill_values is not None and feat_names is not None:
                fill_vals = pd.Series(cal.model_fill_values, index=feat_names)
            else:
                fill_vals = pd.Series(0.0, index=feat_names)

        # Compute residual
        residual = _recompute_residual(hourly_df, spec, model, feat_names, fill_vals)
        residual = residual.dropna()
        if len(residual) == 0:
            return []

        mu = cal.scale_mu
        sigma = cal.scale_sigma
        k = cal.k
        h = cal.h
        rearm = DRIFT_WATCH_REARM_FRACTION

        alarms: List[DriftAlarm] = []

        for i, (ts, val) in enumerate(residual.items()):
            z = (val - mu) / sigma

            # Update accumulators
            state.s_pos = max(0.0, state.s_pos + z - k)
            state.s_neg = max(0.0, state.s_neg - z - k)

            # Track last-zero timestamps
            if state.s_pos <= 0.0:
                state.last_zero_pos_ts = str(ts)
            if state.s_neg <= 0.0:
                state.last_zero_neg_ts = str(ts)

            # Positive direction
            if state.pos_active:
                if state.s_pos < h * rearm:
                    state.pos_active = False
                    state.pos_armed = True
            elif state.pos_armed and state.s_pos > h:
                onset = (pd.Timestamp(state.last_zero_pos_ts)
                         if state.last_zero_pos_ts else ts)
                alarms.append(DriftAlarm(
                    input_id=spec.input_id,
                    target_channel=spec.target_channel,
                    direction="+",
                    onset_estimate=onset,
                    detection_ts=ts,
                    offset_native=z * sigma,
                    peak_statistic=state.s_pos,
                ))
                state.pos_active = True
                state.pos_armed = False

            # Negative direction
            if state.neg_active:
                if state.s_neg < h * rearm:
                    state.neg_active = False
                    state.neg_armed = True
            elif state.neg_armed and state.s_neg > h:
                onset = (pd.Timestamp(state.last_zero_neg_ts)
                         if state.last_zero_neg_ts else ts)
                alarms.append(DriftAlarm(
                    input_id=spec.input_id,
                    target_channel=spec.target_channel,
                    direction="-",
                    onset_estimate=onset,
                    detection_ts=ts,
                    offset_native=-z * sigma,
                    peak_statistic=state.s_neg,
                ))
                state.neg_active = True
                state.neg_armed = False

        state.last_processed_ts = str(residual.index[-1])
        return alarms


# ── Internal CUSUM (alarm-list only, for calibration) ────────────────────

def _run_cusum_alarms(
    z: np.ndarray,
    k: float,
    h: float,
    rearm_frac: float = DRIFT_WATCH_REARM_FRACTION,
) -> List[Dict[str, Any]]:
    """Two-sided CUSUM with independent per-direction alarm tracking.

    Returns a list of alarm dicts with ``idx``, ``dir``, ``onset_est``.
    """
    n = len(z)
    if n == 0:
        return []
    sp = 0.0
    sn = 0.0
    alarms: List[Dict[str, Any]] = []
    p_active = False
    n_active = False
    p_armed = True
    n_armed = True
    last_zero_p = 0
    last_zero_n = 0

    for i in range(n):
        sp = max(0.0, sp + z[i] - k)
        sn = max(0.0, sn - z[i] - k)

        if sp <= 0.0:
            last_zero_p = i
        if sn <= 0.0:
            last_zero_n = i

        if p_active:
            if sp < h * rearm_frac:
                p_active = False
                p_armed = True
        elif p_armed and sp > h:
            alarms.append({"idx": i, "dir": "+", "onset_est": last_zero_p})
            p_active = True
            p_armed = False

        if n_active:
            if sn < h * rearm_frac:
                n_active = False
                n_armed = True
        elif n_armed and sn > h:
            alarms.append({"idx": i, "dir": "-", "onset_est": last_zero_n})
            n_active = True
            n_armed = False

    return alarms


# ── Fault injection helpers (calibration self-test) ──────────────────────

def _build_fault_list(
    spec: DriftInputSpec,
) -> List[Tuple[str, str, str, float, int]]:
    """Build the list of (name, channel, type, magnitude, weeks) for self-test."""
    faults: List[Tuple[str, str, float, float, int]] = []
    ch = spec.target_channel
    for lvl in DRIFT_WATCH_INJECT_BIAS_LEVELS:
        faults.append((f"bias +{lvl}", ch, "bias", lvl * spec.min_shift, 4))
    faults.append((
        "drift",
        ch,
        "drift",
        DRIFT_WATCH_INJECT_DRIFT_RATE * spec.min_shift,
        DRIFT_WATCH_INJECT_DRIFT_WEEKS,
    ))
    return faults  # type: ignore[return-value]


def _spread_starts(
    index: pd.DatetimeIndex,
    n: int,
) -> List[pd.Timestamp]:
    """Evenly spread *n* injection start points across the middle of *index*."""
    t0 = index[0]
    t1 = index[-1]
    margin = (t1 - t0) * 0.1
    start = t0 + margin
    end = t1 - margin
    if n <= 1:
        return [start]
    step = (end - start) / (n - 1)
    return [start + i * step for i in range(n)]


def _inject_fault(
    df: pd.DataFrame,
    channel: str,
    fault_type: str,
    magnitude: float,
    start: pd.Timestamp,
    duration_weeks: int,
) -> pd.DataFrame:
    """Return a copy of *df* with a fault injected into *channel*."""
    out = df.copy()
    end = start + pd.Timedelta(weeks=duration_weeks)
    mask = (out.index >= start) & (out.index < end)
    if fault_type == "bias":
        out.loc[mask, channel] += magnitude
    elif fault_type == "drift":
        hours = (out.index[mask] - start).total_seconds() / 3600
        out.loc[mask, channel] += magnitude * hours / (7 * 24)
    return out


def _recompute_residual(
    df: pd.DataFrame,
    spec: DriftInputSpec,
    model: Optional[Ridge],
    feat_names: Optional[List[str]],
    fill_vals: Optional[pd.Series],
) -> pd.Series:
    """Compute the residual for *spec* on (possibly faulted) *df*."""
    if spec.kind == InputKind.PHYSICS:
        temp_c, rh_c, hd_c = spec.channels[0], spec.channels[1], spec.channels[2]
        return _compute_residual_physics(df, temp_c, rh_c, hd_c)
    elif spec.kind == InputKind.PAIRED_PROBES:
        second_col = spec.channels[0]
        return _compute_residual_paired(
            df, spec.target_channel, second_col,
            spec.channels[1:], model, feat_names, fill_vals,  # type: ignore[arg-type]
        )
    elif spec.kind == InputKind.VIRTUAL_SENSOR:
        return _compute_residual_virtual(
            df, spec.target_channel, model, feat_names, fill_vals,  # type: ignore[arg-type]
        )
    raise ValueError(f"Unknown kind: {spec.kind}")


def _find_index_at_or_after(
    index: pd.DatetimeIndex,
    ts: pd.Timestamp,
) -> Optional[int]:
    """Return the integer position of the first element >= *ts*, or None."""
    pos = index.searchsorted(ts)
    if pos >= len(index):
        return None
    return int(pos)
