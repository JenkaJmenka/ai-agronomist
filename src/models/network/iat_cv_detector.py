"""Inter-Packet Arrival Time coefficient of variation detector.

Monitors the regularity of packet spacing on OT polling links.
Anomalous CV or absolute spacing indicates injection, polling failure,
or unauthenticated command insertion.
"""

from __future__ import annotations

from typing import List

import numpy as np

from src.config import (
    IAT_BASELINE_MEAN,
    IAT_CV_THRESHOLD,
    IAT_CV_WINDOW,
    IAT_SPACING_MULTIPLIER,
)


class IATCVDetector:
    """Detect anomalous inter-packet interval variation."""

    def __init__(
        self,
        window: int = IAT_CV_WINDOW,
        cv_threshold: float = IAT_CV_THRESHOLD,
        baseline_mean: float = IAT_BASELINE_MEAN,
        spacing_multiplier: float = IAT_SPACING_MULTIPLIER,
    ) -> None:
        if window < 2:
            raise ValueError(f"window must be >= 2, got {window}")
        self._window = window
        self._cv_threshold = cv_threshold
        self._spacing_limit = spacing_multiplier * baseline_mean

    def detect(self, iat_series: np.ndarray) -> List[bool]:
        """Per-timestep anomaly detection on an IAT series.

        Args:
            iat_series: 1-D array of inter-packet intervals (seconds).

        Returns:
            List of booleans; ``True`` = anomaly.  First ``w-1``
            entries are ``False``.
        """
        iat = np.asarray(iat_series, dtype=float)
        n = len(iat)
        w = self._window
        flags: List[bool] = [False] * n

        if n < w:
            return flags

        for t in range(w - 1, n):
            window = iat[t - w + 1 : t + 1]
            mu = float(np.mean(window))
            sigma = float(np.std(window, ddof=1))

            spacing_violation = iat[t] > self._spacing_limit
            cv_violation = False
            if mu > 0:
                cv = sigma / mu
                cv_violation = cv > self._cv_threshold

            flags[t] = bool(spacing_violation or cv_violation)

        return flags
