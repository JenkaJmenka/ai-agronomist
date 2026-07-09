"""
Two-sided CUSUM drift detector for slow calibration drift (SCADA spec section 1.3).

Separate from the reconstruction-error based ``DriftDetector`` in
``drift_detector.py`` (Gap 3 — Online Learning Pipeline), which monitors
model-level performance degradation.  This module detects per-sensor
*calibration* drift by tracking cumulative deviations of standardised
residuals from zero.

The detector is stateful and serialisable so it can be persisted per
tenant (e.g. in ``LearningState.state_json``).

Also provides :class:`EMABaselineEstimator` — a lightweight helper that
computes the predicted value ``x_hat_t`` and a rolling standard deviation
so callers can produce the standardised residuals CUSUM expects.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

try:
    from src.config import CUSUM_EMA_ALPHA, CUSUM_H_SIGMA, CUSUM_K_SIGMA
except ImportError:  # pragma: no cover
    CUSUM_K_SIGMA = 0.5
    CUSUM_H_SIGMA = 5.0
    CUSUM_EMA_ALPHA = 0.01


class CUSUMDriftDetector:
    """Stateful two-sided CUSUM detector for slow calibration drift.

    Standardised residual = ``(observed - baseline_mean) / baseline_std``.

    * ``S+`` accumulates positive deviations above the allowance ``k``.
    * ``S-`` accumulates negative deviations below ``-k``.
    * An alarm fires when either accumulator exceeds the decision
      interval ``h``.

    Typical values (Hawkins & Olwell, *Cumulative Sum Charts*):
    ``k = 0.5`` standard deviations, ``h = 5.0``.

    Example::

        cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        for residual in standardised_stream:
            if cusum.update("temperature", residual):
                print("Drift alarm on temperature!")
                cusum.reset("temperature")
    """

    def __init__(self, k: float = CUSUM_K_SIGMA, h: float = CUSUM_H_SIGMA) -> None:
        """
        Args:
            k: Allowance parameter — deviations below ``k`` standard
                deviations are absorbed, preventing false alarms from
                normal noise.
            h: Decision interval — alarm threshold for the cumulative
                sum accumulators.

        Raises:
            ValueError: If ``k`` or ``h`` are negative.
        """
        if k < 0:
            raise ValueError(f"Allowance k must be non-negative, got {k}")
        if h < 0:
            raise ValueError(f"Decision interval h must be non-negative, got {h}")
        self.k = k
        self.h = h
        self._s_pos: dict[str, float] = defaultdict(float)
        self._s_neg: dict[str, float] = defaultdict(float)

    def update(self, sensor: str, standardised_residual: float) -> bool:
        """Update CUSUM accumulators for one sensor reading.

        Args:
            sensor: Sensor channel name (e.g. ``"temperature"``).
            standardised_residual: ``(observed - baseline_mean) / baseline_std``.

        Returns:
            True if either accumulator exceeds the decision interval ``h``
            (drift alarm fires).
        """
        self._s_pos[sensor] = max(
            0.0, self._s_pos[sensor] + standardised_residual - self.k
        )
        self._s_neg[sensor] = max(
            0.0, self._s_neg[sensor] - standardised_residual - self.k
        )
        return self._s_pos[sensor] > self.h or self._s_neg[sensor] > self.h

    def reset(self, sensor: str) -> None:
        """Reset CUSUM accumulators for a sensor after alarm is acknowledged.

        Args:
            sensor: Sensor channel name to reset.
        """
        self._s_pos[sensor] = 0.0
        self._s_neg[sensor] = 0.0

    def alarmed_sensors(self) -> list[str]:
        """Return sensor names currently in alarm state.

        Returns:
            List of sensor names where at least one accumulator exceeds ``h``.
        """
        return [
            s
            for s in self._s_pos
            if self._s_pos[s] > self.h or self._s_neg[s] > self.h
        ]

    def to_dict(self) -> dict[str, Any]:
        """Serialise detector state for persistence (JSON-safe).

        Returns:
            Dict with ``k``, ``h``, ``s_pos``, and ``s_neg`` entries.
        """
        return {
            "k": self.k,
            "h": self.h,
            "s_pos": dict(self._s_pos),
            "s_neg": dict(self._s_neg),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CUSUMDriftDetector:
        """Deserialise detector from a previously exported dict.

        Args:
            d: Dict as returned by :meth:`to_dict`.

        Returns:
            A restored ``CUSUMDriftDetector`` with the same accumulator state.
        """
        k = float(d.get("k", CUSUM_K_SIGMA))
        h = float(d.get("h", CUSUM_H_SIGMA))
        if k < 0 or h < 0:
            raise ValueError("k and h must be non-negative")

        obj = cls(k=k, h=h)

        s_pos_in = d.get("s_pos", {})
        if not isinstance(s_pos_in, dict):
            raise TypeError("s_pos must be a dictionary")
        obj._s_pos = defaultdict(float, {str(k): float(v) for k, v in s_pos_in.items()})

        s_neg_in = d.get("s_neg", {})
        if not isinstance(s_neg_in, dict):
            raise TypeError("s_neg must be a dictionary")
        obj._s_neg = defaultdict(float, {str(k): float(v) for k, v in s_neg_in.items()})

        return obj


class EMABaselineEstimator:
    """Exponential Moving Average baseline for producing standardised residuals.

    Maintains per-sensor EMA of the mean and variance so that the CUSUM
    detector receives ``(observed - ema_mean) / ema_std`` at each step.

    Args:
        alpha: Smoothing factor in ``(0, 1]``.  Smaller values give a
            longer memory (production default 0.01 ≈ 100-sample span).
        min_std: Floor for the rolling standard deviation to prevent
            division by zero during initial warm-up.
    """

    def __init__(
        self,
        alpha: float = CUSUM_EMA_ALPHA,
        min_std: float = 1e-6,
    ) -> None:
        if not 0 < alpha <= 1:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self._alpha = alpha
        self._min_std = min_std
        self._mean: dict[str, float] = {}
        self._var: dict[str, float] = {}

    def update(self, sensor: str, value: float) -> tuple[float, float]:
        """Incorporate a new observation and return the standardised residual.

        Args:
            sensor: Sensor channel name.
            value: Raw sensor reading.

        Returns:
            ``(standardised_residual, ema_mean)`` tuple.  On the very
            first call for a sensor the residual is ``0.0``.
        """
        if sensor not in self._mean:
            self._mean[sensor] = value
            self._var[sensor] = 0.0
            return 0.0, value

        prev_mean = self._mean[sensor]
        diff = value - prev_mean

        self._mean[sensor] = prev_mean + self._alpha * diff
        self._var[sensor] = (1 - self._alpha) * (self._var[sensor] + self._alpha * diff * diff)

        std = max(math.sqrt(self._var[sensor]), self._min_std)
        residual = (value - prev_mean) / std
        return residual, self._mean[sensor]

    def to_dict(self) -> dict[str, Any]:
        """Serialise estimator state (JSON-safe)."""
        return {
            "alpha": self._alpha,
            "min_std": self._min_std,
            "mean": dict(self._mean),
            "var": dict(self._var),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EMABaselineEstimator:
        """Restore estimator from a previously exported dict."""
        obj = cls(
            alpha=float(d.get("alpha", CUSUM_EMA_ALPHA)),
            min_std=float(d.get("min_std", 1e-6)),
        )
        obj._mean = {str(k): float(v) for k, v in d.get("mean", {}).items()}
        obj._var = {str(k): float(v) for k, v in d.get("var", {}).items()}
        return obj
