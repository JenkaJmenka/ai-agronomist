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

from typing import Dict, List, Optional

import numpy as np
from scipy.signal import butter, filtfilt

try:
    from src.config import (
        NOISE_ALPHA,
        NOISE_BASELINE_VARIANCE,
        NOISE_HYBRID_WINDOW,
        NOISE_RATIO_THRESHOLD,
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

# filtfilt with a 2nd-order Butterworth needs > 3 * max(len(a), len(b)) = 9
# samples, so the high-pass path requires at least this many readings.
_MIN_HIGHPASS_SAMPLES: int = 10


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
        b, a = butter(N=2, Wn=self._hp_cutoff, btype="high")
        return filtfilt(b, a, series.astype(float))
