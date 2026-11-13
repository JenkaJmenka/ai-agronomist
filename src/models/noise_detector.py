"""Precision degradation / noise fault detector (SCADA spec section 5).

Two algorithm variants, selected by ``use_highpass``:

Production (``use_highpass=True``, default) — hybrid algorithm:
    1. Pass signal through 2nd-order high-pass Butterworth filter to
       reject low-frequency trends (drift, diurnal cycles, setpoint
       changes).
    2. Compute rolling variance of the filtered (noise-only) signal.
    3. Divide by calibrated baseline noise variance.
    4. Flag when the ratio exceeds the configured threshold.

Spec-faithful (``use_highpass=False``) — the framework spec §5 algorithm:
    plain rolling variance of the RAW signal divided by baseline variance,
    flag when the ratio exceeds ``theta_noise``.  No filtering step.  This
    path reproduces the spec's Table 5 verification dataset exactly.

Backward-compatible: the original ``detect(series, sensor)`` API still
works (falls through to the fixed-threshold path when baseline variance
is unavailable).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
from scipy.signal import butter, filtfilt

try:
    from src.config import (
        NOISE_ALPHA,
        NOISE_BASELINE_VARIANCE,
        NOISE_HYBRID_WINDOW,
        NOISE_RATIO_THRESHOLD,
        SENSOR_NAMES,
        SENSOR_NOISE_TARGETS,
    )
except ImportError:  # pragma: no cover
    NOISE_ALPHA: float = 3.0  # type: ignore[no-redef]
    SENSOR_NOISE_TARGETS: dict[str, float] = {  # type: ignore[no-redef]
        "temperature": 0.3,
        "humidity": 1.0,
        "co2": 10.0,
        "light": 30.0,
    }
    NOISE_HYBRID_WINDOW: int = 5  # type: ignore[no-redef]
    NOISE_BASELINE_VARIANCE: dict[str, float] = {  # type: ignore[no-redef]
        "temperature": 0.01,
        "humidity": 0.04,
        "co2": 25.0,
        "light": 100.0,
    }
    NOISE_RATIO_THRESHOLD: float = 10.0  # type: ignore[no-redef]
    SENSOR_NAMES: list[str] = [  # type: ignore[no-redef]
        "temperature",
        "humidity",
        "co2",
        "light",
    ]

# filtfilt with a 2nd-order Butterworth needs > 3 * max(len(a), len(b)) = 9
# samples, so the high-pass path requires at least this many readings.
_MIN_HIGHPASS_SAMPLES: int = 10


def _highpass_filter(series: np.ndarray, cutoff: float) -> np.ndarray:
    """Zero-phase 2nd-order Butterworth high-pass. Shared with calibration."""
    b, a = butter(N=2, Wn=cutoff, btype="high")
    return filtfilt(b, a, np.asarray(series, dtype=float))


class NoiseDetector:
    """Detect precision degradation via a rolling variance ratio.

    Args:
        hp_cutoff: High-pass Butterworth cutoff (only used when
            ``use_highpass`` is True).
        window: Rolling variance window ``w`` (spec §5: ``w = 5``).
        baseline_variance: Per-sensor calibrated baseline noise variance
            (spec §5: ``sigma_baseline^2 = 0.02``).
        ratio_threshold: Variance-ratio alarm threshold ``theta_noise``
            (spec §5: ``theta_noise = 50.0``).
        use_highpass: When True (default) apply the Butterworth high-pass
            filter before computing rolling variance (production behaviour).
            When False, compute the ratio on the RAW series — the
            spec-faithful path that reproduces Table 5.
    """

    HP_CUTOFF: float = 0.1

    def __init__(
        self,
        hp_cutoff: float | None = None,
        window: int = NOISE_HYBRID_WINDOW,
        baseline_variance: Dict[str, float] | None = None,
        ratio_threshold: float = NOISE_RATIO_THRESHOLD,
        use_highpass: bool = True,
    ) -> None:
        self._hp_cutoff = hp_cutoff or self.HP_CUTOFF
        self._window = window
        self._baseline_var = dict(baseline_variance or NOISE_BASELINE_VARIANCE)
        self._ratio_threshold = ratio_threshold
        self._use_highpass = use_highpass

    def detect(
        self,
        series: np.ndarray,
        sensor: str,
        *,
        target_override: float | None = None,
    ) -> bool:
        """Return True if precision degradation detected (single flag).

        Backward-compatible API.  Uses the Butterworth HP filter and
        falls back to the legacy fixed-threshold check when no baseline
        variance is configured for the sensor.

        Args:
            series: 1-D float array of sensor readings (length = seq_len).
            sensor: Sensor name key.
            target_override: Optional seasonal σ-scaled noise target
                (physical units). When set, replaces SENSOR_NOISE_TARGETS.

        Returns:
            True if precision degradation detected.
        """
        if len(series) < _MIN_HIGHPASS_SAMPLES:
            return False

        signal = self._highpass(series) if self._use_highpass else np.asarray(
            series, dtype=float
        )

        bv = self._baseline_var.get(sensor)
        if bv is not None and bv > 0:
            signal_var = float(np.var(signal))
            return signal_var / bv > self._ratio_threshold

        target = target_override if target_override is not None else SENSOR_NOISE_TARGETS.get(sensor)
        if target is None:
            return False
        signal_std = float(np.std(signal))
        return signal_std > target * NOISE_ALPHA

    def detect_series(
        self,
        series: np.ndarray,
        sensor: str,
        baseline_variance: Optional[float] = None,
    ) -> List[bool]:
        """Per-timestep noise detection using rolling variance ratio.

        On the high-pass path the series must contain at least
        ``_MIN_HIGHPASS_SAMPLES`` readings (a ``filtfilt`` requirement);
        on the spec-faithful raw path only ``window`` readings are needed.
        The first ``window - 1`` entries are always ``False`` (warm-up).

        Args:
            series: 1-D array of sensor readings.
            sensor: Sensor name key.
            baseline_variance: Override per-sensor calibrated baseline
                variance.  If ``None``, uses the configured default.

        Returns:
            List of booleans; ``True`` = noise anomaly at that timestep.
        """
        series = np.asarray(series, dtype=float)
        n = len(series)
        w = self._window
        flags: List[bool] = [False] * n

        bv = baseline_variance or self._baseline_var.get(sensor)
        if bv is None or bv <= 0:
            return flags

        if self._use_highpass:
            if n < _MIN_HIGHPASS_SAMPLES:
                return flags
            signal = self._highpass(series)
        else:
            if n < w:
                return flags
            signal = series

        # Warm-up honours the actual window: the first evaluable index is
        # w - 1 (the earliest timestep whose window is full).
        for t in range(w - 1, n):
            window = signal[t - w + 1 : t + 1]
            var = float(np.var(window, ddof=1))
            flags[t] = var / bv > self._ratio_threshold

        return flags

    def _highpass(self, series: np.ndarray) -> np.ndarray:
        """Apply zero-phase Butterworth high-pass filter."""
        return _highpass_filter(series, self._hp_cutoff)


def _iter_windows(
    windows: np.ndarray | Sequence[np.ndarray],
) -> List[np.ndarray]:
    """Normalize ``(n, seq, sensors)`` or a sequence of 2-D windows."""
    if isinstance(windows, np.ndarray):
        if windows.ndim == 3:
            return [np.asarray(windows[i], dtype=float) for i in range(len(windows))]
        if windows.ndim == 2:
            return [np.asarray(windows, dtype=float)]
        raise ValueError(
            f"windows array must be 2-D or 3-D, got {windows.ndim}D"
        )
    return [np.asarray(window, dtype=float) for window in windows]


def calibrate_noise_baseline(
    windows: np.ndarray | Sequence[np.ndarray],
    sensor_names: Sequence[str] | None = None,
    *,
    hp_cutoff: float = NoiseDetector.HP_CUTOFF,
) -> Dict[str, float]:
    """Per-sensor median high-pass variance from clean windows.

    Matches the July prototype: high-pass each channel (2nd-order
    Butterworth, same cutoff as :class:`NoiseDetector`), take the
    population variance (``ddof=0``, the statistic ``detect`` divides by
    the baseline), then the median across windows.

    Args:
        windows: ``(n_windows, seq_len, n_sensors)`` or a sequence of
            ``(seq_len, n_sensors)`` arrays in physical units.
        sensor_names: Column names. Defaults to ``SENSOR_NAMES``.
        hp_cutoff: Butterworth cutoff. Default is the detector's.

    Returns:
        Map of sensor name to median high-pass variance. A channel with
        no usable window (shorter than the filtfilt minimum, or
        non-finite) is omitted so the caller can keep the fixed
        ``NOISE_BASELINE_VARIANCE`` entry for that sensor.

    The pipeline should pass the merged map into
    ``NoiseDetector(baseline_variance=..., ratio_threshold=NOISE_CALIBRATED_RATIO_TARGET)``.
    ``NOISE_RATIO_THRESHOLD`` stays the cold-start ratio for the fixed
    baselines; the calibrated target is a separate constant because
    raising the cold-start ratio does not replace calibration.
    """
    names = list(sensor_names or SENSOR_NAMES)
    if not names:
        raise ValueError("sensor_names must be non-empty")
    if not 0.0 < hp_cutoff < 1.0:
        raise ValueError(f"hp_cutoff must be in (0, 1), got {hp_cutoff}")

    samples: Dict[str, List[float]] = {name: [] for name in names}
    for window in _iter_windows(windows):
        if window.ndim != 2:
            raise ValueError(
                f"each window must be 2-D (seq_len, sensors), got {window.ndim}D"
            )
        n_cols = min(window.shape[1], len(names))
        for idx in range(n_cols):
            column = window[:, idx]
            if column.size < _MIN_HIGHPASS_SAMPLES or not np.isfinite(column).all():
                continue
            filtered = _highpass_filter(column, hp_cutoff)
            samples[names[idx]].append(float(np.var(filtered)))

    return {
        name: float(np.median(values))
        for name, values in samples.items()
        if values
    }


def resolve_noise_baseline(
    calibrated: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Overlay calibrated variances on the fixed cold-start map.

    A finite positive calibration may only loosen a channel whose fixed
    ``NOISE_BASELINE_VARIANCE`` entry exists and is positive: the stored
    value is ``max(calibrated, fixed)``. A near-flat window (high-pass
    variance far below that floor) must not replace the floor. Missing,
    non-finite, or non-positive calibrations keep the fixed map. An
    empty calibration therefore returns the fixed fallback unchanged.
    A channel with no positive fixed entry still takes the calibrated
    value.
    """
    merged = dict(NOISE_BASELINE_VARIANCE)
    if not calibrated:
        return merged
    for name, value in calibrated.items():
        try:
            variance = float(value)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(variance) or variance <= 0.0:
            continue
        key = str(name)
        fixed = _positive_fixed_variance(key)
        if fixed is None:
            merged[key] = variance
            continue
        merged[key] = max(variance, fixed)
    return merged


def _positive_fixed_variance(name: str) -> float | None:
    """Positive ``NOISE_BASELINE_VARIANCE`` entry, or None when it is absent."""
    raw = NOISE_BASELINE_VARIANCE.get(name)
    if raw is None:
        return None
    try:
        fixed = float(raw)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(fixed) or fixed <= 0.0:
        return None
    return fixed
