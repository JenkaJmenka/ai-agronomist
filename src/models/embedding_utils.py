"""
Utility functions for working with anomaly embeddings.

Pure numpy/sklearn helpers — no model or torch dependency — covering
normalization, statistics, nearest-neighbor search, and 2-D projection
for visualization.
"""

import logging
from typing import Dict, Tuple

import numpy as np
from sklearn.metrics.pairwise import cosine_distances, euclidean_distances

from src.config import (
    DEFAULT_NEAREST_NEIGHBORS,
    PCA_VARIANCE_TARGET,
    PROJECTION_COMPONENTS_2D,
    RANDOM_SEED,
)

logger = logging.getLogger(__name__)


def normalize_embeddings(embeddings: np.ndarray) -> np.ndarray:
    """
    Normalize embeddings to unit length (L2 normalization).

    Useful before cosine-distance computation.

    Args:
        embeddings: Array of shape ``(n, d)``.

    Returns:
        Array of the same shape where each row has unit norm
        (zero rows are left unchanged).
    """
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1.0  # Avoid division by zero for all-zero rows
    return embeddings / norms


def embedding_statistics(embeddings: np.ndarray) -> Dict[str, object]:
    """
    Compute summary statistics over a batch of embeddings.

    Args:
        embeddings: Array of shape ``(n, latent_dim)`` with ``n >= 2``.

    Returns:
        Dict with keys:
            - ``mean``: ``(latent_dim,)`` average embedding.
            - ``std``: ``(latent_dim,)`` per-dimension standard deviation.
            - ``covariance``: ``(latent_dim, latent_dim)`` covariance matrix.
            - ``pca_variance_explained``: ``(latent_dim,)`` normalized
              singular values of the covariance matrix.
            - ``effective_dims``: number of principal components needed to
              explain ``PCA_VARIANCE_TARGET`` of the variance.

    Raises:
        ValueError: If fewer than 2 embeddings are provided.
    """
    if embeddings.ndim != 2 or embeddings.shape[0] < 2:
        raise ValueError(
            f"Need a 2-D array with at least 2 embeddings, got shape "
            f"{embeddings.shape}"
        )

    covariance = np.cov(embeddings.T)
    singular_values = np.linalg.svd(covariance, compute_uv=False)
    variance_explained = singular_values / singular_values.sum()

    # Index of the first component where cumulative variance crosses the
    # target, +1 to convert from index to a count of dimensions.
    cumulative_variance = np.cumsum(variance_explained)
    effective_dims = int(np.searchsorted(cumulative_variance, PCA_VARIANCE_TARGET) + 1)

    return {
        "mean": embeddings.mean(axis=0),
        "std": embeddings.std(axis=0),
        "covariance": covariance,
        "pca_variance_explained": variance_explained,
        "effective_dims": effective_dims,
    }


def find_nearest_embeddings(
    query: np.ndarray,
    candidates: np.ndarray,
    k: int = DEFAULT_NEAREST_NEIGHBORS,
    metric: str = "cosine",
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Find the k nearest candidate embeddings to a query embedding.

    Args:
        query: Array of shape ``(latent_dim,)``.
        candidates: Array of shape ``(n_candidates, latent_dim)``.
        k: Number of neighbors to return (clamped to ``n_candidates``).
        metric: ``"cosine"`` or ``"euclidean"``.

    Returns:
        Tuple of:
            - ``indices``: ``(k,)`` indices into ``candidates``.
            - ``distances``: ``(k,)`` distances, ascending.

    Raises:
        ValueError: If ``metric`` is unknown or ``candidates`` is empty.
    """
    if candidates.size == 0:
        raise ValueError("Cannot search nearest embeddings in an empty candidate set")

    query_row = query.reshape(1, -1)
    if metric == "cosine":
        distances = cosine_distances(query_row, candidates)[0]
    elif metric == "euclidean":
        distances = euclidean_distances(query_row, candidates)[0]
    else:
        raise ValueError(f"Unknown metric: {metric!r} (use 'cosine' or 'euclidean')")

    nearest_count = min(k, len(distances))
    indices = np.argsort(distances)[:nearest_count]
    return indices, distances[indices]


def embedding_projection_2d(
    embeddings: np.ndarray, method: str = "tsne"
) -> np.ndarray:
    """
    Project high-dimensional embeddings to 2-D for visualization.

    Args:
        embeddings: Array of shape ``(n, latent_dim)``.
        method: ``"tsne"`` (default) or ``"umap"``. UMAP falls back to
            t-SNE with a warning if ``umap-learn`` is not installed.

    Returns:
        Projected coordinates of shape ``(n, 2)``.

    Raises:
        ValueError: If ``method`` is unknown.
    """
    if method == "tsne":
        return _project_tsne(embeddings)
    if method == "umap":
        return _project_umap(embeddings)
    raise ValueError(f"Unknown method: {method!r} (use 'tsne' or 'umap')")


def _project_tsne(embeddings: np.ndarray) -> np.ndarray:
    """Project embeddings to 2-D with t-SNE (perplexity capped for small n)."""
    from sklearn.manifold import TSNE

    # t-SNE requires perplexity < n_samples; cap it for small batches.
    max_perplexity = max(1.0, (embeddings.shape[0] - 1) / 3.0)
    default_perplexity = 30.0
    tsne = TSNE(
        n_components=PROJECTION_COMPONENTS_2D,
        random_state=RANDOM_SEED,
        perplexity=min(default_perplexity, max_perplexity),
    )
    return tsne.fit_transform(embeddings)


def _project_umap(embeddings: np.ndarray) -> np.ndarray:
    """Project embeddings to 2-D with UMAP, falling back to t-SNE."""
    try:
        import umap
    except ImportError:
        logger.warning("umap-learn not installed; falling back to t-SNE projection")
        return _project_tsne(embeddings)

    reducer = umap.UMAP(
        n_components=PROJECTION_COMPONENTS_2D, random_state=RANDOM_SEED
    )
    return reducer.fit_transform(embeddings)
