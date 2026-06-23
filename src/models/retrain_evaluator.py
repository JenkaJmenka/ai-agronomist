"""
Evaluate proposed configs against labeled operator feedback holdout.
"""

from __future__ import annotations

from typing import Any, Dict, List


class RetrainEvaluator:
    """Replay feedback labels through threshold + ensemble logic."""

    @staticmethod
    def _predict(item: Dict[str, Any], config: Dict[str, Any]) -> bool:
        threshold = float(config.get("ANOMALY_THRESHOLD", 0.5))
        scores = item.get("detector_scores")
        if isinstance(scores, dict) and scores:
            weights = config.get("ENSEMBLE_DETECTOR_WEIGHTS", {})
            weighted = 0.0
            weight_sum = 0.0
            for name, score in scores.items():
                w = float(weights.get(name, 0.25))
                weighted += w * float(score)
                weight_sum += w
            ensemble = weighted / weight_sum if weight_sum else 0.0
            ens_threshold = float(config.get("ENSEMBLE_ANOMALY_THRESHOLD", 0.5))
            min_agreement = int(config.get("ENSEMBLE_MIN_AGREEMENT", 2))
            votes = sum(1 for s in scores.values() if float(s) >= 0.5)
            return ensemble >= ens_threshold and votes >= min_agreement

        anomaly_score = item.get("anomaly_score")
        if anomaly_score is not None:
            return float(anomaly_score) > threshold

        return bool(item.get("is_anomaly", item.get("is_true_anomaly", False)))

    @classmethod
    def evaluate(
        cls,
        config: Dict[str, Any],
        feedback_holdout: List[Dict[str, Any]],
    ) -> Dict[str, float]:
        """Return fp_rate, tp_rate, mean_confidence on holdout."""
        if not feedback_holdout:
            return {"fp_rate": 0.0, "tp_rate": 0.0, "mean_confidence": 0.0}

        tp = fp = tn = fn = 0
        confidences: List[float] = []

        for item in feedback_holdout:
            label = bool(item.get("is_anomaly", item.get("is_true_anomaly", True)))
            pred = cls._predict(item, config)
            if label and pred:
                tp += 1
            elif not label and pred:
                fp += 1
            elif label and not pred:
                fn += 1
            else:
                tn += 1
            confidences.append(float(item.get("operator_confidence", 0.8)))

        positives = tp + fn
        negatives = fp + tn
        tp_rate = tp / positives if positives else 0.0
        fp_rate = fp / negatives if negatives else 0.0
        mean_confidence = sum(confidences) / len(confidences)

        return {
            "fp_rate": round(fp_rate, 4),
            "tp_rate": round(tp_rate, 4),
            "mean_confidence": round(mean_confidence, 4),
        }

    @staticmethod
    def improvement_pct(
        before: Dict[str, float], after: Dict[str, float]
    ) -> float:
        """Fractional FP-rate reduction (primary promote metric)."""
        fp_before = before.get("fp_rate", 0.0)
        fp_after = after.get("fp_rate", 0.0)
        if fp_before <= 0:
            tp_gain = after.get("tp_rate", 0.0) - before.get("tp_rate", 0.0)
            return max(0.0, tp_gain)
        return max(0.0, (fp_before - fp_after) / fp_before)
