"""Vendor-agnostic drift-watch: CUSUM on weather-removed residuals.

Inputs are canonical channel names (no vendor column names). Three residual
types are supported:

- **physics** -- humidity deficit vs Tetens(temperature, RH). Auto-classifies
  as *derived* (HD deterministically follows T+RH) or *independent* (genuine
  second measurement). Always uses a frozen baseline.
- **paired_probes** -- two channels measuring the same quantity, with optional
  covariates removed by a Ridge model. Rolling baseline by default.
- **virtual_sensor** -- a target channel predicted from a caller-supplied list
  of other channels plus hour of day. Rolling baseline by default.

Episode-based excursion logic (2026-11-15): after a CUSUM alarm, the
accumulator resets to zero and keeps accumulating. Consecutive alarms
within ``DRIFT_WATCH_QUIET_HOURS`` are ONE ongoing episode. A new episode
starts only after the direction has been quiet for that many hours. This
ensures a past alarm can never mask a later fault.

Rolling baseline (2026-11-15): for model-based inputs, the Ridge model
and residual scale (mu, sigma) are refit on the trailing baseline window
every ``DRIFT_WATCH_REFIT_INTERVAL_WEEKS`` weeks. Hours with elevated
z-scores (``|z| > DRIFT_WATCH_ELEVATED_Z_CEIL``) are excluded from the
refit to prevent fault contamination. The CUSUM parameters k and h are
kept from the initial calibration; only the model and scale adapt.

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
    DRIFT_WATCH_PHYSICS_DERIVED_SIGMA_CEIL,
    DRIFT_WATCH_SCHEMA_VERSION,
    DRIFT_WATCH_DEFAULT_SHIFT_TEMP_C,
    DRIFT_WATCH_DEFAULT_SHIFT_RH_PCT,
    DRIFT_WATCH_DEFAULT_SHIFT_HD_GM3,
    DRIFT_WATCH_INJECT_BIAS_LEVELS,
    DRIFT_WATCH_INJECT_DRIFT_RATE,
    DRIFT_WATCH_INJECT_DRIFT_WEEKS,
    DRIFT_WATCH_INJECT_N_STARTS,
    DRIFT_WATCH_QUIET_HOURS,
    DRIFT_WATCH_REFIT_INTERVAL_WEEKS,
    DRIFT_WATCH_ELEVATED_Z_CEIL,
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
class DriftEpisode:
    """One episode event emitted by :meth:`DriftWatch.update`.

    An episode groups consecutive CUSUM alarms in the same direction
    that are separated by fewer than ``DRIFT_WATCH_QUIET_HOURS`` hours.
    The first event in an episode has ``is_new=True``; subsequent
    confirmations have ``is_new=False``.

    Attributes:
        input_id: Which drift input fired.
        target_channel: The monitored channel.
        direction: ``"+"`` (positive shift) or ``"-"`` (negative shift).
        episode_id: Stable identifier for this episode.
        first_detected_ts: When the episode was first detected.
        last_confirmed_ts: When the episode was last confirmed (updated).
        onset_estimate: Timestamp of the last time the CUSUM statistic
            was at zero before the current alarm.
        current_offset_native: Estimated offset in physical units.
        peak_statistic: CUSUM statistic value at detection.
        is_new: ``True`` on first detection, ``False`` on continuation.
    """
    input_id: str
    target_channel: str
    direction: str
    episode_id: str
    first_detected_ts: pd.Timestamp
    last_confirmed_ts: pd.Timestamp
    onset_estimate: pd.Timestamp
    current_offset_native: float
    peak_statistic: float
    is_new: bool


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
        ceiling_hit: Whether h reached ``DRIFT_WATCH_CALIBRATION_H_MAX``
            during binary search (i.e. the FA budget was not met).
        shadow_reason: Human-readable reason if status is ``SHADOW``.
        gap_weeks: Gap used during calibration (stored so rolling refit
            can use the same value).
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
    ceiling_hit: bool = False
    shadow_reason: Optional[str] = None
    gap_weeks: int = DRIFT_WATCH_GAP_WEEKS

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CalibrationResult":
        """Reconstruct from a dictionary."""
        d = dict(d)
        d["self_test"] = [dict(s) for s in d.get("self_test", [])]
        # Backward compatibility
        if "model_fill_values" not in d:
            d["model_fill_values"] = None
        if "ceiling_hit" not in d:
            d["ceiling_hit"] = False
        if "shadow_reason" not in d:
            d["shadow_reason"] = None
        if "gap_weeks" not in d:
            d["gap_weeks"] = DRIFT_WATCH_GAP_WEEKS
        return cls(**d)


@dataclass
class DriftInputState:
    """Serialisable per-input CUSUM state for :meth:`DriftWatch.update`.

    Attributes:
        schema_version: Format version for forward compatibility.
        input_id: Which input this state belongs to.
        s_pos: Positive-direction CUSUM accumulator.
        s_neg: Negative-direction CUSUM accumulator.
        last_zero_pos_ts: ISO timestamp of the last time ``s_pos`` was zero.
        last_zero_neg_ts: ISO timestamp of the last time ``s_neg`` was zero.
        last_processed_ts: ISO timestamp of the last hourly mean processed.
        calibration: The calibration result governing this input.
        pos_episode_id: Current positive-direction episode ID (``None``
            if no active episode).
        pos_episode_start: ISO timestamp of the current positive episode's
            first detection.
        pos_last_alarm_ts: ISO timestamp of the last positive alarm.
        neg_episode_id: Current negative-direction episode ID.
        neg_episode_start: ISO timestamp of the current negative episode's
            first detection.
        neg_last_alarm_ts: ISO timestamp of the last negative alarm.
        next_episode_seq: Monotonic counter for generating episode IDs.
        last_refit_ts: ISO timestamp of the last rolling refit.
    """
    schema_version: int
    input_id: str
    s_pos: float = 0.0
    s_neg: float = 0.0
    last_zero_pos_ts: Optional[str] = None
    last_zero_neg_ts: Optional[str] = None
    last_processed_ts: Optional[str] = None
    calibration: Optional[Dict[str, Any]] = None
    # Episode tracking
    pos_episode_id: Optional[str] = None
    pos_episode_start: Optional[str] = None
    pos_last_alarm_ts: Optional[str] = None
    neg_episode_id: Optional[str] = None
    neg_episode_start: Optional[str] = None
    neg_last_alarm_ts: Optional[str] = None
    next_episode_seq: int = 0
    # Rolling refit
    last_refit_ts: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DriftInputState":
        """Reconstruct from a dictionary."""
        d = dict(d)
        # Backward compatibility: v1 states lack episode/rolling fields
        for fld, default in [
            ("pos_episode_id", None), ("pos_episode_start", None),
            ("pos_last_alarm_ts", None), ("neg_episode_id", None),
            ("neg_episode_start", None), ("neg_last_alarm_ts", None),
            ("next_episode_seq", 0), ("last_refit_ts", None),
        ]:
            if fld not in d:
                d[fld] = default
        # Drop v1 fields that no longer exist
        for old in ("pos_armed", "neg_armed", "pos_active", "neg_active"):
            d.pop(old, None)
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


def _reconstruct_model(
    cal: CalibrationResult,
) -> Tuple[Optional[Ridge], Optional[List[str]], Optional[pd.Series]]:
    """Rebuild a Ridge model from calibration state."""
    if cal.model_coefficients is None:
        return None, None, None
    model = Ridge(alpha=1.0)
    model.coef_ = np.array(cal.model_coefficients)
    model.intercept_ = cal.model_intercept
    model.n_features_in_ = len(cal.model_coefficients)
    feat_names = cal.model_feature_names
    fill_vals: Optional[pd.Series] = None
    if cal.model_fill_values is not None and feat_names is not None:
        fill_vals = pd.Series(cal.model_fill_values, index=feat_names)
    elif feat_names is not None:
        fill_vals = pd.Series(0.0, index=feat_names)
    return model, feat_names, fill_vals


# ── Rolling refit ────────────────────────────────────────────────────────

def _rolling_refit(
    state: DriftInputState,
    hourly_df: pd.DataFrame,
    spec: DriftInputSpec,
) -> None:
    """Refit the Ridge model and residual scale on the trailing baseline.

    Modifies *state.calibration* in place with updated model coefficients,
    intercept, fill values, mu, sigma, and fit window. The CUSUM parameters
    k and h are preserved from the initial calibration.

    The refit excludes hours where the residual z-score (on the current
    model) exceeds ``DRIFT_WATCH_ELEVATED_Z_CEIL``, preventing fault
    contamination of the baseline.
    """
    cal = CalibrationResult.from_dict(state.calibration)  # type: ignore[arg-type]
    gap_wk = cal.gap_weeks

    # Determine baseline window: trailing BASELINE_WEEKS ending gap_wk
    # before the scoring start (last_processed_ts).
    scoring_start = pd.Timestamp(state.last_processed_ts)
    bl_end = scoring_start - pd.Timedelta(weeks=gap_wk)
    bl_start = bl_end - pd.Timedelta(weeks=DRIFT_WATCH_BASELINE_WEEKS)

    baseline = hourly_df[(hourly_df.index >= bl_start) & (hourly_df.index < bl_end)].copy()
    if "hour" not in baseline.columns:
        baseline["hour"] = baseline.index.hour

    if len(baseline) < 200:
        return  # Not enough baseline data; keep current model

    # Pass 1: compute residuals with current model to identify outliers
    model_cur, feat_cur, fill_cur = _reconstruct_model(cal)
    residual_cur = _recompute_residual(baseline, spec, model_cur, feat_cur, fill_cur)
    r_vals = residual_cur.dropna()
    if len(r_vals) < 100:
        return

    mu_pre = float(r_vals.mean())
    sigma_pre = float(r_vals.std())
    if sigma_pre < 1e-9:
        return

    z_pre = (r_vals - mu_pre) / sigma_pre
    clean_mask = z_pre.abs() <= DRIFT_WATCH_ELEVATED_Z_CEIL
    clean_idx = r_vals[clean_mask].index

    # Need enough clean hours to fit a stable model
    if len(clean_idx) < 100:
        return

    clean_baseline = baseline.loc[clean_idx]

    # Pass 2: refit model on clean baseline
    if spec.kind == InputKind.PAIRED_PROBES:
        second_col = spec.channels[0]
        covariates = spec.channels[1:]
        bl_aug = clean_baseline.copy()
        bl_aug["__delta"] = bl_aug[spec.target_channel] - bl_aug[second_col]
        new_model, new_feat = _fit_ridge(bl_aug, covariates, "__delta")
    elif spec.kind == InputKind.VIRTUAL_SENSOR:
        new_model, new_feat = _fit_ridge(
            clean_baseline, spec.channels, spec.target_channel,
        )
    else:
        return  # Physics: no model to refit

    # Compute fill values from clean training data
    feat_input = [f for f in new_feat if f not in ("hour_sin", "hour_cos")]
    hcols = feat_input + ["hour"] if "hour" not in feat_input else feat_input
    X_fit = _add_hour_features(clean_baseline[hcols])
    new_fill = X_fit[new_feat].mean()

    # Compute new residuals and scale on clean baseline
    new_fill_series = pd.Series(new_fill.values, index=new_feat)
    new_residual = _recompute_residual(
        clean_baseline, spec, new_model, new_feat, new_fill_series,
    )
    r_clean = new_residual.dropna()
    if len(r_clean) < 50:
        return

    new_mu = float(r_clean.mean())
    new_sigma = float(r_clean.std())
    if new_sigma < 1e-9:
        return

    # Update calibration in state (preserve k, h from initial calibration)
    cal_dict = dict(state.calibration)  # type: ignore[arg-type]
    cal_dict["scale_mu"] = new_mu
    cal_dict["scale_sigma"] = new_sigma
    cal_dict["model_coefficients"] = new_model.coef_.tolist()
    cal_dict["model_intercept"] = float(new_model.intercept_)
    cal_dict["model_feature_names"] = new_feat
    cal_dict["model_fill_values"] = new_fill.tolist()
    cal_dict["fit_window_start"] = str(clean_baseline.index[0])
    cal_dict["fit_window_end"] = str(clean_baseline.index[-1])
    state.calibration = cal_dict
    state.last_refit_ts = str(scoring_start)


# ── Main class ───────────────────────────────────────────────────────────

class DriftWatch:
    """Vendor-agnostic drift detector driven by CUSUM on weather-removed residuals.

    Lifecycle:
        1. ``calibrate(hourly_df, spec)`` -- fit residual model, choose h,
           self-test, return ``CalibrationResult``.
        2. ``init_state(calibration)`` -- create a fresh ``DriftInputState``.
        3. ``update(state, hourly_df, spec)`` -- score new data, return episodes.
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
            temp_c, rh_c, hd_c = spec.channels[0], spec.channels[1], spec.channels[2]
            r_fit = _compute_residual_physics(fit_data, temp_c, rh_c, hd_c)
            r_cal = _compute_residual_physics(cal_data, temp_c, rh_c, hd_c)

        elif spec.kind == InputKind.PAIRED_PROBES:
            second_col = spec.channels[0]
            covariates = spec.channels[1:]
            fit_aug = fit_data.copy()
            fit_aug["__delta"] = fit_aug[spec.target_channel] - fit_aug[second_col]
            model, feat_names = _fit_ridge(fit_aug, covariates, "__delta")
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
                    gap_weeks=gap_weeks,
                )

        # ── Scale from fit window ────────────────────────────────────────
        mu = float(r_fit.dropna().mean())
        sigma = float(r_fit.dropna().std())
        if sigma < 1e-9:
            sigma = 1e-9

        k = (spec.min_shift / sigma) / 2.0

        # ── Calibrate h (episode-based counting) ────────────────────────
        z_cal = ((r_cal.dropna() - mu) / sigma).values
        cal_months = max((cal_data.index[-1] - cal_data.index[0]).days / 30.44, 0.5)

        lo, hi = 1.0, DRIFT_WATCH_CALIBRATION_H_MAX
        best_h = hi
        for _ in range(50):
            mid = (lo + hi) / 2.0
            episodes = _run_cusum_episodes(z_cal, k, mid, DRIFT_WATCH_QUIET_HOURS)
            n_ep = sum(1 for e in episodes if e["is_new"])
            if n_ep / cal_months > fa_budget:
                lo = mid
            else:
                hi = mid
                best_h = mid
        h = best_h

        # Check calibration ceiling
        ceiling_hit = abs(h - DRIFT_WATCH_CALIBRATION_H_MAX) < 0.1
        shadow_reason: Optional[str] = None

        measured_episodes = _run_cusum_episodes(z_cal, k, h, DRIFT_WATCH_QUIET_HOURS)
        measured_fa = sum(1 for e in measured_episodes if e["is_new"])
        fa_per_month = measured_fa / cal_months

        # ── Self-test (inject into calibration period) ───────────────────
        self_test_results: List[SelfTestResult] = []
        test_status = InputStatus.ARMED

        if ceiling_hit:
            test_status = InputStatus.SHADOW
            shadow_reason = "budget not met within search range"

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
                episodes = _run_cusum_episodes(z_inj, k, h, DRIFT_WATCH_QUIET_HOURS)
                start_pos = _find_index_at_or_after(z_idx, start)
                if start_pos is not None:
                    for ep in episodes:
                        if ep["is_new"] and ep["idx"] >= start_pos:
                            detections += 1
                            delay_h = (z_idx[ep["idx"]] - start).total_seconds() / 3600
                            delays.append(delay_h)
                            break

            det_rate = detections / max(len(inject_starts), 1)
            med_delay = float(np.median(delays)) if delays else float("nan")
            self_test_results.append(SelfTestResult(
                fault_name=fault_name,
                detection_rate=det_rate,
                median_delay_hours=med_delay,
            ))
            if det_rate < detection_threshold and not ceiling_hit:
                test_status = InputStatus.SHADOW
                if shadow_reason is None:
                    shadow_reason = (
                        f"detection rate {det_rate:.0%} < "
                        f"{detection_threshold:.0%} for {fault_name}"
                    )

        if fa_per_month > fa_budget and not ceiling_hit:
            test_status = InputStatus.SHADOW
            if shadow_reason is None:
                shadow_reason = (
                    f"FA rate {fa_per_month:.2f}/mo > "
                    f"budget {fa_budget:.1f}/mo"
                )

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
            ceiling_hit=ceiling_hit,
            shadow_reason=shadow_reason,
            gap_weeks=gap_weeks,
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
    ) -> List[DriftEpisode]:
        """Score new hourly data and return any episode events.

        For model-based inputs (paired_probes, virtual_sensor), the method
        automatically performs a rolling refit of the Ridge model and
        residual scale when ``DRIFT_WATCH_REFIT_INTERVAL_WEEKS`` have
        elapsed since the last refit. The caller should provide enough
        historical data in *hourly_df* for the refit (at least
        ``DRIFT_WATCH_BASELINE_WEEKS`` weeks before the new data).

        Rows at or before ``state.last_processed_ts`` are used only for
        rolling refit context; they are not re-scored.

        Args:
            state: Mutable state object (updated in place).
            hourly_df: Hourly-mean data (same schema as calibrate). Should
                include enough historical context for rolling refit.
            spec: The input specification.

        Returns:
            List of ``DriftEpisode`` objects (may be empty). New episodes
            have ``is_new=True``; continuations have ``is_new=False``.
        """
        cal = CalibrationResult.from_dict(state.calibration)  # type: ignore[arg-type]
        if cal.status == InputStatus.DERIVED.value:
            return []

        hourly_df = hourly_df.copy()
        if "hour" not in hourly_df.columns:
            hourly_df["hour"] = hourly_df.index.hour

        # Identify new rows to score (rows after last_processed_ts)
        if state.last_processed_ts is not None:
            cutoff = pd.Timestamp(state.last_processed_ts)
            new_df = hourly_df[hourly_df.index > cutoff]
        else:
            new_df = hourly_df

        if len(new_df) == 0:
            return []

        # Rolling refit for model-based inputs
        if (cal.kind != InputKind.PHYSICS.value
                and state.last_processed_ts is not None):
            needs_refit = False
            if state.last_refit_ts is None:
                needs_refit = True
            else:
                last_refit = pd.Timestamp(state.last_refit_ts)
                weeks_since = (
                    pd.Timestamp(state.last_processed_ts) - last_refit
                ).total_seconds() / (7 * 24 * 3600)
                if weeks_since >= DRIFT_WATCH_REFIT_INTERVAL_WEEKS:
                    needs_refit = True

            if needs_refit:
                _rolling_refit(state, hourly_df, spec)
                cal = CalibrationResult.from_dict(state.calibration)  # type: ignore[arg-type]

        # Reconstruct model
        model, feat_names, fill_vals = _reconstruct_model(cal)

        # Compute residual on new rows only
        residual = _recompute_residual(new_df, spec, model, feat_names, fill_vals)
        residual = residual.dropna()
        if len(residual) == 0:
            return []

        mu = cal.scale_mu
        sigma = cal.scale_sigma
        k = cal.k
        h = cal.h
        quiet_hours = DRIFT_WATCH_QUIET_HOURS

        episodes: List[DriftEpisode] = []

        for ts, val in residual.items():
            z = (val - mu) / sigma

            # Update accumulators
            state.s_pos = max(0.0, state.s_pos + z - k)
            state.s_neg = max(0.0, state.s_neg - z - k)

            # Track last-zero timestamps
            if state.s_pos <= 0.0:
                state.last_zero_pos_ts = str(ts)
            if state.s_neg <= 0.0:
                state.last_zero_neg_ts = str(ts)

            # ── Positive direction episode logic ─────────────────────────
            if state.s_pos > h:
                is_new_episode = True
                if state.pos_last_alarm_ts is not None:
                    hours_since = (
                        ts - pd.Timestamp(state.pos_last_alarm_ts)
                    ).total_seconds() / 3600
                    if hours_since <= quiet_hours:
                        is_new_episode = False

                if is_new_episode:
                    state.next_episode_seq += 1
                    ep_id = f"{spec.input_id}_{state.next_episode_seq}"
                    state.pos_episode_id = ep_id
                    state.pos_episode_start = str(ts)
                else:
                    ep_id = state.pos_episode_id  # type: ignore[assignment]

                onset = (pd.Timestamp(state.last_zero_pos_ts)
                         if state.last_zero_pos_ts else ts)
                episodes.append(DriftEpisode(
                    input_id=spec.input_id,
                    target_channel=spec.target_channel,
                    direction="+",
                    episode_id=ep_id,
                    first_detected_ts=(
                        pd.Timestamp(state.pos_episode_start)
                        if state.pos_episode_start else ts
                    ),
                    last_confirmed_ts=ts,
                    onset_estimate=onset,
                    current_offset_native=z * sigma,
                    peak_statistic=state.s_pos,
                    is_new=is_new_episode,
                ))

                state.pos_last_alarm_ts = str(ts)
                state.s_pos = 0.0
                state.last_zero_pos_ts = str(ts)

            # ── Negative direction episode logic ─────────────────────────
            if state.s_neg > h:
                is_new_episode = True
                if state.neg_last_alarm_ts is not None:
                    hours_since = (
                        ts - pd.Timestamp(state.neg_last_alarm_ts)
                    ).total_seconds() / 3600
                    if hours_since <= quiet_hours:
                        is_new_episode = False

                if is_new_episode:
                    state.next_episode_seq += 1
                    ep_id = f"{spec.input_id}_{state.next_episode_seq}"
                    state.neg_episode_id = ep_id
                    state.neg_episode_start = str(ts)
                else:
                    ep_id = state.neg_episode_id  # type: ignore[assignment]

                onset = (pd.Timestamp(state.last_zero_neg_ts)
                         if state.last_zero_neg_ts else ts)
                episodes.append(DriftEpisode(
                    input_id=spec.input_id,
                    target_channel=spec.target_channel,
                    direction="-",
                    episode_id=ep_id,
                    first_detected_ts=(
                        pd.Timestamp(state.neg_episode_start)
                        if state.neg_episode_start else ts
                    ),
                    last_confirmed_ts=ts,
                    onset_estimate=onset,
                    current_offset_native=-z * sigma,
                    peak_statistic=state.s_neg,
                    is_new=is_new_episode,
                ))

                state.neg_last_alarm_ts = str(ts)
                state.s_neg = 0.0
                state.last_zero_neg_ts = str(ts)

        state.last_processed_ts = str(residual.index[-1])
        return episodes


# ── Internal CUSUM (episode-list only, for calibration) ────────────────

def _run_cusum_episodes(
    z: np.ndarray,
    k: float,
    h: float,
    quiet_steps: int,
) -> List[Dict[str, Any]]:
    """Two-sided CUSUM with episode-based excursion logic.

    After each threshold crossing the accumulator resets to zero and
    keeps accumulating. Consecutive crossings in the same direction
    within *quiet_steps* are grouped into one episode (only the first
    has ``is_new=True``).

    Args:
        z: Standardised residual series.
        k: CUSUM allowance parameter (sigma units).
        h: CUSUM decision interval (sigma units).
        quiet_steps: Number of steps of silence before a new episode.

    Returns:
        List of episode-event dicts with ``idx``, ``dir``, ``onset_est``,
        ``is_new``.
    """
    n = len(z)
    if n == 0:
        return []

    sp = 0.0
    sn = 0.0
    events: List[Dict[str, Any]] = []
    pos_last_alarm = -(quiet_steps + 1)
    neg_last_alarm = -(quiet_steps + 1)
    last_zero_p = 0
    last_zero_n = 0

    for i in range(n):
        sp = max(0.0, sp + z[i] - k)
        sn = max(0.0, sn - z[i] - k)

        if sp <= 0.0:
            last_zero_p = i
        if sn <= 0.0:
            last_zero_n = i

        if sp > h:
            is_new = (i - pos_last_alarm) > quiet_steps
            events.append({
                "idx": i,
                "dir": "+",
                "onset_est": last_zero_p,
                "is_new": is_new,
            })
            pos_last_alarm = i
            sp = 0.0
            last_zero_p = i

        if sn > h:
            is_new = (i - neg_last_alarm) > quiet_steps
            events.append({
                "idx": i,
                "dir": "-",
                "onset_est": last_zero_n,
                "is_new": is_new,
            })
            neg_last_alarm = i
            sn = 0.0
            last_zero_n = i

    return events


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


def _find_index_at_or_after(
    index: pd.DatetimeIndex,
    ts: pd.Timestamp,
) -> Optional[int]:
    """Return the integer position of the first element >= *ts*, or None."""
    pos = index.searchsorted(ts)
    if pos >= len(index):
        return None
    return int(pos)
