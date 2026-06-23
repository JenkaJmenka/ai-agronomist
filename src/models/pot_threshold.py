"""
Peaks Over Threshold (POT) adaptive anomaly threshold using EVT/GPD.

Maintains a circular buffer of recent anomaly residuals and periodically
re-fits a Generalized Pareto Distribution to the tail to derive a
statistically grounded detection threshold.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Minimum buffer fill before computing any threshold.
_MIN_BUFFER_FILL: int = 20

# Re-fit the GPD after this many new observations.
_REFIT_INTERVAL: int = 50


class POTThresholdFitter:
    """EVT-based adaptive threshold via Peaks Over Threshold.

    Args:
        q: Quantile of the buffer used as the initial threshold for
            extracting peaks (exceedances).
        fpr: Target false-positive rate for the final threshold.
        min_peaks: Minimum number of peaks required to attempt a GPD fit;
            falls back to a simple quantile otherwise.
        buffer_size: Capacity of the circular residual buffer.
    """

    def __init__(
        self,
        q: float = 0.90,
        fpr: float = 0.001,
        min_peaks: int = 10,
        buffer_size: int = 500,
    ) -> None:
        self._q = q
        self._fpr = fpr
        self._min_peaks = min_peaks
        self._buffer: deque[float] = deque(maxlen=buffer_size)
        self._threshold: float = float("inf")
        self._n_since_fit: int = 0

    def update(self, new_residual: float) -> None:
        """Append a residual and re-fit when interval is reached.

        Args:
            new_residual: Latest anomaly reconstruction error.
        """
        self._buffer.append(float(new_residual))
        self._n_since_fit += 1

        if self._n_since_fit >= _REFIT_INTERVAL and len(self._buffer) >= _MIN_BUFFER_FILL:
            self._threshold = self.fit()
            self._n_since_fit = 0

    def fit(self) -> float:
        """Fit GPD to exceedances and return the anomaly threshold.

        Steps:
            1. Extract peaks above the ``q``-quantile of the buffer.
            2. If fewer than ``min_peaks`` peaks: fall back to a simple
               ``(1 - fpr)``-quantile of the buffer.
            3. Fit ``scipy.stats.genpareto`` to the exceedances.
            4. If the fit fails: fall back to the quantile method.

        Returns:
            The computed anomaly threshold.
        """
        arr = np.array(self._buffer, dtype=np.float64)
        high_quantile = float(np.quantile(arr, self._q))
        peaks = arr[arr > high_quantile] - high_quantile

        if len(peaks) < self._min_peaks:
            return float(np.quantile(arr, 1.0 - self._fpr))

        try:
            from scipy.stats import genpareto  # noqa: PLC0415

            params = genpareto.fit(peaks, floc=0)
            gpd_threshold = high_quantile + float(
                genpareto.ppf(1.0 - self._fpr, *params)
            )
            if not np.isfinite(gpd_threshold):
                raise ValueError("GPD ppf returned non-finite value")
            return gpd_threshold
        except Exception:
            logger.warning(
                "GPD fit failed; falling back to quantile threshold",
                exc_info=True,
            )
            return float(np.quantile(arr, 1.0 - self._fpr))

    def get_threshold(self) -> float:
        """Return the last fitted threshold.

        Returns ``float("inf")`` until the buffer has at least
        ``_MIN_BUFFER_FILL`` observations.
        """
        if len(self._buffer) < _MIN_BUFFER_FILL:
            return float("inf")
        return self._threshold

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for JSON persistence."""
        return {
            "q": self._q,
            "fpr": self._fpr,
            "min_peaks": self._min_peaks,
            "buffer_size": self._buffer.maxlen,
            "buffer": list(self._buffer),
            "threshold": self._threshold,
            "n_since_fit": self._n_since_fit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> POTThresholdFitter:
        """Reconstruct a fitter from a previously serialized dict."""
        fitter = cls(
            q=d["q"],
            fpr=d["fpr"],
            min_peaks=d["min_peaks"],
            buffer_size=d.get("buffer_size", 500),
        )
        for val in d.get("buffer", []):
            fitter._buffer.append(val)
        fitter._threshold = d.get("threshold", float("inf"))
        fitter._n_since_fit = d.get("n_since_fit", 0)
        return fitter
