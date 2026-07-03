import torch
import numpy as np
from typing import Optional, List, Tuple, Union
from src.models.lstm_vae import LSTMVAE
from src.config import DEVICE, MAX_OBSERVE_BATCH
import glob

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
                             If None, loads the latest baseline.
        """
        if checkpoint_path is None:
            # Fallback to latest baseline
            checkpoints = sorted(glob.glob("models/checkpoints/epoch_*.pt"))
            self.checkpoint_path = checkpoints[-1] if checkpoints else "models/baseline.pt"
        else:
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
