"""
Unit tests for src/models/embedding_utils.py (Gap 1 — Vector Embeddings).

Pure numpy/sklearn helpers: normalization, statistics, nearest-neighbor
search, and 2-D projection. Projection tests use a tiny N so t-SNE stays
fast; the UMAP path is exercised via a forced-ImportError fallback.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.config import LATENT_DIM, PROJECTION_COMPONENTS_2D, RANDOM_SEED
from src.models.embedding_utils import (
    embedding_projection_2d,
    embedding_statistics,
    find_nearest_embeddings,
    normalize_embeddings,
)

# Tiny cloud keeps t-SNE projection fast while exercising all code paths.
TINY_CLOUD_SIZE = 8


@pytest.fixture
def embedding_cloud() -> np.ndarray:
    rng = np.random.default_rng(RANDOM_SEED)
    return rng.random((TINY_CLOUD_SIZE, LATENT_DIM))


class TestNormalizeEmbeddings:
    def test_rows_have_unit_norm(self, embedding_cloud):
        normalized = normalize_embeddings(embedding_cloud)
        norms = np.linalg.norm(normalized, axis=1)
        assert np.allclose(norms, 1.0)

    def test_shape_is_preserved(self, embedding_cloud):
        assert normalize_embeddings(embedding_cloud).shape == embedding_cloud.shape

    def test_zero_rows_stay_zero_without_nan(self):
        embeddings = np.zeros((3, LATENT_DIM))
        embeddings[0, 0] = 2.0
        normalized = normalize_embeddings(embeddings)
        assert np.isfinite(normalized).all()
        assert np.allclose(normalized[1], 0.0)
        assert np.allclose(normalized[2], 0.0)

    def test_direction_is_preserved(self):
        embeddings = np.array([[3.0, 4.0] + [0.0] * (LATENT_DIM - 2)])
        normalized = normalize_embeddings(embeddings)
        assert normalized[0, 0] == pytest.approx(0.6)
        assert normalized[0, 1] == pytest.approx(0.8)


class TestEmbeddingStatistics:
    def test_returns_expected_keys(self, embedding_cloud):
        stats = embedding_statistics(embedding_cloud)
        assert set(stats) == {
            "mean",
            "std",
            "covariance",
            "pca_variance_explained",
            "effective_dims",
        }

    def test_mean_and_std_shapes(self, embedding_cloud):
        stats = embedding_statistics(embedding_cloud)
        assert stats["mean"].shape == (LATENT_DIM,)
        assert stats["std"].shape == (LATENT_DIM,)
        assert np.allclose(stats["mean"], embedding_cloud.mean(axis=0))

    def test_covariance_shape(self, embedding_cloud):
        stats = embedding_statistics(embedding_cloud)
        assert stats["covariance"].shape == (LATENT_DIM, LATENT_DIM)

    def test_pca_variance_sums_to_one(self, embedding_cloud):
        stats = embedding_statistics(embedding_cloud)
        assert stats["pca_variance_explained"].sum() == pytest.approx(1.0)

    def test_effective_dims_is_valid_count(self, embedding_cloud):
        stats = embedding_statistics(embedding_cloud)
        assert isinstance(stats["effective_dims"], int)
        assert 1 <= stats["effective_dims"] <= LATENT_DIM

    def test_rejects_single_embedding(self):
        with pytest.raises(ValueError, match="at least 2"):
            embedding_statistics(np.zeros((1, LATENT_DIM)))

    def test_rejects_1d_input(self):
        with pytest.raises(ValueError, match="2-D"):
            embedding_statistics(np.zeros(LATENT_DIM))


class TestFindNearestEmbeddings:
    def test_exact_match_is_nearest_with_zero_distance(self, embedding_cloud):
        query = embedding_cloud[4]
        indices, distances = find_nearest_embeddings(query, embedding_cloud, k=3)
        assert indices[0] == 4
        assert distances[0] == pytest.approx(0.0, abs=1e-6)

    def test_distances_are_ascending(self, embedding_cloud):
        rng = np.random.default_rng(RANDOM_SEED + 1)
        query = rng.random(LATENT_DIM)
        _, distances = find_nearest_embeddings(query, embedding_cloud, k=5)
        assert np.all(np.diff(distances) >= 0)

    def test_returns_k_results(self, embedding_cloud):
        query = embedding_cloud[0]
        indices, distances = find_nearest_embeddings(query, embedding_cloud, k=3)
        assert len(indices) == 3
        assert len(distances) == 3

    def test_k_is_clamped_to_candidate_count(self, embedding_cloud):
        query = embedding_cloud[0]
        indices, _ = find_nearest_embeddings(query, embedding_cloud, k=100)
        assert len(indices) == TINY_CLOUD_SIZE

    def test_euclidean_metric(self, embedding_cloud):
        query = embedding_cloud[2]
        indices, distances = find_nearest_embeddings(
            query, embedding_cloud, k=2, metric="euclidean"
        )
        assert indices[0] == 2
        assert distances[0] == pytest.approx(0.0, abs=1e-6)

    def test_unknown_metric_raises(self, embedding_cloud):
        with pytest.raises(ValueError, match="Unknown metric"):
            find_nearest_embeddings(
                embedding_cloud[0], embedding_cloud, metric="manhattan"
            )

    def test_empty_candidates_raise(self):
        with pytest.raises(ValueError, match="empty"):
            find_nearest_embeddings(
                np.zeros(LATENT_DIM), np.empty((0, LATENT_DIM))
            )


class TestEmbeddingProjection2D:
    def test_tsne_projects_to_2d(self, embedding_cloud):
        projected = embedding_projection_2d(embedding_cloud, method="tsne")
        assert projected.shape == (TINY_CLOUD_SIZE, 2)
        assert np.isfinite(projected).all()

    def test_unknown_method_raises(self, embedding_cloud):
        with pytest.raises(ValueError, match="Unknown method"):
            embedding_projection_2d(embedding_cloud, method="pca")

    def test_umap_falls_back_to_tsne_when_not_installed(
        self, embedding_cloud, caplog
    ):
        # A None entry in sys.modules makes `import umap` raise ImportError,
        # forcing the graceful t-SNE fallback path.
        with patch.dict(sys.modules, {"umap": None}):
            with caplog.at_level("WARNING"):
                projected = embedding_projection_2d(embedding_cloud, method="umap")
        assert projected.shape == (TINY_CLOUD_SIZE, 2)
        assert "falling back" in caplog.text.lower()

    def test_umap_reducer_used_when_installed(self, embedding_cloud):
        # Inject a stub umap module so the real (non-fallback) branch runs
        # without requiring the optional umap-learn dependency.
        fake_umap_module = ModuleType("umap")
        fake_umap_module.UMAP = MagicMock(
            return_value=MagicMock(
                fit_transform=MagicMock(
                    return_value=np.zeros((TINY_CLOUD_SIZE, 2))
                )
            )
        )
        with patch.dict(sys.modules, {"umap": fake_umap_module}):
            projected = embedding_projection_2d(embedding_cloud, method="umap")
        assert projected.shape == (TINY_CLOUD_SIZE, 2)
        fake_umap_module.UMAP.assert_called_once_with(
            n_components=PROJECTION_COMPONENTS_2D, random_state=RANDOM_SEED
        )
