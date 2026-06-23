"""
Anomaly attribution: explains which sensors caused an LSTM-VAE anomaly.

Core idea (permutation-based SHAP approximation):
- The LSTM-VAE reconstruction error is the "prediction".
- Each sensor channel is masked (replaced by its baseline mean) in turn.
- A sensor's contribution is how much masking it reduces the error.
- Contributions are normalized to [0, 1] and sum to 1.0.

Determinism note: ``LSTMVAE.forward`` samples ``z = mu + eps * std`` even in
eval mode, so two passes over the same input give slightly different errors
(~1e-3) --- enough to swamp small per-sensor contributions. Attribution
therefore uses the deterministic path ``encode -> decode(mu)`` instead of
``forward`` so that results are reproducible.
"""

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn.functional as F

from src.config import (
    ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD,
    ATTRIBUTION_CONFIDENCE_SCALE,
    DEVICE,
    EXPLANATION_DEVIATION_THRESHOLD,
    NUMERICAL_EPSILON,
    SENSOR_NAMES,
)
from src.models.attribution_strength import AttributionStrength
from src.models.lstm_vae import LSTMVAE

logger = logging.getLogger(__name__)

# Re-export so existing imports continue to work.
__all__ = ["AnomalyAttributor", "AttributionStrength"]


class AnomalyAttributor:
    """
    SHAP-style (permutation-based) attribution for LSTM-VAE anomalies.

    Usage:
        attributor = AnomalyAttributor(model)
        attributor.set_baseline(normal_sequences)   # (N, seq_len, input_dim)
        result = attributor.attribute_sequence(anomalous_sequence)
    """

    def __init__(
        self,
        model: LSTMVAE,
        device: str = DEVICE,
        sensor_names: Optional[List[str]] = None,
    ) -> None:
        """
        Args:
            model: Trained LSTMVAE instance.
            device: Torch device string, e.g. "cpu" or "cuda".
            sensor_names: Per-channel sensor names; defaults to
                ``config.SENSOR_NAMES``. Length must equal the model input dim.

        Side effects:
            ``model`` is mutated in place: it is moved to ``device`` and
            switched to eval mode (attribution is inference-only and dropout
            must stay disabled). Callers sharing the model with a training
            pipeline must expect this; eval mode is also re-asserted before
            every reconstruction pass (see :meth:`_reconstruction_error`).
        """
        self.model = model
        self.device = torch.device(device)
        self.model.to(self.device)
        # Attribution is inference-only: disable dropout permanently.
        self.model.eval()
        self.sensor_names: List[str] = (
            list(sensor_names) if sensor_names is not None else list(SENSOR_NAMES)
        )
        self.baseline_sequences: Optional[np.ndarray] = None
        self.baseline_error: Optional[float] = None

    def set_baseline(self, sequences: np.ndarray) -> None:
        """
        Establish the normal-operation baseline used for masking and deviations.

        Args:
            sequences: Array of shape (N, seq_len, input_dim) of normal
                (non-anomalous) sequences.

        Raises:
            ValueError: If the array is empty, contains non-finite values
                (NaN/Inf), or the sensor axis does not match ``sensor_names``.
        """
        baseline = np.asarray(sequences, dtype=np.float32)
        if baseline.ndim != 3 or baseline.shape[0] == 0:
            raise ValueError(
                f"Baseline must be a non-empty (N, seq_len, input_dim) array, "
                f"got shape {baseline.shape}"
            )
        if baseline.shape[2] != len(self.sensor_names):
            raise ValueError(
                f"Baseline has {baseline.shape[2]} sensors, expected "
                f"{len(self.sensor_names)} ({self.sensor_names})"
            )
        if not np.isfinite(baseline).all():
            raise ValueError(
                "Baseline contains non-finite values (NaN/Inf); "
                "clean the sensor data before setting the baseline"
            )

        baseline_error = self._reconstruction_error(
            torch.from_numpy(baseline).to(self.device)
        )
        self.baseline_sequences = baseline
        self.baseline_error = baseline_error
        logger.info(
            "Attribution baseline set: %d sequences, baseline error %.6f",
            baseline.shape[0],
            self.baseline_error,
        )

    def attribute_sequence(self, sequence: np.ndarray) -> Dict[str, Any]:
        """
        Compute per-sensor attribution for one anomalous sequence.

        Each sensor channel is replaced by its baseline mean; the drop in
        reconstruction error is that sensor's (clamped, then normalized)
        contribution.

        Args:
            sequence: Array of shape (seq_len, input_dim).

        Returns:
            Dict with keys: ``attribution`` (name -> normalized contribution,
            sums to 1.0), ``sensor_contributions`` (raw error deltas),
            ``affected_sensors``, ``confidence``, ``anomaly_error``,
            ``baseline_error``, ``is_fallback``.

            ``is_fallback`` is True only when no sensor stood out (all raw
            contributions ~0) and the uniform 1/K fallback fired; confidence
            is then 0.0. In the non-fallback case the max contribution is at
            least 1/K for K sensors, so ``confidence = min(max_contribution *
            ATTRIBUTION_CONFIDENCE_SCALE, 1.0)`` has a hard floor of
            ``ATTRIBUTION_CONFIDENCE_SCALE / K`` (0.5 with the default 4
            sensors and scale 2.0) --- it can never express "low" there.

        Raises:
            ValueError: If no baseline is set, the shape is invalid, the
                sequence contains non-finite values, or the model produces
                a non-finite reconstruction error.
        """
        sequence = self._validate_sequence(sequence)
        assert self.baseline_sequences is not None  # narrowed by validation

        anomaly_error = self._reconstruction_error(self._to_batch_tensor(sequence))

        raw_contributions: List[float] = []
        for sensor_idx in range(sequence.shape[1]):
            masked_sequence = sequence.copy()
            masked_sequence[:, sensor_idx] = self.baseline_sequences[
                :, :, sensor_idx
            ].mean()
            masked_error = self._reconstruction_error(
                self._to_batch_tensor(masked_sequence)
            )
            # Contribution = how much masking this sensor reduces the error.
            raw_contributions.append(max(0.0, anomaly_error - masked_error))

        attribution, is_fallback = self._normalize_contributions(raw_contributions)
        affected_sensors = [
            name
            for name, contribution in attribution.items()
            if contribution > ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD
        ]
        if is_fallback:
            # Nothing attributable: don't dress the uniform split up as 0.5.
            confidence = 0.0
        else:
            # A single dominant sensor -> high confidence; evenly spread -> low.
            max_contribution = max(attribution.values())
            confidence = min(max_contribution * ATTRIBUTION_CONFIDENCE_SCALE, 1.0)

        return {
            "attribution": attribution,
            "sensor_contributions": raw_contributions,
            "affected_sensors": affected_sensors,
            "confidence": confidence,
            "anomaly_error": anomaly_error,
            "baseline_error": self.baseline_error,
            "is_fallback": is_fallback,
        }

    def attribute_batch(self, sequences: np.ndarray) -> List[Dict[str, Any]]:
        """
        Attribute a batch of sequences.

        Args:
            sequences: Array of shape (batch, seq_len, input_dim).

        Returns:
            One attribution dict (see :meth:`attribute_sequence`) per sequence.
        """
        return [self.attribute_sequence(sequence) for sequence in sequences]

    def get_explanation(
        self, attribution_result: Dict[str, Any], sequence: np.ndarray
    ) -> str:
        """
        Build a natural-language explanation from an attribution result.

        Args:
            attribution_result: Output of :meth:`attribute_sequence`.
            sequence: The same (seq_len, input_dim) sequence that was attributed.

        Returns:
            Human-readable explanation, e.g.
            ``"temperature deviation: +0.31 (45% contribution)"``.
        """
        affected_sensors = attribution_result.get("affected_sensors", [])
        if not affected_sensors or self.baseline_sequences is None:
            return "No significant sensor deviation detected"

        # Mean per-sensor deviation from the baseline mean, shape (input_dim,).
        baseline_sensor_means = self.baseline_sequences.mean(axis=(0, 1))
        deviations = np.asarray(sequence, dtype=np.float32).mean(axis=0) - (
            baseline_sensor_means
        )

        explanation_parts: List[str] = []
        for sensor_name in affected_sensors:
            sensor_idx = self.sensor_names.index(sensor_name)
            deviation = float(deviations[sensor_idx])
            contribution = attribution_result["attribution"].get(sensor_name, 0.0)
            if abs(deviation) > EXPLANATION_DEVIATION_THRESHOLD:
                explanation_parts.append(
                    f"{sensor_name} deviation: {deviation:+.2f} "
                    f"({contribution:.0%} contribution)"
                )

        return " + ".join(explanation_parts) if explanation_parts else "Unknown cause"

    def _reconstruction_error(self, x: torch.Tensor) -> float:
        """
        Deterministic MSE reconstruction error via encode -> decode(mu).

        Re-asserts eval mode first: pipelines sharing the model (e.g. the
        Gap 3 online learner) call ``model.train()``, which would silently
        re-enable dropout and break the determinism guarantee.

        Raises:
            ValueError: If the computed error is non-finite (NaN/Inf) ---
                e.g. corrupted/diverged model weights or pathological input.
        """
        if self.model.training:
            self.model.eval()
        with torch.no_grad():
            mu, _ = self.model.encode(x)
            reconstruction = self.model.decode(mu)
            error = float(F.mse_loss(reconstruction, x).item())
        if not math.isfinite(error):
            raise ValueError(
                f"Non-finite reconstruction error ({error}): model weights "
                f"or input data are corrupted; attribution aborted"
            )
        return error

    def _to_batch_tensor(self, sequence: np.ndarray) -> torch.Tensor:
        """Convert a (seq_len, input_dim) array to a (1, seq_len, input_dim) tensor."""
        return torch.from_numpy(sequence[np.newaxis, :]).to(self.device)

    def _normalize_contributions(
        self, raw_contributions: List[float]
    ) -> Tuple[Dict[str, float], bool]:
        """
        Normalize raw contributions so they sum to 1.0.

        Falls back to a uniform distribution when no sensor stands out
        (all contributions ~0), preserving the sums-to-one contract.

        Returns:
            ``(attribution, is_fallback)`` where ``is_fallback`` is True
            only when the uniform fallback fired.
        """
        total = sum(raw_contributions)
        if total > NUMERICAL_EPSILON:
            normalized = [contribution / total for contribution in raw_contributions]
            return dict(zip(self.sensor_names, normalized)), False
        uniform = [1.0 / len(raw_contributions)] * len(raw_contributions)
        return dict(zip(self.sensor_names, uniform)), True

    def _validate_sequence(self, sequence: np.ndarray) -> np.ndarray:
        """Validate and coerce one (seq_len, input_dim) sequence to float32.

        Raises:
            ValueError: If no baseline is set, the shape is invalid, or the
                sequence contains non-finite values (NaN/Inf).
        """
        if self.baseline_sequences is None:
            raise ValueError("Must call set_baseline() first")
        sequence = np.asarray(sequence, dtype=np.float32)
        if sequence.ndim != 2 or sequence.shape[1] != len(self.sensor_names):
            raise ValueError(
                f"Sequence must have shape (seq_len, {len(self.sensor_names)}), "
                f"got {sequence.shape}"
            )
        if not np.isfinite(sequence).all():
            raise ValueError(
                "Sequence contains non-finite values (NaN/Inf); "
                "clean the sensor data before attribution"
            )
        return sequence
