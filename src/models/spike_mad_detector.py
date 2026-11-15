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

Reversion confirmation (``reversion_offsets`` / ``detect_confirmed``):
the causal test above fires on the first sample of a spike AND on the first
sample of a step, because both leave the trailing median behind. A spike
comes back; a step does not. A flagged sample is confirmed when one of the
next ``horizon`` samples lies within ``fraction * |x_t - median_t|`` of
``median_t``, the pre-event level. Confirmation needs samples after ``t``,
so it is for callers that have them (a whole upload, or an enrichment
window that votes on an earlier sample).
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from src.config import SPIKE_MAD_TUNING_C, SPIKE_MAD_WINDOW_K, SPIKE_REVERT_FRACTION


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

    def reversion_offsets(
        self,
        series: np.ndarray,
        mad_floor: float = 0.0,
        *,
        horizon: int,
        fraction: float = SPIKE_REVERT_FRACTION,
    ) -> List[Optional[int]]:
        """Offset of the first reverting sample after each flagged timestep.

        A timestep ``t`` flagged by :meth:`detect` gets the smallest ``j`` in
        ``1..horizon`` for which ``|x[t+j] - median_t| <= fraction * |x_t -
        median_t|``; ``median_t`` is the rolling median the flag was raised
        against. ``None`` means not flagged, or flagged with no reverting
        sample inside the horizon (a step, a ramp, or the end of the series).
        Non-finite following samples never confirm.

        Args:
            series: 1-D array of sensor readings.
            mad_floor: As for :meth:`detect`.
            horizon: How many following samples may confirm (>= 1).
            fraction: Share of the deviation the series may keep and still
                count as back at the pre-event level (0 < fraction < 1).

        Returns:
            One entry per timestep: an ``int`` offset or ``None``.
        """
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1, got {horizon}")
        if not 0.0 < fraction < 1.0:
            raise ValueError(f"fraction must be in (0, 1), got {fraction}")
        series = np.asarray(series, dtype=float)
        n = len(series)
        offsets: List[Optional[int]] = [None] * n
        if n < self._k:
            return offsets

        for t in range(self._k - 1, n):
            window = series[t - self._k + 1 : t + 1]
            median = float(np.median(window))
            mad = float(np.median(np.abs(window - median)))
            effective_mad = max(mad, mad_floor)
            delta = abs(series[t] - median)
            if not (effective_mad > 0 and delta > self._c * effective_mad):
                continue
            tolerance = fraction * delta
            for j in range(1, horizon + 1):
                if t + j >= n:
                    break
                if abs(series[t + j] - median) <= tolerance:
                    offsets[t] = j
                    break

        return offsets

    def detect_confirmed(
        self,
        series: np.ndarray,
        mad_floor: float = 0.0,
        *,
        horizon: int,
        fraction: float = SPIKE_REVERT_FRACTION,
    ) -> List[bool]:
        """Flags from :meth:`detect` that revert within ``horizon`` samples."""
        return [
            offset is not None
            for offset in self.reversion_offsets(
                series, mad_floor, horizon=horizon, fraction=fraction
            )
        ]
