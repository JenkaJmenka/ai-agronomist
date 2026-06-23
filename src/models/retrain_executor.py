"""
Phase 1 retrain executor: tune thresholds and ensemble weights from feedback.
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

from src.config import (
    ACTIVE_LEARNING_MAX_THRESHOLD_DELTA,
    ANOMALY_THRESHOLD,
    ENSEMBLE_DETECTOR_WEIGHTS,
)
from src.models.customer_config_store import default_customer_config


class RetrainExecutor:
    """Propose config updates from labeled operator feedback."""

    @staticmethod
    def apply_phase1a_thresholds(
        feedback: List[Dict[str, Any]],
        current_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Grid-search ANOMALY_THRESHOLD on labeled holdout."""
        proposed = dict(current_config)
        base = float(current_config.get("ANOMALY_THRESHOLD", ANOMALY_THRESHOLD))
        if not feedback:
            return proposed

        labels = [bool(f.get("is_anomaly", f.get("is_true_anomaly", True))) for f in feedback]
        scores = [
            float(f.get("anomaly_score", 0.5))
            for f in feedback
        ]
        if not any(scores):
            return proposed

        best_threshold = base
        best_error = float("inf")
        deltas = np.linspace(
            -ACTIVE_LEARNING_MAX_THRESHOLD_DELTA,
            ACTIVE_LEARNING_MAX_THRESHOLD_DELTA,
            num=7,
        )
        for delta in deltas:
            candidate = float(np.clip(base + delta, 0.05, 0.95))
            preds = [s > candidate for s in scores]
            error = sum(int(p != l) for p, l in zip(preds, labels))
            if error < best_error:
                best_error = error
                best_threshold = candidate

        proposed["ANOMALY_THRESHOLD"] = round(best_threshold, 4)
        return proposed

    @staticmethod
    def apply_phase1b_ensemble_weights(
        feedback: List[Dict[str, Any]],
        current_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Adjust detector weights using stored detector_scores vs labels."""
        proposed = dict(current_config)
        weights = dict(
            current_config.get("ENSEMBLE_DETECTOR_WEIGHTS", ENSEMBLE_DETECTOR_WEIGHTS)
        )

        labeled = [
            f for f in feedback if isinstance(f.get("detector_scores"), dict)
        ]
        if len(labeled) < 3:
            proposed["ENSEMBLE_DETECTOR_WEIGHTS"] = weights
            return proposed

        detector_names = list(weights.keys())
        accuracy: Dict[str, float] = {name: 0.0 for name in detector_names}
        counts: Dict[str, int] = {name: 0 for name in detector_names}

        for item in labeled:
            label = bool(item.get("is_anomaly", item.get("is_true_anomaly", True)))
            scores = item["detector_scores"]
            for name in detector_names:
                if name not in scores:
                    continue
                pred = float(scores[name]) >= 0.5
                if pred == label:
                    accuracy[name] += 1.0
                counts[name] += 1

        raw = []
        for name in detector_names:
            if counts[name] == 0:
                raw.append(weights.get(name, 0.25))
            else:
                raw.append(max(0.05, accuracy[name] / counts[name]))

        total = sum(raw) or 1.0
        proposed["ENSEMBLE_DETECTOR_WEIGHTS"] = {
            name: round(val / total, 4)
            for name, val in zip(detector_names, raw)
        }
        return proposed

    @classmethod
    def apply_phase1(
        cls,
        feedback: List[Dict[str, Any]],
        current_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run phase 1a then 1b."""
        after_a = cls.apply_phase1a_thresholds(feedback, current_config)
        return cls.apply_phase1b_ensemble_weights(feedback, after_a)

    @staticmethod
    def rollback(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Return snapshot config (identity helper for clarity)."""
        return dict(snapshot) if snapshot else default_customer_config()
