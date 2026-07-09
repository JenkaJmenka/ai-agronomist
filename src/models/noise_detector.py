"""Precision degradation / noise fault detector (SCADA spec section 1.4).

Hybrid algorithm (Butterworth HP filter + variance ratio):
    1. Pass signal through 2nd-order high-pass Butterworth filter to
       reject low-frequency trends (drift, diurnal cycles, setpoint
       changes).
    2. Compute rolling variance of the filtered (noise-only) signal.
    3. Divide by calibrated baseline noise variance.
    4. Flag when the ratio exceeds the configured threshold.

The hybrid approach combines trend immunity from the Butterworth filter
with adaptive sensitivity from the variance ratio.

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


class NoiseDetector:
    """Detect precision degradation via HP-filtered rolling variance ratio."""

    HP_CUTOFF: float = 0.1

    def __init__(
        self,
        hp_cutoff: float | None = None,
        window: int = NOISE_HYBRID_WINDOW,
        baseline_variance: Dict[str, float] | None = None,
        ratio_threshold: float = NOISE_RATIO_THRESHOLD,
    ) -> None:
        self._hp_cutoff = hp_cutoff or self.HP_CUTOFF
        self._window = window
        self._baseline_var = dict(baseline_variance or NOISE_BASELINE_VARIANCE)
        self._ratio_threshold = ratio_threshold

    def detect(
        self,
        series: np.ndarray,
        sensor: str,
    ) -> bool:
        """Return True if precision degradation detected (single flag).

        Backward-compatible API.  Uses the Butterworth HP filter and
        falls back to the legacy fixed-threshold check when no baseline
        variance is configured for the sensor.

        Args:
            series: 1-D float array of sensor readings (length = seq_len).
            sensor: Sensor name key.

        Returns:
            True if precision degradation detected.
        """
        if len(series) < 10:
            return False

        filtered = self._highpass(series)

        bv = self._baseline_var.get(sensor)
        if bv is not None and bv > 0:
            filtered_var = float(np.var(filtered))
            return filtered_var / bv > self._ratio_threshold

        target = SENSOR_NOISE_TARGETS.get(sensor)
        if target is None:
            return False
        filtered_std = float(np.std(filtered))
        return filtered_std > target * NOISE_ALPHA

    def detect_series(
        self,
        series: np.ndarray,
        sensor: str,
        baseline_variance: Optional[float] = None,
    ) -> List[bool]:
        """Per-timestep noise detection using rolling variance ratio.

        Args:
            series: 1-D array of sensor readings.
            sensor: Sensor name key.
            baseline_variance: Override per-sensor calibrated baseline
                variance.  If ``None``, uses the configured default.

        Returns:
            List of booleans; ``True`` = noise anomaly at that timestep.
            First ``max(window-1, 9)`` entries are ``False`` (warm-up).
        """
        series = np.asarray(series, dtype=float)
        n = len(series)
        flags: List[bool] = [False] * n

        if n < 10:
            return flags

        bv = baseline_variance or self._baseline_var.get(sensor)
        if bv is None or bv <= 0:
            return flags

        filtered = self._highpass(series)
        w = self._window

        for t in range(max(w - 1, 9), n):
            window = filtered[t - w + 1 : t + 1]
            var = float(np.var(window, ddof=1))
            flags[t] = var / bv > self._ratio_threshold

        return flags

    def _highpass(self, series: np.ndarray) -> np.ndarray:
        """Apply zero-phase Butterworth high-pass filter."""
        b, a = butter(N=2, Wn=self._hp_cutoff, btype="high")
        return filtfilt(b, a, series.astype(float))
