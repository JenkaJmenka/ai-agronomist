"""
Exponentially Weighted Moving Average (EWMA) anomaly threshold tracker.

Maintains a running mean and variance of anomaly scores to derive a
dynamic detection threshold that adapts to the tenant's data distribution.
"""

from __future__ import annotations

import math
from typing import Any


class EWMATracker:
    """Adaptive threshold via EWMA of anomaly scores.

    The threshold is ``ewma_mean + k * sqrt(ewma_var)``.  Until at least
    ``_MIN_OBS`` observations have been seen, ``get_threshold()`` returns
    ``float("inf")`` to disable dynamic thresholding during warm-up.

    Args:
        alpha: Smoothing factor for the exponential average (0 < alpha <= 1).
        k: Number of standard deviations above the mean for the threshold.
    """

    _MIN_OBS: int = 10

    def __init__(self, alpha: float = 0.1, k: float = 3.0) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        if k < 0:
            raise ValueError(f"k must be non-negative, got {k}")

        self._alpha = alpha
        self._k = k
        self._mean: float = 0.0
        self._var: float = 0.0
        self._n: int = 0

    def update(self, score: float) -> float:
        """Update running EWMA statistics with a new score.

        Uses an exponentially-weighted adaptation of Welford's online
        algorithm for mean and variance tracking.

        Args:
            score: The latest anomaly reconstruction error.

        Returns:
            The current dynamic threshold after incorporating this score.
        """
        if not math.isfinite(score):
            raise ValueError(f"score must be finite, got {score}")

        self._n += 1

        if self._n == 1:
            self._mean = score
            self._var = 0.0
        else:
            self._mean = self._alpha * score + (1.0 - self._alpha) * self._mean
            self._var = (
                self._alpha * (score - self._mean) ** 2
                + (1.0 - self._alpha) * self._var
            )

        return self.get_threshold()

    def get_threshold(self) -> float:
        """Return the current dynamic threshold.

        Returns:
            ``ewma_mean + k * sqrt(ewma_var)`` when enough observations
            have been collected; ``float("inf")`` otherwise.
        """
        if self._n < self._MIN_OBS:
            return float("inf")
        return self._mean + self._k * math.sqrt(max(self._var, 0.0))

    def is_anomaly(self, score: float) -> bool:
        """Check whether a score exceeds the current threshold."""
        return score > self.get_threshold()

    def to_dict(self) -> dict[str, Any]:
        """Serialize tracker state for JSON persistence."""
        return {
            "alpha": self._alpha,
            "k": self._k,
            "mean": self._mean,
            "var": self._var,
            "n": self._n,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EWMATracker:
        """Reconstruct a tracker from a previously serialized dict."""
        tracker = cls(alpha=d["alpha"], k=d["k"])
        tracker._mean = d["mean"]
        tracker._var = d["var"]
        tracker._n = d["n"]
        return tracker
