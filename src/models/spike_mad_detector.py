"""Spike / transient outlier detector using rolling Median Absolute Deviation.

Flags timesteps where the absolute difference between the current reading
and the rolling median exceeds a tunable multiple of the rolling MAD.
Unlike simple delta / velocity filters, the MAD-based threshold is robust
to outliers contaminating the reference window.

Algorithm:
    median_t = median(x[t-k+1 : t+1])
    MAD_t    = median(|x[t-k+1 : t+1] - median_t|)
    flag     = |x_t - median_t| > c * max(MAD_t, mad_floor)

The optional ``mad_floor`` clamps the rolling MAD from below: on a
near-constant series the MAD collapses toward zero and ``delta > c * MAD``
flags ordinary measurement noise. Callers pass the sensor's noise floor in
the same physical units as the series (see ``SPIKE_MAD_FLOOR`` in config).
"""

from __future__ import annotations

from typing import List

import numpy as np

from src.config import SPIKE_MAD_TUNING_C, SPIKE_MAD_WINDOW_K


class SpikeMADDetector:
    """Detect transient spike anomalies via rolling median + MAD threshold."""

    def __init__(
        self,
        window_k: int = SPIKE_MAD_WINDOW_K,
        tuning_c: float = SPIKE_MAD_TUNING_C,
    ) -> None:
        if window_k < 2:
            raise ValueError(f"window_k must be >= 2, got {window_k}")
        if tuning_c <= 0:
            raise ValueError(f"tuning_c must be > 0, got {tuning_c}")
        self._k = window_k
        self._c = tuning_c

    def detect(self, series: np.ndarray, mad_floor: float = 0.0) -> List[bool]:
        """Flag each timestep as anomalous (spike) or normal.

        Args:
            series: 1-D array of sensor readings.
            mad_floor: Minimum MAD (physical units) used for thresholding;
                guards against false positives when the window is
                near-constant and the rolling MAD collapses to ~0.

        Returns:
            List of booleans, one per timestep.  ``True`` = spike detected.
            The first ``k-1`` entries are always ``False`` (insufficient
            window).
        """
        series = np.asarray(series, dtype=float)
        n = len(series)
        flags: List[bool] = [False] * n

        if n < self._k:
            return flags

        for t in range(self._k - 1, n):
            window = series[t - self._k + 1 : t + 1]
            median = float(np.median(window))
            mad = float(np.median(np.abs(window - median)))
            effective_mad = max(mad, mad_floor)
            delta = abs(series[t] - median)
            if effective_mad > 0 and delta > self._c * effective_mad:
                flags[t] = True

        return flags
