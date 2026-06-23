"""
Two-sided CUSUM drift detector for slow calibration drift (SCADA spec section 1.3).

Separate from the reconstruction-error based ``DriftDetector`` in
``drift_detector.py`` (Gap 3 — Online Learning Pipeline), which monitors
model-level performance degradation.  This module detects per-sensor
*calibration* drift by tracking cumulative deviations of standardised
residuals from zero.

The detector is stateful and serialisable so it can be persisted per
tenant (e.g. in ``LearningState.state_json``).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

try:
    from src.config import CUSUM_H_SIGMA, CUSUM_K_SIGMA
except ImportError:  # pragma: no cover
    CUSUM_K_SIGMA = 0.5
    CUSUM_H_SIGMA = 5.0


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
