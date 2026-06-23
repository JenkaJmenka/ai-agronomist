"""
Anomaly Embedding Module: extract vector representations from the LSTM-VAE
latent space.

Core concept:
- The VAE encoder maps a sensor sequence to a latent Gaussian (mu, logvar).
- mu is an 8-dimensional point in latent space (see ``LATENT_DIM`` in config).
- This mu vector IS the embedding of the anomaly.
- Embeddings enable semantic similarity search, clustering of failure types,
  and few-shot learning over past incidents.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Tuple

import numpy as np
import torch
from scipy.spatial.distance import cosine as cosine_distance
from scipy.spatial.distance import euclidean as euclidean_distance
from sklearn.cluster import KMeans

from src.config import (
    DEFAULT_EMBEDDING_CLUSTERS,
    DEVICE,
    EMBEDDING_CONFIDENCE_THRESHOLD,
    RANDOM_SEED,
)

logger = logging.getLogger(__name__)


class AnomalyEmbedding:
    """
    Extract and analyze anomaly embeddings from the LSTM-VAE latent space.

    Embeddings capture the "essence" of an anomaly in ``LATENT_DIM`` dims:
    - Encode complex temporal patterns learned by the encoder.
    - Enable semantic similarity ("this anomaly is like the May 15 failure").
    - Foundation for few-shot learning and incident memory.
    """

    def __init__(self, model: torch.nn.Module, device: str = DEVICE) -> None:
        """
        Args:
            model: Trained ``LSTMVAE`` instance (must expose ``encode``,
                ``decode`` and a ``latent_dim`` attribute).
            device: Torch device string, e.g. ``"cpu"`` or ``"cuda"``.
        """
        self.device = torch.device(device)
        self.model = model.to(self.device)
        self.model.eval()  # No gradients / dropout during inference
        self.embedding_dim: int = model.latent_dim

    # =========================================================================
    # CORE METHODS: extract embeddings
    # =========================================================================

    def embed_sequence(self, sequence: np.ndarray) -> np.ndarray:
        """
        Extract the embedding for a single sensor sequence.

        Args:
            sequence: Array of shape ``(seq_len, input_dim)`` or
                ``(1, seq_len, input_dim)``.

        Returns:
            Embedding of shape ``(latent_dim,)`` — the encoder mu vector.

        Raises:
            ValueError: If the input is not 2-D or singleton 3-D.
        """
        batched = self._as_single_batch(sequence)
        mu, _ = self._encode_tensor(batched)
        return mu[0].cpu().numpy()

    def embed_batch(self, sequences: np.ndarray) -> np.ndarray:
        """
        Extract embeddings for a batch of sequences.

        Args:
            sequences: Array of shape ``(batch_size, seq_len, input_dim)``.

        Returns:
            Embeddings of shape ``(batch_size, latent_dim)``.

        Raises:
            ValueError: If the input is not 3-D.
        """
        if sequences.ndim != 3:
            raise ValueError(
                f"Expected 3-D batch (batch, seq_len, input_dim), "
                f"got shape {sequences.shape}"
            )
        mu, _ = self._encode_tensor(sequences)
        return mu.cpu().numpy()

    def embed_with_uncertainty(self, sequence: np.ndarray) -> Dict[str, object]:
        """
        Extract an embedding together with an uncertainty estimate.

        Uncertainty is the mean standard deviation of the latent distribution;
        a high value means the model is unsure about this anomaly type.

        Args:
            sequence: Array of shape ``(seq_len, input_dim)`` or
                ``(1, seq_len, input_dim)``.

        Returns:
            Dict with keys:
                - ``embedding``: ``(latent_dim,)`` mu vector.
                - ``uncertainty``: float, mean std across latent dims.
                - ``mu``: ``(latent_dim,)`` mean of the latent distribution.
                - ``logvar``: ``(latent_dim,)`` log variance.
                - ``std``: ``(latent_dim,)`` per-dimension std.
        """
        batched = self._as_single_batch(sequence)
        mu, logvar = self._encode_tensor(batched)

        mu_np = mu[0].cpu().numpy()
        logvar_np = logvar[0].cpu().numpy()
        std = np.exp(0.5 * logvar_np)  # exp(0.5 * logvar) = std

        return {
            "embedding": mu_np,
            "uncertainty": float(std.mean()),
            "mu": mu_np,
            "logvar": logvar_np,
            "std": std,
        }

    # =========================================================================
    # ANALYSIS: interpret embeddings
    # =========================================================================

    def embedding_distance(
        self,
        embedding_1: np.ndarray,
        embedding_2: np.ndarray,
        metric: str = "cosine",
    ) -> float:
        """
        Compute the distance between two embeddings.

        Args:
            embedding_1: Array of shape ``(latent_dim,)``.
            embedding_2: Array of shape ``(latent_dim,)``.
            metric: ``"cosine"`` (default) or ``"euclidean"``.

        Returns:
            Distance: ``[0, 2]`` for cosine, ``[0, inf)`` for euclidean.
            Smaller means more similar.

        Raises:
            ValueError: If ``metric`` is unknown.
        """
        if metric == "cosine":
            return float(cosine_distance(embedding_1, embedding_2))
        if metric == "euclidean":
            return float(euclidean_distance(embedding_1, embedding_2))
        raise ValueError(f"Unknown metric: {metric!r} (use 'cosine' or 'euclidean')")

    def embedding_similarity(
        self, embedding_1: np.ndarray, embedding_2: np.ndarray
    ) -> float:
        """
        Compute cosine similarity between two embeddings.

        Args:
            embedding_1: Array of shape ``(latent_dim,)``.
            embedding_2: Array of shape ``(latent_dim,)``.

        Returns:
            Similarity in ``[-1, 1]`` where 1 means identical direction.
        """
        distance = self.embedding_distance(embedding_1, embedding_2, metric="cosine")
        return 1.0 - distance

    def cluster_embeddings(
        self,
        embeddings: np.ndarray,
        n_clusters: int = DEFAULT_EMBEDDING_CLUSTERS,
    ) -> Dict[str, object]:
        """
        Cluster embeddings with KMeans to discover anomaly types.

        Args:
            embeddings: Array of shape ``(n_anomalies, latent_dim)``.
            n_clusters: Number of anomaly types to find.

        Returns:
            Dict with keys:
                - ``cluster_centers``: ``(n_clusters, latent_dim)``.
                - ``labels``: ``(n_anomalies,)`` cluster assignments.
                - ``inertia``: within-cluster sum of squared distances.
                - ``n_clusters``: number of clusters used.

        Raises:
            ValueError: If there are fewer embeddings than clusters.
        """
        if embeddings.shape[0] < n_clusters:
            raise ValueError(
                f"Need at least {n_clusters} embeddings to form {n_clusters} "
                f"clusters, got {embeddings.shape[0]}"
            )

        kmeans = KMeans(n_clusters=n_clusters, random_state=RANDOM_SEED, n_init="auto")
        labels = kmeans.fit_predict(embeddings)

        return {
            "cluster_centers": kmeans.cluster_centers_,
            "labels": labels,
            "inertia": float(kmeans.inertia_),
            "n_clusters": n_clusters,
        }

    # =========================================================================
    # RECONSTRUCTION: from embedding back to sequence
    # =========================================================================

    def decode_embedding(self, embedding: np.ndarray) -> np.ndarray:
        """
        Reconstruct a sequence from an embedding (inverse of embed_sequence).

        The result is the "prototype" sequence for this point in latent
        space — useful for visualizing what a cluster center looks like.

        Args:
            embedding: Array of shape ``(latent_dim,)`` or ``(1, latent_dim)``.

        Returns:
            Reconstructed sequence of shape ``(seq_len, input_dim)``.

        Raises:
            ValueError: If the embedding dimensionality is wrong.
        """
        if embedding.ndim == 1:
            embedding = embedding[np.newaxis, :]  # (latent_dim,) -> (1, latent_dim)
        if embedding.ndim != 2 or embedding.shape[-1] != self.embedding_dim:
            raise ValueError(
                f"Expected embedding of dim {self.embedding_dim}, "
                f"got shape {embedding.shape}"
            )

        # LSTMVAE.decode expects (batch, latent_dim) and expands the sequence
        # dimension internally — no manual seq-axis insertion needed.
        z = torch.as_tensor(embedding, dtype=torch.float32, device=self.device)
        with torch.no_grad():
            reconstruction = self.model.decode(z)  # (batch, seq_len, input_dim)

        return reconstruction[0].cpu().numpy()

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _as_single_batch(self, sequence: np.ndarray) -> np.ndarray:
        """Validate a single sequence and add a batch axis if missing."""
        if sequence.ndim == 2:
            return sequence[np.newaxis, :]
        if sequence.ndim == 3 and sequence.shape[0] == 1:
            return sequence
        raise ValueError(
            f"Expected (seq_len, input_dim) or (1, seq_len, input_dim), "
            f"got shape {sequence.shape}"
        )

    def _encode_tensor(
        self, sequences: np.ndarray
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Run the encoder on a numpy batch; returns (mu, logvar) tensors."""
        x = torch.as_tensor(sequences, dtype=torch.float32, device=self.device)
        with torch.no_grad():
            mu, logvar = self.model.encode(x)
        return mu, logvar


@dataclass
class EnrichedAnomalyResult:
    """Anomaly detection result enriched with embedding metadata."""

    sequence: np.ndarray
    anomaly_score: float
    embedding: np.ndarray
    uncertainty: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, object]:
        """Convert to a JSON-serializable dict for API responses."""
        return {
            "anomaly_score": float(self.anomaly_score),
            "embedding": self.embedding.tolist(),
            "uncertainty": float(self.uncertainty),
            "timestamp": self.timestamp.isoformat(),
            "is_confident": self.uncertainty < EMBEDDING_CONFIDENCE_THRESHOLD,
            "embedding_dim": int(len(self.embedding)),
        }
