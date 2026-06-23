"""Assess how clear or concentrated an anomaly attribution distribution is."""

from typing import Dict

import numpy as np

from src.config import NUMERICAL_EPSILON


class AttributionStrength:
    """Assess how clear/concentrated an attribution distribution is."""

    @staticmethod
    def gini_impurity(attribution: Dict[str, float]) -> float:
        """
        Gini impurity of the attribution distribution.

        Low Gini = one sensor dominates (clear attribution).
        High Gini = contributions spread out (unclear attribution).

        Args:
            attribution: Sensor name -> contribution mapping.

        Returns:
            Gini impurity in [0, 1); 0.0 for an all-zero distribution.
        """
        values = np.array(list(attribution.values()), dtype=np.float64)
        if values.sum() <= 0:
            return 0.0
        probabilities = values / values.sum()
        return float(1.0 - np.sum(probabilities**2))

    @staticmethod
    def entropy(attribution: Dict[str, float]) -> float:
        """
        Shannon entropy (bits) of the attribution distribution.

        Args:
            attribution: Sensor name -> contribution mapping.

        Returns:
            Entropy >= 0.0; 0.0 for an all-zero distribution.
        """
        values = np.array(list(attribution.values()), dtype=np.float64)
        if values.sum() <= 0:
            return 0.0
        probabilities = values / values.sum()
        probabilities = probabilities[probabilities > NUMERICAL_EPSILON]
        return float(-np.sum(probabilities * np.log2(probabilities)))
