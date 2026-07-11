"""Service response duration anomaly detector (SCADA spec section 14).

Implements the spec's "Dynamic Quantile Filtering": the alarm threshold is
the P99 (configurable percentile) of a rolling window of the most recent
``window`` response times.  The calibrated static baseline (120 ms) acts as
a floor and as the fallback threshold until at least ``window`` samples have
been observed — so a freshly constructed detector with no history behaves
exactly like the original static-threshold implementation.

History is populated explicitly via ``update_baseline`` / ``fit``; ``detect``
never mutates the rolling window, so existing callers that only invoke
``detect`` retain their original static-threshold behaviour.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, List, Optional

import numpy as np

from src.config import (
    SERVICE_RESPONSE_BASELINE_MS,
    SERVICE_RESPONSE_PERCENTILE,
    SERVICE_RESPONSE_WINDOW,
)


class ServiceResponseDetector:
    """Detect anomalous service response durations via dynamic P99 filtering."""

    def __init__(
        self,
        baseline_threshold_ms: float = SERVICE_RESPONSE_BASELINE_MS,
        window: int = SERVICE_RESPONSE_WINDOW,
        percentile: float = SERVICE_RESPONSE_PERCENTILE,
    ) -> None:
        # spec ref: N=100 (docs/anomaly_framework_spec.md §14); production uses
        # window=1000 for a smoother rolling P99. percentile spec ref: P99.
        self._baseline = baseline_threshold_ms
        self._window = window
        self._percentile = percentile
        self._samples: deque[float] = deque(maxlen=window)

    @property
    def threshold_ms(self) -> float:
        """Current effective threshold (dynamic P99 once the window fills)."""
        return self._current_threshold()

    def update_baseline(self, duration_ms: float) -> None:
        """Add one observed response time to the rolling baseline window."""
        self._samples.append(float(duration_ms))

    def fit(self, durations: Iterable[float]) -> None:
        """Seed the rolling baseline window from an iterable of durations."""
        for duration in durations:
            self._samples.append(float(duration))

    def _current_threshold(self) -> float:
        """Dynamic threshold: max(static baseline, rolling P99).

        Falls back to the static baseline until the window is full, and
        keeps the baseline as a floor so the threshold never drops below
        the calibrated 120 ms even for a very fast service.
        """
        if len(self._samples) < self._window:
            return self._baseline
        rolling_p99 = float(np.percentile(np.asarray(self._samples), self._percentile))
        return max(self._baseline, rolling_p99)

    def detect(
        self,
        duration_ms: float,
        baseline_threshold_ms: Optional[float] = None,
    ) -> bool:
        """Check a single response duration.

        Args:
            duration_ms: Measured response time in milliseconds.
            baseline_threshold_ms: Explicit threshold override for this
                check; bypasses the dynamic P99 window entirely.

        Returns:
            ``True`` if duration exceeds the threshold.
        """
        threshold = (
            baseline_threshold_ms
            if baseline_threshold_ms is not None
            else self._current_threshold()
        )
        return duration_ms > threshold

    def detect_series(
        self,
        durations: np.ndarray,
        baseline_threshold_ms: Optional[float] = None,
    ) -> List[bool]:
        """Check every element of a 1-D duration series.

        Args:
            durations: 1-D array of response times in ms.
            baseline_threshold_ms: Explicit threshold override; bypasses
                the dynamic P99 window.

        Returns:
            List of booleans.
        """
        threshold = (
            baseline_threshold_ms
            if baseline_threshold_ms is not None
            else self._current_threshold()
        )
        durations = np.asarray(durations, dtype=float)
        return (durations > threshold).tolist()
