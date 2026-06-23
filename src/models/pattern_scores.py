"""
Pure scoring functions for the seven anomaly pattern types.

Each ``score_*`` function takes one ``(seq_len, input_dim)`` sequence plus
the precomputed :class:`PatternBaseline` and returns
``(score in [0, 1], affected sensor indices)``. The functions are stateless
(all tunables live in :mod:`src.config`) so each detector can be tuned and
unit-tested in isolation; :class:`src.models.pattern_detector.PatternDetector`
orchestrates them and maps indices back to sensor names.
"""

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np

from src.config import (
    NUMERICAL_EPSILON,
    SENSOR_NAMES,
    VELOCITY_LIMITS,
    PATTERN_CASCADE_MIN_SENSOR_Z,
    PATTERN_CASCADE_MIN_SENSORS,
    PATTERN_CYCLIC_MIN_BASELINE_POWER,
    PATTERN_CYCLIC_MIN_SEQ_LEN,
    PATTERN_CYCLIC_POWER_DROP_RATIO,
    PATTERN_DEPENDENCY_PAIR_THRESHOLD,
    PATTERN_DRIFT_VARIANCE_RATIO,
    PATTERN_LAG_IMPROVEMENT_MARGIN,
    PATTERN_LAG_MAX_STEPS,
    PATTERN_LAG_MIN_BASELINE_CORRELATION,
    PATTERN_LAG_MIN_CORRELATION,
    PATTERN_LAG_MIN_SEQ_LEN,
    PATTERN_LAG_WINDOW_FRACTION,
    PATTERN_SATURATION_BOUNDARY_TOLERANCE,
    PATTERN_SATURATION_FRACTION_THRESHOLD,
    PATTERN_SPIKE_MAX_DURATION_FRACTION,
    PATTERN_SPIKE_SCORE_SCALE,
    PATTERN_SPIKE_Z_THRESHOLD,
)

# (score in [0, 1], indices of affected sensor channels)
PatternScore = Tuple[float, np.ndarray]

_EMPTY_INDICES: np.ndarray = np.array([], dtype=int)

# Velocity limits per sensor sourced from config (SCADA spec section 1.1).
_SPIKE_VELOCITY_LIMITS: dict[str, float] = VELOCITY_LIMITS



@dataclass(frozen=True)
class PatternBaseline:
    """Normal-operation statistics consumed by the pattern scorers.

    Per-sensor arrays all have shape ``(input_dim,)``; ``correlations`` is
    ``(input_dim, input_dim)`` with undefined (constant-channel) entries
    zeroed. ``dominant_frequency_bins`` indexes the dominant non-DC FFT bin
    per sensor and ``dominant_power_fractions`` is the share of total non-DC
    power it holds (0.0 when the sensor is not periodic or the baseline
    sequences are shorter than ``PATTERN_CYCLIC_MIN_SEQ_LEN``).
    ``sequence_length`` is the FFT bin basis for cyclic comparison.
    """

    mean: np.ndarray
    std: np.ndarray
    correlations: np.ndarray
    min_values: np.ndarray
    max_values: np.ndarray
    sequence_length: int
    dominant_frequency_bins: np.ndarray
    dominant_power_fractions: np.ndarray
    num_sequences: int


def _z_scores(sequence: np.ndarray, baseline: PatternBaseline) -> np.ndarray:
    """Absolute z-scores for each reading relative to the baseline distribution."""
    return np.abs(sequence - baseline.mean) / (baseline.std + NUMERICAL_EPSILON)


def _freeze(array: np.ndarray) -> np.ndarray:
    """Mark a numpy array as read-only so frozen dataclass fields stay immutable."""
    array.flags.writeable = False
    return array


def compute_baseline(sequences: np.ndarray) -> PatternBaseline:
    """Compute baseline statistics from normal-operation sequences.

    Args:
        sequences: Validated array of shape ``(N, seq_len, input_dim)``.

    Returns:
        A :class:`PatternBaseline` with per-sensor statistics, pairwise
        correlations and dominant-frequency spectra.  All numpy array
        fields are set to read-only to prevent accidental mutation of
        shared state.
    """
    flattened = sequences.reshape(-1, sequences.shape[-1])
    frequency_bins, power_fractions = _baseline_spectra(sequences)
    return PatternBaseline(
        mean=_freeze(flattened.mean(axis=0)),
        std=_freeze(flattened.std(axis=0)),
        correlations=_freeze(_safe_corrcoef(flattened.T)),
        min_values=_freeze(flattened.min(axis=0)),
        max_values=_freeze(flattened.max(axis=0)),
        sequence_length=int(sequences.shape[1]),
        dominant_frequency_bins=_freeze(frequency_bins),
        dominant_power_fractions=_freeze(power_fractions),
        num_sequences=int(sequences.shape[0]),
    )


def score_drift(sequence: np.ndarray, baseline: PatternBaseline) -> PatternScore:
    """DRIFT: sensor stuck -- score = fraction of sensors whose in-sequence
    variance fell below ``PATTERN_DRIFT_VARIANCE_RATIO`` of baseline variance."""
    variances = sequence.var(axis=0)
    expected_variances = baseline.std**2
    is_drifting = variances < PATTERN_DRIFT_VARIANCE_RATIO * (
        expected_variances + NUMERICAL_EPSILON
    )
    return float(is_drifting.mean()), np.flatnonzero(is_drifting)


def score_spike(sequence: np.ndarray, baseline: PatternBaseline) -> PatternScore:
    """SPIKE: sudden short-lived deviation (SCADA spec section 1.1).

    **Primary path** — velocity + reversal (spec-compliant):
        |Δx_t| > Δ_max  AND  sign(Δx_t) ≠ sign(Δx_{t+1})
    This is the authoritative detection condition. If any sensor passes
    the velocity+reversal check, the spike is confirmed (score = 1.0).

    **Fallback path** — z-score detection for sensors without velocity
    limits: uses ``PATTERN_SPIKE_Z_THRESHOLD`` outlier fractions with
    ``PATTERN_SPIKE_MAX_DURATION_FRACTION`` cap and score scaled by peak z.
    """
    n_sensors = sequence.shape[1]
    sensor_names = list(SENSOR_NAMES[:n_sensors])

    # --- Primary path: spec-compliant velocity + reversal (SCADA spec 1.1) ---
    # Δx_t = x[t] - x[t-1].  Flag if |Δx_t| > Δ_max AND sign reverses next step.
    primary_affected: list[int] = []
    if sequence.shape[0] >= 3:
        for s_idx, s_name in enumerate(sensor_names):
            vel_limit = _SPIKE_VELOCITY_LIMITS.get(s_name)
            if vel_limit is None:
                continue
            diffs = np.diff(sequence[:, s_idx])
            for t in range(len(diffs) - 1):
                if abs(diffs[t]) > vel_limit and diffs[t] * diffs[t + 1] < 0:
                    primary_affected.append(s_idx)
                    break
    if primary_affected:
        affected = np.array(primary_affected, dtype=int)
        return 1.0, affected

    # --- Fallback path: z-score detection for sensors without velocity limits ---
    z = _z_scores(sequence, baseline)
    outlier_fractions = (z > PATTERN_SPIKE_Z_THRESHOLD).mean(axis=0)
    has_spike = (outlier_fractions > 0) & (
        outlier_fractions <= PATTERN_SPIKE_MAX_DURATION_FRACTION
    )
    affected = np.flatnonzero(has_spike)
    if affected.size == 0:
        return 0.0, _EMPTY_INDICES
    peak_z = float(z[:, affected].max())
    full_scale_z = PATTERN_SPIKE_Z_THRESHOLD * PATTERN_SPIKE_SCORE_SCALE
    return min(peak_z / full_scale_z, 1.0), affected


def score_dependency(sequence: np.ndarray, baseline: PatternBaseline) -> PatternScore:
    """DEPENDENCY: expected pairwise correlation structure breaks.

    Score = mean absolute change of the upper-triangle correlations vs the
    baseline (clipped to [0, 1]). Sensors in pairs whose correlation changed
    by more than ``PATTERN_DEPENDENCY_PAIR_THRESHOLD`` are affected."""
    correlation_diff = np.abs(_safe_corrcoef(sequence.T) - baseline.correlations)
    upper_i, upper_j = np.triu_indices_from(correlation_diff, k=1)
    if upper_i.size == 0:
        return 0.0, _EMPTY_INDICES
    pair_diffs = correlation_diff[upper_i, upper_j]
    broken_pairs = pair_diffs > PATTERN_DEPENDENCY_PAIR_THRESHOLD
    affected = np.unique(
        np.concatenate([upper_i[broken_pairs], upper_j[broken_pairs]])
    )
    return float(np.clip(pair_diffs.mean(), 0.0, 1.0)), affected


def score_lag(sequence: np.ndarray, baseline: PatternBaseline) -> PatternScore:
    """LAG: normally-correlated sensors are now correlated at a delay.

    Only pairs with strong baseline correlation are candidates. A pair is
    lagged when its best cross-correlation occurs at a non-zero shift, is
    strong, and clearly beats the zero-shift correlation.
    Score = lagged pairs / candidate pairs."""
    seq_len, num_sensors = sequence.shape
    if seq_len < PATTERN_LAG_MIN_SEQ_LEN:
        return 0.0, _EMPTY_INDICES
    candidate_pairs: List[Tuple[int, int]] = [
        (i, j)
        for i in range(num_sensors)
        for j in range(i + 1, num_sensors)
        if abs(baseline.correlations[i, j]) >= PATTERN_LAG_MIN_BASELINE_CORRELATION
    ]
    if not candidate_pairs:
        return 0.0, _EMPTY_INDICES
    lagged_pairs = [
        pair
        for pair in candidate_pairs
        if _is_pair_lagged(sequence[:, pair[0]], sequence[:, pair[1]])
    ]
    if not lagged_pairs:
        return 0.0, _EMPTY_INDICES
    affected = np.unique(np.array(lagged_pairs, dtype=int).ravel())
    return float(len(lagged_pairs) / len(candidate_pairs)), affected


def score_saturation(sequence: np.ndarray, baseline: PatternBaseline) -> PatternScore:
    """SATURATION: sensor railed at (or beyond) its baseline min/max.

    A sensor is saturated when at least
    ``PATTERN_SATURATION_FRACTION_THRESHOLD`` of its readings sit within a
    tolerance band of -- or beyond -- the baseline boundary. Score = fraction
    of saturated sensors."""
    boundary_band = PATTERN_SATURATION_BOUNDARY_TOLERANCE * (
        baseline.max_values - baseline.min_values + NUMERICAL_EPSILON
    )
    at_lower_bound = sequence <= baseline.min_values + boundary_band
    at_upper_bound = sequence >= baseline.max_values - boundary_band
    boundary_fractions = (at_lower_bound | at_upper_bound).mean(axis=0)
    is_saturated = boundary_fractions >= PATTERN_SATURATION_FRACTION_THRESHOLD
    return float(is_saturated.mean()), np.flatnonzero(is_saturated)


def score_cyclic(sequence: np.ndarray, baseline: PatternBaseline) -> PatternScore:
    """CYCLIC: a sensor's normal periodicity disappears.

    For sensors that were periodic in the baseline (dominant non-DC FFT bin
    holding >= ``PATTERN_CYCLIC_MIN_BASELINE_POWER`` of non-DC power), the
    cycle is broken when the sequence's power fraction at that bin drops
    below ``PATTERN_CYCLIC_POWER_DROP_RATIO`` of the baseline fraction.
    Score = broken cyclic sensors / cyclic sensors. Returns 0.0 when the
    sequence length differs from the baseline's (FFT bins not comparable)."""
    seq_len = sequence.shape[0]
    if seq_len < PATTERN_CYCLIC_MIN_SEQ_LEN or seq_len != baseline.sequence_length:
        return 0.0, _EMPTY_INDICES
    cyclic_sensors = np.flatnonzero(
        baseline.dominant_power_fractions >= PATTERN_CYCLIC_MIN_BASELINE_POWER
    )
    if cyclic_sensors.size == 0:
        return 0.0, _EMPTY_INDICES
    broken_sensors: List[int] = []
    for sensor_idx in cyclic_sensors:
        power = _non_dc_power(sequence[:, sensor_idx])
        total_power = power.sum()
        current_fraction = (
            float(power[baseline.dominant_frequency_bins[sensor_idx]] / total_power)
            if total_power > NUMERICAL_EPSILON
            else 0.0
        )
        baseline_fraction = baseline.dominant_power_fractions[sensor_idx]
        if current_fraction < PATTERN_CYCLIC_POWER_DROP_RATIO * baseline_fraction:
            broken_sensors.append(int(sensor_idx))
    score = len(broken_sensors) / cyclic_sensors.size
    return float(score), np.array(broken_sensors, dtype=int)


def score_cascade(sequence: np.ndarray, baseline: PatternBaseline) -> PatternScore:
    """CASCADE: multiple sensors deviate strongly at the same time.

    A sensor participates when its mean |z-score| over the sequence exceeds
    ``PATTERN_CASCADE_MIN_SENSOR_Z``; a cascade requires at least
    ``PATTERN_CASCADE_MIN_SENSORS`` of them. Score = fraction of deviating
    sensors (0.0 below the minimum count)."""
    z = _z_scores(sequence, baseline)
    is_deviating = z.mean(axis=0) > PATTERN_CASCADE_MIN_SENSOR_Z
    affected = np.flatnonzero(is_deviating)
    if affected.size < PATTERN_CASCADE_MIN_SENSORS:
        return 0.0, _EMPTY_INDICES
    return float(is_deviating.mean()), affected


def _safe_corrcoef(rows: np.ndarray) -> np.ndarray:
    """Pearson correlation matrix with undefined (constant-row NaN) entries
    zeroed, so downstream means stay finite.

    ``np.corrcoef`` of a single variable returns a 0-d scalar; ``atleast_2d``
    ensures ``triu_indices_from`` always receives a 2-d matrix (BUG-001).
    """
    with np.errstate(invalid="ignore", divide="ignore"):
        correlations = np.corrcoef(rows)
    return np.atleast_2d(np.nan_to_num(correlations, nan=0.0))


def _non_dc_power(series: np.ndarray) -> np.ndarray:
    """Non-DC FFT power spectrum of a 1-D series (mean removed first)."""
    centered = series - series.mean()
    return np.abs(np.fft.rfft(centered))[1:] ** 2


def _baseline_spectra(sequences: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Per-sensor ``(dominant non-DC FFT bin, mean power fraction)`` arrays,
    each of shape ``(input_dim,)``; all-zero when sequences are too short."""
    num_sensors = sequences.shape[-1]
    bins = np.zeros(num_sensors, dtype=int)
    fractions = np.zeros(num_sensors, dtype=float)
    if sequences.shape[1] < PATTERN_CYCLIC_MIN_SEQ_LEN:
        return bins, fractions
    centered = sequences - sequences.mean(axis=1, keepdims=True)
    # (N, non_dc_bins, input_dim) -> mean over sequences -> (bins, input_dim)
    power = np.abs(np.fft.rfft(centered, axis=1))[:, 1:, :] ** 2
    mean_power = power.mean(axis=0)
    total_power = mean_power.sum(axis=0)
    for sensor_idx in range(num_sensors):
        if total_power[sensor_idx] <= NUMERICAL_EPSILON:
            continue
        bins[sensor_idx] = int(np.argmax(mean_power[:, sensor_idx]))
        fractions[sensor_idx] = float(
            mean_power[bins[sensor_idx], sensor_idx] / total_power[sensor_idx]
        )
    return bins, fractions


def _is_pair_lagged(x: np.ndarray, y: np.ndarray) -> bool:
    """True when the pair's best cross-correlation is at a non-zero shift."""
    max_lag = min(PATTERN_LAG_MAX_STEPS, len(x) // PATTERN_LAG_WINDOW_FRACTION)
    correlations_by_lag = {
        lag: abs(_lagged_correlation(x, y, lag))
        for lag in range(-max_lag, max_lag + 1)
    }
    best_lag = max(correlations_by_lag, key=correlations_by_lag.__getitem__)
    best_correlation = correlations_by_lag[best_lag]
    improvement = best_correlation - correlations_by_lag[0]
    return (
        best_lag != 0
        and best_correlation >= PATTERN_LAG_MIN_CORRELATION
        and improvement >= PATTERN_LAG_IMPROVEMENT_MARGIN
    )


def _lagged_correlation(x: np.ndarray, y: np.ndarray, lag: int) -> float:
    """Pearson correlation of ``x`` shifted by ``lag`` steps against ``y``."""
    if lag > 0:
        x_part, y_part = x[lag:], y[:-lag]
    elif lag < 0:
        x_part, y_part = x[:lag], y[-lag:]
    else:
        x_part, y_part = x, y
    return float(_safe_corrcoef(np.stack([x_part, y_part]))[0, 1])
