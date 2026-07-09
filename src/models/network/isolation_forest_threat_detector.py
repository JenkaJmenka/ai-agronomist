"""Multi-dimensional Isolation Forest threat scoring for OT networks.

Processes a 19-feature network behaviour vector (RTT, jitter, IAT CV,
RSSI, BSSID counts, DNS latency, etc.) through an ensemble of
isolation trees.  The anomaly score is converted to a 0-100 threat
score; values above the threshold trigger an alert.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from sklearn.ensemble import IsolationForest as SklearnIF

from src.config import (
    IF_THREAT_CONTAMINATION,
    IF_THREAT_N_ESTIMATORS,
    THREAT_SCORE_THRESHOLD,
)


class IsolationForestThreatDetector:
    """Multi-feature Isolation Forest for OT network threat detection."""

    def __init__(
        self,
        n_estimators: int = IF_THREAT_N_ESTIMATORS,
        contamination: float = IF_THREAT_CONTAMINATION,
        threshold: float = THREAT_SCORE_THRESHOLD,
        random_state: int = 42,
    ) -> None:
        self._threshold = threshold
        self._model: Optional[SklearnIF] = None
        self._n_estimators = n_estimators
        self._contamination = contamination
        self._random_state = random_state

    def fit(self, baseline_features: np.ndarray) -> None:
        """Fit the Isolation Forest on normal-traffic feature vectors.

        Args:
            baseline_features: Array of shape ``(n_samples, n_features)``.
        """
        self._model = SklearnIF(
            n_estimators=self._n_estimators,
            contamination=self._contamination,
            random_state=self._random_state,
        )
        self._model.fit(baseline_features)

    def detect(
        self,
        feature_vector: np.ndarray,
    ) -> Tuple[float, bool]:
        """Score a single feature vector.

        Args:
            feature_vector: 1-D array of shape ``(n_features,)`` or
                2-D of shape ``(1, n_features)``.

        Returns:
            ``(threat_score, is_anomaly)`` where threat_score is in
            [0, 100] and is_anomaly is ``True`` when it exceeds the
            configured threshold.

        Raises:
            RuntimeError: If the model has not been fitted.
        """
        if self._model is None:
            raise RuntimeError("Model not fitted — call fit() first")

        vec = np.asarray(feature_vector, dtype=float)
        if vec.ndim == 1:
            vec = vec.reshape(1, -1)

        raw_score = -self._model.score_samples(vec)[0]
        threat_score = float(np.clip(raw_score * 100, 0, 100))

        return threat_score, threat_score > self._threshold
