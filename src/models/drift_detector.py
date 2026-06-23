"""
Drift detection (Gap 3 — Online Learning Pipeline).

Detects when model performance degrades by comparing recent reconstruction
losses against the historical baseline. A sustained, normalized increase
above the threshold indicates the data distribution has drifted and the
model should be retrained rather than incrementally updated.

Also provides :class:`CUSUMDriftDetector` — a per-sensor CUSUM (Cumulative
Sum) detector that operates on standardised residuals and fires an alarm when
the cumulative deviation exceeds a decision interval.

CUSUM stub — final implementation delivered by ml-engineer.  The public
interface (constructor, update, alarmed_sensors, to_dict, from_dict) is final
so the pipeline can import and wire it immediately.
"""

import logging
from collections import deque
from typing import Any, ClassVar, Dict

import numpy as np

from src.config import CUSUM_H_SIGMA, CUSUM_K_SIGMA, DRIFT_THRESHOLD, DRIFT_WINDOW_SIZE, NUMERICAL_EPSILON

logger = logging.getLogger(__name__)


class DriftDetector:
    """
    Detect data drift from reconstruction-error statistics.

    Keeps a rolling history of 2 * window_size losses: the newest window_size
    values form the "recent" window, the rest form the historical baseline.

    Example:
        detector = DriftDetector(window_size=100, threshold=0.3)
        for loss in loss_stream:
            status = detector.update(loss)
            if status["drift_detected"]:
                schedule_retraining()
    """

    def __init__(
        self,
        window_size: int = DRIFT_WINDOW_SIZE,
        threshold: float = DRIFT_THRESHOLD,
    ) -> None:
        """
        Args:
            window_size: Number of losses in the recent comparison window.
            threshold: Normalized recent-vs-historical increase that flags drift.
        """
        self.window_size = window_size
        self.threshold = threshold
        self.losses: deque = deque(maxlen=window_size * 2)

    def update(self, loss: float) -> Dict[str, Any]:
        """
        Record a new loss value and evaluate drift.

        Args:
            loss: Latest reconstruction loss.

        Returns:
            Dict with drift_detected, drift_score, recent_avg, historical_avg
            and action ('retrain_model' or 'continue_learning'). While fewer
            than window_size + 1 losses have been seen (no historical data
            yet), returns drift_detected=False with a warning instead.
        """
        self.losses.append(loss)

        if len(self.losses) <= self.window_size:
            return {
                "drift_detected": False,
                "drift_score": 0.0,
                "warning": (
                    f"Insufficient data ({len(self.losses)}/{self.window_size})"
                ),
            }

        history = list(self.losses)
        recent = float(np.mean(history[-self.window_size:]))
        historical = float(np.mean(history[:-self.window_size]))

        drift = (recent - historical) / (historical + NUMERICAL_EPSILON)
        drift_detected = drift > self.threshold

        if drift_detected:
            logger.warning(
                "Drift detected: score=%.4f (recent=%.6f, historical=%.6f)",
                drift,
                recent,
                historical,
            )

        return {
            "drift_detected": drift_detected,
            "drift_score": float(drift),
            "recent_avg": recent,
            "historical_avg": historical,
            "action": "retrain_model" if drift_detected else "continue_learning",
        }


class CUSUMDriftDetector:
    """Per-sensor CUSUM drift detector operating on standardised residuals.

    Maintains two one-sided CUSUM statistics (upper S⁺ and lower S⁻) for each
    sensor.  An alarm is raised for a sensor when either statistic exceeds the
    decision interval *h*.  The sensor remains in alarm until :meth:`reset` is
    called.

    Args:
        k: Allowance parameter (half the shift size to detect, in standardised
           units).  Defaults to :data:`~src.config.CUSUM_K_SIGMA`.
        h: Decision interval — alarm threshold.  Defaults to
           :data:`~src.config.CUSUM_H_SIGMA`.

    Example::

        cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        for step in sequence:
            residual = (step["temperature"] - baseline_mean) / baseline_std
            if cusum.update("temperature", residual):
                print("Drift alarm on temperature!")

    Note:
        This is a stub.  The ml-engineer will replace :meth:`update` with the
        real CUSUM recurrence; the public interface is frozen.
    """

    # Default parameters mirrored from config so callers can construct without
    # importing config directly.
    _DEFAULT_K: ClassVar[float] = CUSUM_K_SIGMA
    _DEFAULT_H: ClassVar[float] = CUSUM_H_SIGMA

    def __init__(
        self,
        k: float = CUSUM_K_SIGMA,
        h: float = CUSUM_H_SIGMA,
    ) -> None:
        self.k = k
        self.h = h
        # Per-sensor CUSUM statistics: {sensor: (s_pos, s_neg)}.
        self._stats: Dict[str, tuple[float, float]] = {}
        # Sensors currently in alarm state.
        self._alarms: list[str] = []

    def update(self, sensor: str, residual: float) -> bool:
        """Update the CUSUM statistic for *sensor* and return True on alarm.

        Args:
            sensor: Sensor name (e.g. ``"temperature"``).
            residual: Standardised residual ``(value - baseline_mean) /
                baseline_std`` for the current timestep.

        Returns:
            ``True`` if *sensor* entered alarm state (or was already in alarm)
            after this update; ``False`` otherwise.

        Note:
            Stub implementation always returns ``False``.
            TODO(ml-engineer): implement CUSUM recurrence — 2026-06-22::

                s_pos = max(0, s_pos + residual - self.k)
                s_neg = max(0, s_neg - residual - self.k)
                alarm = s_pos >= self.h or s_neg >= self.h
        """
        # TODO(ml-engineer): replace with real CUSUM recurrence — 2026-06-22
        s_pos, s_neg = self._stats.get(sensor, (0.0, 0.0))
        _ = residual  # consumed once real logic is in place
        self._stats[sensor] = (s_pos, s_neg)
        return False

    def alarmed_sensors(self) -> list[str]:
        """Return the list of sensors currently in alarm state."""
        return list(self._alarms)

    def reset(self, sensor: str) -> None:
        """Reset CUSUM statistics and alarm flag for *sensor*.

        Call after an alarm has been acknowledged or acted upon to restart
        accumulation from zero.
        """
        self._stats[sensor] = (0.0, 0.0)
        self._alarms = [s for s in self._alarms if s != sensor]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize internal state for DB persistence."""
        return {
            "k": self.k,
            "h": self.h,
            "stats": {
                sensor: list(pair) for sensor, pair in self._stats.items()
            },
            "alarms": list(self._alarms),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CUSUMDriftDetector":
        """Restore state from a previously serialized dict."""
        inst = cls(
            k=float(data.get("k", CUSUM_K_SIGMA)),
            h=float(data.get("h", CUSUM_H_SIGMA)),
        )
        raw_stats = data.get("stats", {})
        inst._stats = {
            sensor: (float(pair[0]), float(pair[1]))
            for sensor, pair in raw_stats.items()
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        }
        inst._alarms = [str(s) for s in data.get("alarms", [])]
        return inst
