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
from typing import (
    Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple,
)

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
    DRIFT_WATCH_ALERT_MIN_OFFSET_MULT,
    DRIFT_WATCH_ALERT_MIN_OOS_WEEKS,
    DRIFT_WATCH_ALERT_RATE_WINDOW_WEEKS,
    DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT,
    DRIFT_WATCH_ALERT_SUSPEND_MIN_COUNT,
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
        current_offset_native: Single-hour offset at the alarm hour,
            ``|z| * sigma`` in native units (positive for both directions).
            Kept for diagnosis only; ADR-013 §5 forbids alerting on it.
        peak_statistic: CUSUM statistic value at detection.
        is_new: ``True`` on first detection, ``False`` on continuation.
        offset_robust: ADR-013 §5 robust offset: the median of
            ``(residual - mu)`` over every scored hour from the episode's
            first onset estimate to this event's ``last_confirmed_ts``,
            inclusive, in native units. **Signed** (negative for a ``"-"``
            episode), unlike ``current_offset_native``; the alert limit
            compares its absolute value. ``mu`` is the value in force when
            each hour was scored, so a mid-episode refit does not rewrite
            earlier hours. ``None`` only when constructed without it.
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
    offset_robust: Optional[float] = None


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
        calibration_window_end: ISO timestamp of the last hour of the
            calibration window (the window ``h`` was chosen on and the
            self-test injected into). Set once by :meth:`DriftWatch.calibrate`
            and **never changed afterwards** -- :func:`_rolling_refit`
            rewrites ``fit_window_start/end`` but must leave this alone.
            ADR-013 §3: data strictly after it is out-of-sample; an episode
            with ``first_detected_ts <=`` it is in-sample. ``None`` for a
            payload serialised before the field existed (see
            :meth:`from_dict`), meaning "out-of-sample start unknown".
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
    calibration_window_end: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CalibrationResult":
        """Reconstruct from a dictionary.

        Back-compat for ``calibration_window_end``: a payload without it is
        loaded with ``None``; the value is deliberately **not derived**.
        The only candidate source is ``fit_window_end + gap + baseline``,
        and the payload cannot say whether ``fit_window_end`` is still the
        original one: the copy embedded in ``DriftInputState.calibration``
        has been rewritten by every rolling refit, which would place the
        derived boundary weeks or months too late and silently move
        out-of-sample episodes into the uncounted in-sample set. That error
        is optimistic, which ADR-013 §3 forbids. ``None`` instead makes the
        alert gate treat the input as having no out-of-sample evidence, so
        it stays ``off`` until it is recalibrated (a backfill does that).
        """
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
        if "calibration_window_end" not in d:
            d["calibration_window_end"] = None
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
        pos_pending_devs: ``residual - mu`` (native units) for every scored
            hour since ``s_pos`` was last zero, that hour included. Becomes
            the episode buffer when a new positive episode is detected, so
            the robust offset starts at the onset estimate (ADR-013 §5).
        pos_episode_devs: ``residual - mu`` for every scored hour from the
            open positive episode's first onset estimate up to the latest
            scored hour. Cleared once the episode has been quiet for more
            than ``DRIFT_WATCH_QUIET_HOURS`` (the episode can no longer
            continue). Its median is ``DriftEpisode.offset_robust``.
        neg_pending_devs: As ``pos_pending_devs`` for ``s_neg``.
        neg_episode_devs: As ``pos_episode_devs`` for negative episodes.
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
    # Robust-offset buffers (ADR-013 §5)
    pos_pending_devs: List[float] = field(default_factory=list)
    pos_episode_devs: List[float] = field(default_factory=list)
    neg_pending_devs: List[float] = field(default_factory=list)
    neg_episode_devs: List[float] = field(default_factory=list)

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
        # Backward compatibility: states written before the ADR-013 robust
        # offset lack its buffers. An episode open across the upgrade then
        # gets a robust offset over the hours scored since the upgrade only.
        for fld in ("pos_pending_devs", "pos_episode_devs",
                    "neg_pending_devs", "neg_episode_devs"):
            d[fld] = [float(v) for v in (d.get(fld) or [])]
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


def _robust_offset(devs: Sequence[float], current_dev: float) -> float:
    """Median of the episode's ``residual - mu`` values (ADR-013 §5).

    Args:
        devs: Per-hour ``residual - mu`` from the episode's first onset
            estimate to the current alarm hour, inclusive.
        current_dev: The alarm hour's own value, used only if *devs* is
            empty (cannot happen on a state written by this version; it
            guards a hand-built or truncated state).

    Returns:
        The signed median in native units.
    """
    if not devs:
        return float(current_dev)
    return float(np.median(np.asarray(devs, dtype=float)))


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

    # Update calibration in state (preserve k, h from initial calibration).
    # calibration_window_end is carried over untouched from the existing
    # payload: it marks where out-of-sample data starts (ADR-013 §3) and
    # must survive every refit. Only the fit window moves.
    cal_dict = dict(state.calibration)  # type: ignore[arg-type]
    cal_dict["scale_mu"] = new_mu
    cal_dict["scale_sigma"] = new_sigma
    cal_dict["model_coefficients"] = new_model.coef_.tolist()
    cal_dict["model_intercept"] = float(new_model.intercept_)
    cal_dict["model_feature_names"] = new_feat
    cal_dict["model_fill_values"] = new_fill.tolist()
    cal_dict["fit_window_start"] = str(clean_baseline.index[0])
    cal_dict["fit_window_end"] = str(clean_baseline.index[-1])
    cal_dict["calibration_window_end"] = cal.calibration_window_end
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
                    calibration_window_end=str(cal_data.index[-1]),
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
            calibration_window_end=str(cal_data.index[-1]),
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
            dev = float(val - mu)  # native units, signed (ADR-013 §5)

            # Robust-offset buffers: an open episode absorbs every scored
            # hour while it can still continue (last alarm within
            # quiet_hours); once it cannot, its buffer is released.
            for last_alarm, buf in (
                (state.pos_last_alarm_ts, state.pos_episode_devs),
                (state.neg_last_alarm_ts, state.neg_episode_devs),
            ):
                if last_alarm is None:
                    buf.clear()
                    continue
                since = (ts - pd.Timestamp(last_alarm)).total_seconds() / 3600
                if since <= quiet_hours:
                    buf.append(dev)
                else:
                    buf.clear()

            # Update accumulators
            state.s_pos = max(0.0, state.s_pos + z - k)
            state.s_neg = max(0.0, state.s_neg - z - k)

            # Track last-zero timestamps. The pending buffer restarts at the
            # zero hour, which is the onset estimate a later alarm reports.
            if state.s_pos <= 0.0:
                state.last_zero_pos_ts = str(ts)
                state.pos_pending_devs = [dev]
            else:
                state.pos_pending_devs.append(dev)
            if state.s_neg <= 0.0:
                state.last_zero_neg_ts = str(ts)
                state.neg_pending_devs = [dev]
            else:
                state.neg_pending_devs.append(dev)

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
                    state.pos_episode_devs = list(state.pos_pending_devs)
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
                    offset_robust=_robust_offset(state.pos_episode_devs, dev),
                ))

                state.pos_last_alarm_ts = str(ts)
                state.s_pos = 0.0
                state.last_zero_pos_ts = str(ts)
                state.pos_pending_devs = [dev]

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
                    state.neg_episode_devs = list(state.neg_pending_devs)
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
                    offset_robust=_robust_offset(state.neg_episode_devs, dev),
                ))

                state.neg_last_alarm_ts = str(ts)
                state.s_neg = 0.0
                state.last_zero_neg_ts = str(ts)
                state.neg_pending_devs = [dev]

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


# ── Alert policy (ADR-013 §3 / §5) ───────────────────────────────────────
#
# Pure functions only: no I/O, no clock, no global flag read. The caller
# (the backend alert writer, ADR-013 §8 step 5) loads the episodes, labels,
# calibration and current alert_state, passes them in, and persists the
# returned state with its reason and a timestamp.


class AlertState(str, Enum):
    """Per-input alert state (ADR-013 §3). Separate from :class:`InputStatus`."""
    OFF = "off"
    ELIGIBLE = "eligible"
    ENABLED = "enabled"
    SUSPENDED = "suspended"


class FeedbackLabel(str, Enum):
    """Operator label on a drift episode (ADR-013 §4, ``drift_watch_feedback``).

    Only ``CONFIRMED_DRIFT`` takes an episode out of the false-alarm count.
    ``FALSE_ALARM``, no label, and any unrecognised label string all count.
    """
    CONFIRMED_DRIFT = "confirmed_drift"
    FALSE_ALARM = "false_alarm"


class DriftEpisodeKey(NamedTuple):
    """ADR-013 §4 natural key of a drift episode.

    ``(tenant_id, greenhouse_id, input_id, direction, first_detected_ts)``.
    Unlike ``episode_id`` it survives a backfill, because
    ``first_detected_ts`` depends only on the data. Build it with
    :func:`episode_natural_key` so the timestamp is normalised to UTC and
    two keys for the same episode compare equal.
    """
    tenant_id: str
    greenhouse_id: str
    input_id: str
    direction: str
    first_detected_ts: pd.Timestamp


@dataclass(frozen=True)
class GateEpisode:
    """One stored drift episode as seen by the §3 alert gate.

    Attributes:
        key: The episode's natural key (one input, one scope).
        offset_robust: ADR-013 §5 robust offset (signed, native units), or
            ``None`` if not known (an episode stored before the field
            existed). An unknown offset is treated as passing the §5 limit,
            so it is counted: the count must stay an upper bound.
        suppressed: ``True`` if the §6 suppression rule (an overlapping
            ``stuck_fault`` on any channel the input reads) withheld the
            alert. Decided by the caller, which has the database.
    """
    key: DriftEpisodeKey
    offset_robust: Optional[float] = None
    suppressed: bool = False


class AlertGateDecision(NamedTuple):
    """Result of :func:`evaluate_alert_gate`: ``(count, next_state, reason)``."""
    count: int
    next_state: AlertState
    reason: str


class OosEpisodeCounts(NamedTuple):
    """Result of :func:`count_oos_false_alarms`.

    Attributes:
        count: Alert-eligible OOS episodes in the trailing window that do
            not carry ``confirmed_drift`` -- the figure the gate uses.
        raw_count: All OOS episodes in the trailing window, before the §5
            and §6 filters and before labels. Diagnosis only.
    """
    count: int
    raw_count: int


_WEEK_SECONDS: float = 7 * 24 * 3600.0


def _to_utc(ts: Any) -> pd.Timestamp:
    """Coerce *ts* to a tz-aware UTC ``pd.Timestamp`` (naive is read as UTC).

    Naive is read as UTC because that is how the backend reads SQLite rows
    (``_aware`` in ``src/api/routes/drift_watch.py``).

    Raises:
        ValueError: If *ts* is ``None`` or NaT.
    """
    if ts is None:
        raise ValueError("timestamp is None")
    out = pd.Timestamp(ts)
    if pd.isna(out):
        raise ValueError("timestamp is NaT")
    if out.tzinfo is None:
        return out.tz_localize("UTC")
    return out.tz_convert("UTC")


def episode_natural_key(
    tenant_id: str,
    greenhouse_id: str,
    input_id: str,
    direction: str,
    first_detected_ts: Any,
) -> DriftEpisodeKey:
    """Build a normalised :class:`DriftEpisodeKey` (ADR-013 §4).

    Args:
        tenant_id: Tenant identifier.
        greenhouse_id: Greenhouse identifier.
        input_id: Drift input identifier (e.g. ``"temp_paired"``).
        direction: ``"+"`` or ``"-"``.
        first_detected_ts: Episode first-detection time; ``datetime``,
            ``pd.Timestamp`` or ISO string. Naive is read as UTC.

    Returns:
        The key with ``first_detected_ts`` as a UTC ``pd.Timestamp``.

    Raises:
        ValueError: If *direction* is not ``"+"``/``"-"`` or the timestamp
            is missing.
    """
    if direction not in ("+", "-"):
        raise ValueError(f"direction must be '+' or '-', got {direction!r}")
    return DriftEpisodeKey(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        input_id=input_id,
        direction=direction,
        first_detected_ts=_to_utc(first_detected_ts),
    )


def _normalise_key(key: DriftEpisodeKey) -> DriftEpisodeKey:
    """Re-normalise a key built without :func:`episode_natural_key`."""
    return episode_natural_key(*key)


def passes_alert_min_offset(
    offset_robust: Optional[float],
    min_shift: float,
    *,
    min_offset_mult: float = DRIFT_WATCH_ALERT_MIN_OFFSET_MULT,
) -> bool:
    """ADR-013 §5: ``|robust offset| >= min_offset_mult * min_shift``.

    Shared by the gate count and (step 5) the alert writer, so both apply
    the same rule. An unknown (``None`` or NaN) offset returns ``True``:
    for the count that is the conservative choice. The alert writer always
    has a robust offset from :meth:`DriftWatch.update`, so it never relies
    on this branch.

    Args:
        offset_robust: Signed robust offset in native units, or ``None``.
        min_shift: The input's ``DriftInputSpec.min_shift`` (native units).
        min_offset_mult: Multiplier on *min_shift*.

    Returns:
        Whether the episode passes the minimum-offset limit.

    Raises:
        ValueError: If *min_shift* is not a positive finite number.
    """
    if not (math.isfinite(min_shift) and min_shift > 0):
        raise ValueError(f"min_shift must be positive and finite, got {min_shift!r}")
    if offset_robust is None or not math.isfinite(offset_robust):
        return True
    return abs(offset_robust) >= min_offset_mult * min_shift


def count_oos_false_alarms(
    episodes: Iterable[GateEpisode],
    labels: Mapping[DriftEpisodeKey, str],
    *,
    calibration_window_end: Optional[Any],
    as_of: Any,
    min_shift: float,
    rate_window_weeks: int = DRIFT_WATCH_ALERT_RATE_WINDOW_WEEKS,
    min_offset_mult: float = DRIFT_WATCH_ALERT_MIN_OFFSET_MULT,
) -> OosEpisodeCounts:
    """ADR-013 §3 out-of-sample false-alarm count for one input.

    An episode is counted when all of these hold:

    - it is out-of-sample: ``first_detected_ts > calibration_window_end``
      (in-sample episodes are never counted, in either direction);
    - it is new in the trailing window:
      ``as_of - rate_window_weeks < first_detected_ts <= as_of``;
    - it passes the alert filters: §5 minimum robust offset and not §6
      suppressed;
    - its label is not ``confirmed_drift`` (no label counts).

    Episodes are de-duplicated on the natural key, so passing continuation
    events of one episode counts it once (the first record seen wins for
    ``offset_robust``/``suppressed``; pass one record per episode).

    Args:
        episodes: Stored episodes of ONE input in ONE scope.
        labels: Operator labels keyed on the natural key.
        calibration_window_end: ``CalibrationResult.calibration_window_end``.
            ``None`` (unknown) means no episode can be shown to be
            out-of-sample, so both counts are 0; the gate separately refuses
            eligibility in that case.
        as_of: The newest scored hour (``DriftInputState.last_processed_ts``).
        min_shift: The input's ``min_shift`` in native units.
        rate_window_weeks: Trailing window length.
        min_offset_mult: §5 multiplier.

    Returns:
        ``OosEpisodeCounts(count, raw_count)``.

    Raises:
        ValueError: If episodes span more than one input/scope, if any
            episode is after *as_of* (the caller passed the wrong as-of), or
            if *min_shift* is invalid.
    """
    if not (math.isfinite(min_shift) and min_shift > 0):
        raise ValueError(f"min_shift must be positive and finite, got {min_shift!r}")
    as_of_utc = _to_utc(as_of)
    window_start = as_of_utc - pd.Timedelta(weeks=rate_window_weeks)
    cal_end = None if calibration_window_end is None else _to_utc(calibration_window_end)
    # Enum members are read by value: str() of a str-mixin Enum is
    # "FeedbackLabel.CONFIRMED_DRIFT", which would never match and would
    # silently count a confirmed episode as a false alarm.
    norm_labels = {
        _normalise_key(k): (v.value if isinstance(v, Enum) else str(v))
        for k, v in labels.items()
    }

    seen: Dict[DriftEpisodeKey, GateEpisode] = {}
    scope: Optional[Tuple[str, str, str]] = None
    for ep in episodes:
        key = _normalise_key(ep.key)
        ep_scope = (key.tenant_id, key.greenhouse_id, key.input_id)
        if scope is None:
            scope = ep_scope
        elif ep_scope != scope:
            raise ValueError(
                f"episodes span more than one input/scope: {scope} vs {ep_scope}"
            )
        if key.first_detected_ts > as_of_utc:
            raise ValueError(
                f"episode first detected at {key.first_detected_ts} is after "
                f"as_of {as_of_utc}; as_of must be the newest scored hour"
            )
        if key not in seen:
            seen[key] = ep

    if cal_end is None:
        return OosEpisodeCounts(count=0, raw_count=0)

    count = 0
    raw = 0
    for key, ep in seen.items():
        ts = key.first_detected_ts
        if ts <= cal_end:
            continue  # in-sample: never counted
        if not (window_start < ts <= as_of_utc):
            continue
        raw += 1
        if ep.suppressed:
            continue
        if not passes_alert_min_offset(
            ep.offset_robust, min_shift, min_offset_mult=min_offset_mult,
        ):
            continue
        if norm_labels.get(key) == FeedbackLabel.CONFIRMED_DRIFT.value:
            continue
        count += 1
    return OosEpisodeCounts(count=count, raw_count=raw)


def evaluate_alert_gate(
    episodes: Iterable[GateEpisode],
    labels: Mapping[DriftEpisodeKey, str],
    *,
    current_state: AlertState,
    input_status: str,
    calibration_window_end: Optional[Any],
    as_of: Any,
    min_shift: float,
    alerts_enabled: bool,
    min_oos_weeks: int = DRIFT_WATCH_ALERT_MIN_OOS_WEEKS,
    rate_window_weeks: int = DRIFT_WATCH_ALERT_RATE_WINDOW_WEEKS,
    enable_max_count: int = DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT,
    suspend_min_count: int = DRIFT_WATCH_ALERT_SUSPEND_MIN_COUNT,
    min_offset_mult: float = DRIFT_WATCH_ALERT_MIN_OFFSET_MULT,
) -> AlertGateDecision:
    """Evaluate the ADR-013 §3 automatic alert-state transitions for one input.

    Transitions (``count`` from :func:`count_oos_false_alarms`; "ready"
    means ``input_status == armed`` AND a known ``calibration_window_end``
    AND at least ``min_oos_weeks`` of OOS data AND ``count <=
    enable_max_count``):

    =============  =========================================  ============
    from           condition                                  to
    =============  =========================================  ============
    any            ``alerts_enabled`` is False                ``off``
    ``off``        ready                                      ``eligible``
    ``off``        not ready                                  ``off``
    ``eligible``   ready                                      ``eligible``
    ``eligible``   not ready                                  ``off`` (*)
    ``enabled``    not armed                                  ``suspended``
    ``enabled``    ``count >= suspend_min_count``             ``suspended``
    ``enabled``    OOS evidence lost (*)                      ``suspended``
    ``enabled``    otherwise (incl. count 4..6)               ``enabled``
    ``suspended``  ready (so count <= 3)                      ``eligible``
    ``suspended``  otherwise (incl. count 4..6)               ``suspended``
    =============  =========================================  ============

    ``eligible -> enabled`` is never returned: it is manual only (the
    admin endpoint, ADR-013 §8 step 6). Rows marked (*) are readings of cases the
    ADR does not state, chosen as the safer option:

    - ``eligible`` whose entry conditions lapse goes back to ``off``, so an
      admin cannot enable an input that no longer meets the gate.
      ``off``/``eligible`` write no alerts, so this cannot flap alerts.
    - ``enabled`` with an unknown ``calibration_window_end`` or fewer than
      ``min_oos_weeks`` of OOS data (only possible after a recalibration
      moved the boundary) is suspended: the OOS evidence the enable rested
      on was about the old ``k``/``h``.
    - ``suspended -> eligible`` needs every ``off -> eligible`` condition,
      not only the count, so an input suspended for leaving ``armed`` does
      not become eligible while still unarmed.
    - The global flag off forces ``off`` from every state, so turning the
      flag back on never silently resumes alerts; re-enabling stays manual.

    Args:
        episodes: Stored episodes of this input (see
            :func:`count_oos_false_alarms`).
        labels: Operator labels keyed on the natural key.
        current_state: The persisted ``alert_state``.
        input_status: The input's calibration status (``armed``/``shadow``/
            ``derived``, or the backend's ``uncalibrated``).
        calibration_window_end: Immutable OOS boundary, or ``None``.
        as_of: The newest scored hour.
        min_shift: The input's ``min_shift`` in native units.
        alerts_enabled: ``DRIFT_WATCH_ALERTS_ENABLED``, passed explicitly so
            this function reads no global state.
        min_oos_weeks: OOS weeks needed for eligibility.
        rate_window_weeks: Trailing count window.
        enable_max_count: Count at or below which an input is eligible.
        suspend_min_count: Count at or above which ``enabled`` suspends.
        min_offset_mult: §5 multiplier.

    Returns:
        ``AlertGateDecision(count, next_state, reason)``. ``reason`` is a
        short human-readable sentence to store with the transition.

    Raises:
        ValueError: On invalid inputs (see :func:`count_oos_false_alarms`),
            or if ``enable_max_count >= suspend_min_count`` (no hysteresis).
    """
    if enable_max_count >= suspend_min_count:
        raise ValueError(
            f"enable_max_count ({enable_max_count}) must be < "
            f"suspend_min_count ({suspend_min_count})"
        )
    state = AlertState(current_state)
    counts = count_oos_false_alarms(
        episodes, labels,
        calibration_window_end=calibration_window_end,
        as_of=as_of,
        min_shift=min_shift,
        rate_window_weeks=rate_window_weeks,
        min_offset_mult=min_offset_mult,
    )
    count = counts.count

    if not alerts_enabled:
        return AlertGateDecision(
            count, AlertState.OFF, "drift-watch alerts are disabled globally",
        )

    armed = input_status == InputStatus.ARMED.value
    oos_weeks: Optional[float] = None
    if calibration_window_end is not None:
        span = _to_utc(as_of) - _to_utc(calibration_window_end)
        oos_weeks = max(span.total_seconds() / _WEEK_SECONDS, 0.0)
    oos_ok = oos_weeks is not None and oos_weeks >= min_oos_weeks

    # Why the input is not ready, in priority order (first failure wins).
    not_ready: Optional[str] = None
    if not armed:
        not_ready = f"input status is {input_status!r}, not 'armed'"
    elif oos_weeks is None:
        not_ready = "calibration window end unknown; recalibration needed"
    elif not oos_ok:
        not_ready = (
            f"{oos_weeks:.1f} weeks of out-of-sample data < {min_oos_weeks} required"
        )
    elif count > enable_max_count:
        not_ready = (
            f"out-of-sample count {count} in trailing {rate_window_weeks} weeks "
            f"> {enable_max_count}"
        )
    ready_reason = (
        f"armed, {oos_weeks:.1f} weeks out-of-sample, count {count} "
        f"<= {enable_max_count} in trailing {rate_window_weeks} weeks"
        if not_ready is None else ""
    )

    if state is AlertState.OFF:
        if not_ready is None:
            return AlertGateDecision(count, AlertState.ELIGIBLE, ready_reason)
        return AlertGateDecision(count, AlertState.OFF, not_ready)

    if state is AlertState.ELIGIBLE:
        if not_ready is None:
            return AlertGateDecision(count, AlertState.ELIGIBLE, ready_reason)
        return AlertGateDecision(count, AlertState.OFF, not_ready)

    if state is AlertState.ENABLED:
        if not armed:
            return AlertGateDecision(
                count, AlertState.SUSPENDED,
                f"input status is {input_status!r}, not 'armed'",
            )
        if count >= suspend_min_count:
            return AlertGateDecision(
                count, AlertState.SUSPENDED,
                f"out-of-sample count {count} in trailing {rate_window_weeks} "
                f"weeks >= {suspend_min_count}",
            )
        if not oos_ok:
            return AlertGateDecision(
                count, AlertState.SUSPENDED,
                not_ready or "out-of-sample evidence lost",
            )
        return AlertGateDecision(
            count, AlertState.ENABLED,
            f"count {count} < {suspend_min_count} in trailing "
            f"{rate_window_weeks} weeks",
        )

    # SUSPENDED
    if not_ready is None:
        return AlertGateDecision(count, AlertState.ELIGIBLE, ready_reason)
    return AlertGateDecision(count, AlertState.SUSPENDED, not_ready)

