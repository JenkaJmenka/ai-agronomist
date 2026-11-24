from dataclasses import dataclass

import torch
import numpy as np
from typing import Optional, List, Tuple, Union
from src.models.lstm_vae import LSTMVAE
from src.config import ANOMALY_SCALING_FACTOR, DEVICE, MAX_OBSERVE_BATCH


class MissingCheckpointError(RuntimeError):
    """Raised when an AnomalyDetector is constructed without a checkpoint path.

    There is no filesystem baseline to fall back to: models are resolved from
    the DB artifact store (``model_artifacts``), and ``.dockerignore`` excludes
    ``models/*.pt`` from the image, so no checkpoint ships in any environment.
    Callers must resolve a concrete path first — see
    ``src.api.services.detector_cache.resolve_tenant_detector``.

    Subclasses ``RuntimeError`` so callers that already handle the load-failure
    ``RuntimeError`` from ``_load_model`` keep working unchanged.
    """


class AnomalyDetector:
    """
    Anomaly detector using trained LSTM-VAE models.
    Instance reuse is handled by the route-level LRUDict in
    src/api/routes/inference.py (get_cached_detector).  There is no
    class-level cache here — one eviction policy governs all callers.
    """

    def __init__(self, checkpoint_path: Optional[str] = None):
        """
        Initializes the AnomalyDetector.
        Args:
            checkpoint_path: Path to the specific model weights to load.
                             Required — None raises MissingCheckpointError.
        Raises:
            MissingCheckpointError: if checkpoint_path is None.
        """
        if checkpoint_path is None:
            raise MissingCheckpointError(
                "AnomalyDetector requires an explicit checkpoint_path; models are "
                "resolved from the DB artifact store (model_artifacts). Resolve one "
                "via src.api.services.detector_cache.resolve_tenant_detector."
            )

        self.checkpoint_path = checkpoint_path
        self.model = self._load_model()

    def _load_model(self) -> LSTMVAE:
        """
        Loads a trained LSTM-VAE model from a checkpoint.
        Caching is the caller's responsibility (get_cached_detector at the route
        level).  Each direct instantiation loads from disk.
        """
        try:
            checkpoint = torch.load(self.checkpoint_path, map_location=DEVICE, weights_only=False)
            model_config = checkpoint.get("model_config", checkpoint.get("config", {}))
            model = LSTMVAE(**model_config)
            model.load_state_dict(checkpoint["model_state_dict"])
            model.eval()
            return model
        except Exception as e:
            raise RuntimeError(f"Failed to load model from {self.checkpoint_path}: {e}")

    @staticmethod
    def _validate_batch(sequences: np.ndarray) -> None:
        """Rejects malformed batches before they reach the model."""
        if not isinstance(sequences, np.ndarray) or sequences.ndim != 3:
            raise ValueError("Input must be a 3D NumPy array of shape (batch_size, seq_len, input_dim)")

        if np.isnan(sequences).any() or np.isinf(sequences).any():
            raise ValueError("Input contains NaN or Inf values")

    def predict(self, sequences: np.ndarray) -> np.ndarray:
        self._validate_batch(sequences)

        with torch.no_grad():
            sequences_tensor = torch.from_numpy(sequences).float()
            reconstructed_sequences, _, _, _ = self.model(sequences_tensor)
            # MSE across feature & seq dimensions
            reconstruction_error = torch.mean((sequences_tensor - reconstructed_sequences) ** 2, dim=[1, 2])
            return reconstruction_error.numpy()

    def predict_batch(self, sequences: List[np.ndarray]) -> np.ndarray:
        stacked = np.stack(sequences)
        self._validate_batch(stacked)

        all_scores = []
        with torch.no_grad():
            for i in range(0, len(stacked), MAX_OBSERVE_BATCH):
                batch = stacked[i:i + MAX_OBSERVE_BATCH]
                batch_tensor = torch.from_numpy(batch).float()
                reconstructed, _, _, _ = self.model(batch_tensor)
                scores = torch.mean((batch_tensor - reconstructed) ** 2, dim=[1, 2])
                all_scores.append(scores.numpy())

        return np.concatenate(all_scores) if all_scores else np.array([])

    def predict_batch_enriched(
        self, sequences: Union[List[np.ndarray], np.ndarray]
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Runs ONE enriched forward pass and returns scores, embeddings, and uncertainties.

        Reuses mu/logvar already produced by the encoder during the forward pass,
        so no additional encode pass is needed for the embedding outputs.

        Uncertainty convention: STANDARD DEVIATION (std = exp(0.5*logvar)),
        averaged across latent dims. This matches AnomalyEmbedding.embed_with_uncertainty
        and the pgvector incidents contract ('Std dev in latent space') —
        unified codebase-wide; do NOT switch back to variance (exp(logvar)).

        Args:
            sequences: List of (seq_len, input_dim) arrays, or an already-stacked
                       3D array of shape (batch_size, seq_len, input_dim).

        Returns:
            A tuple of:
                - scores: (batch_size,) per-sequence reconstruction MSE
                - embeddings: (batch_size, latent_dim) encoder means (mu)
                - uncertainties: (batch_size,) mean of exp(0.5*logvar) (std)
                  across latent dims
        """
        stacked = np.stack(sequences) if isinstance(sequences, list) else sequences
        self._validate_batch(stacked)

        all_scores = []
        all_mu = []
        all_uncertainties = []

        with torch.no_grad():
            for i in range(0, len(stacked), MAX_OBSERVE_BATCH):
                batch = stacked[i:i + MAX_OBSERVE_BATCH]
                batch_tensor = torch.from_numpy(batch).float()
                reconstructed, mu, logvar, _ = self.model(batch_tensor)

                scores = torch.mean((batch_tensor - reconstructed) ** 2, dim=[1, 2])
                uncertainties = torch.exp(0.5 * logvar).mean(dim=1)

                all_scores.append(scores.numpy())
                all_mu.append(mu.numpy())
                all_uncertainties.append(uncertainties.numpy())

        if not all_scores:
            return np.array([]), np.array([]), np.array([])

        return (
            np.concatenate(all_scores),
            np.concatenate(all_mu),
            np.concatenate(all_uncertainties)
        )


@dataclass(frozen=True)
class ModelScores:
    """Output of one LSTM-VAE forward pass over a batch of windows (ADR-014 A3).

    Arrays keep the model's float32 dtype so every path can derive its value
    exactly as it does today.

    Attributes:
        raw: ``(n,)`` per-window reconstruction MSE over sequence and channels.
        per_channel: ``(n, input_dim)`` per-channel MSE over the sequence.
        embeddings: ``(n, latent_dim)`` encoder means (mu).
        uncertainties: ``(n,)`` mean of ``exp(0.5 * logvar)`` over latent dims.
    """

    raw: np.ndarray
    per_channel: np.ndarray
    embeddings: np.ndarray
    uncertainties: np.ndarray

    def scaled_array(self) -> np.ndarray:
        """Vectorised scaled scores, as upload and ``/api/inference`` compute them.

        Returns:
            ``clip(raw / ANOMALY_SCALING_FACTOR, 0, 1)`` on the float32 array.
        """
        return np.clip(self.raw / ANOMALY_SCALING_FACTOR, 0.0, 1.0)

    def scaled_scalar(self, index: int) -> float:
        """One scaled score, as live and the comprehensive pipeline compute it.

        Args:
            index: Window index in the batch.

        Returns:
            ``clip(float(raw[index]) / ANOMALY_SCALING_FACTOR, 0, 1)`` in
            Python float arithmetic.
        """
        return float(np.clip(float(self.raw[index]) / ANOMALY_SCALING_FACTOR, 0.0, 1.0))


class ModelScorer:
    """The one LSTM-VAE forward pass for every scoring path (ADR-014 §2.1, A3).

    Replaces three copies of the same maths: ``AnomalyDetector.predict_batch_enriched``
    (upload, API), ``ComprehensiveInferencePipeline._run_lstm_vae`` and the live
    ``forward_pass_sync`` (``src/live/attribution.py``). Batches are chunked at
    ``MAX_OBSERVE_BATCH`` exactly like ``predict_batch_enriched``, so a single
    window is a batch of one, as on live; results are bit-identical to each
    path. Normalisation and the decision rule are not here: they are
    ``src.models.scoring.lstm``.
    """

    def __init__(self, model: LSTMVAE) -> None:
        """Wrap a loaded model.

        Args:
            model: An LSTM-VAE in eval mode (``AnomalyDetector.model``).
        """
        self.model = model

    @classmethod
    def from_detector(cls, detector: AnomalyDetector) -> "ModelScorer":
        """Build a scorer over an ``AnomalyDetector``'s loaded model.

        Args:
            detector: A detector whose checkpoint is already loaded.

        Returns:
            A scorer sharing the detector's model instance.
        """
        return cls(detector.model)

    def predict_with_channel_errors(
        self, windows: Union[List[np.ndarray], np.ndarray]
    ) -> ModelScores:
        """Run ONE forward pass and return every per-window model output.

        Args:
            windows: List of ``(seq_len, input_dim)`` model-input windows, or a
                stacked ``(n, seq_len, input_dim)`` array.

        Returns:
            ``ModelScores`` with float32 arrays. An empty batch returns empty
            arrays.

        Raises:
            ValueError: If the batch is not 3-D or holds NaN / Inf.
        """
        stacked = np.stack(windows) if isinstance(windows, list) else windows
        AnomalyDetector._validate_batch(stacked)
        raw: List[np.ndarray] = []
        per_channel: List[np.ndarray] = []
        mus: List[np.ndarray] = []
        stds: List[np.ndarray] = []
        with torch.no_grad():
            for start in range(0, len(stacked), MAX_OBSERVE_BATCH):
                batch = torch.from_numpy(stacked[start:start + MAX_OBSERVE_BATCH]).float()
                reconstructed, mu, logvar, _ = self.model(batch)
                error = (batch - reconstructed) ** 2
                raw.append(error.mean(dim=[1, 2]).numpy())
                per_channel.append(error.mean(dim=1).numpy())
                mus.append(mu.numpy())
                stds.append(torch.exp(0.5 * logvar).mean(dim=1).numpy())
        if not raw:
            empty = np.array([], dtype=np.float32)
            return ModelScores(empty, empty, empty, empty)
        return ModelScores(
            raw=np.concatenate(raw),
            per_channel=np.concatenate(per_channel),
            embeddings=np.concatenate(mus),
            uncertainties=np.concatenate(stds),
        )
