"""
Unit tests for src/models/anomaly_embedding.py (Gap 1 — Vector Embeddings).

Covers shape contracts, distance/similarity identities, uncertainty
extraction, clustering, decode round-trips, and EnrichedAnomalyResult
serialization. Encoder mu is deterministic in eval mode, so embedding
comparisons use np.allclose — never exact reconstruction-score equality
(VAE scoring is stochastic via reparameterization).
"""

from datetime import datetime, timezone

import numpy as np
import pytest

from src.config import (
    EMBEDDING_CONFIDENCE_THRESHOLD,
    INPUT_DIM,
    LATENT_DIM,
    RANDOM_SEED,
    SEQ_LEN,
)
from src.models.anomaly_embedding import AnomalyEmbedding, EnrichedAnomalyResult


@pytest.fixture
def embedder(model) -> AnomalyEmbedding:
    """AnomalyEmbedding wrapping the shared LSTMVAE test model."""
    return AnomalyEmbedding(model, device="cpu")


@pytest.fixture
def sample_sequence() -> np.ndarray:
    """Single sensor sequence of shape (SEQ_LEN, INPUT_DIM)."""
    rng = np.random.default_rng(RANDOM_SEED)
    return rng.random((SEQ_LEN, INPUT_DIM))


@pytest.fixture
def sample_batch() -> np.ndarray:
    """Batch of sensor sequences of shape (10, SEQ_LEN, INPUT_DIM)."""
    rng = np.random.default_rng(RANDOM_SEED)
    return rng.random((10, SEQ_LEN, INPUT_DIM))


class TestEmbedSequence:
    def test_embedding_shape_is_latent_dim(self, embedder, sample_sequence):
        embedding = embedder.embed_sequence(sample_sequence)
        assert embedding.shape == (LATENT_DIM,)

    def test_accepts_singleton_batch_input(self, embedder, sample_sequence):
        embedding = embedder.embed_sequence(sample_sequence[np.newaxis, :])
        assert embedding.shape == (LATENT_DIM,)

    def test_deterministic_in_eval_mode(self, embedder, sample_sequence):
        first = embedder.embed_sequence(sample_sequence)
        second = embedder.embed_sequence(sample_sequence)
        assert np.allclose(first, second)

    def test_rejects_1d_input(self, embedder):
        with pytest.raises(ValueError, match="shape"):
            embedder.embed_sequence(np.zeros(SEQ_LEN))

    def test_rejects_multi_item_3d_input(self, embedder, sample_batch):
        with pytest.raises(ValueError, match="shape"):
            embedder.embed_sequence(sample_batch)

    def test_embedding_dim_attribute_matches_config(self, embedder):
        assert embedder.embedding_dim == LATENT_DIM


class TestEmbedBatch:
    def test_batch_embedding_shape(self, embedder, sample_batch):
        embeddings = embedder.embed_batch(sample_batch)
        assert embeddings.shape == (10, LATENT_DIM)

    def test_batch_row_matches_single_sequence_embedding(
        self, embedder, sample_batch
    ):
        batch_embeddings = embedder.embed_batch(sample_batch)
        single_embedding = embedder.embed_sequence(sample_batch[3])
        assert np.allclose(batch_embeddings[3], single_embedding, atol=1e-5)

    def test_rejects_2d_input(self, embedder, sample_sequence):
        with pytest.raises(ValueError, match="3-D"):
            embedder.embed_batch(sample_sequence)


class TestEmbedWithUncertainty:
    def test_returns_expected_keys(self, embedder, sample_sequence):
        enriched = embedder.embed_with_uncertainty(sample_sequence)
        assert set(enriched) == {"embedding", "uncertainty", "mu", "logvar", "std"}

    def test_embedding_and_latent_shapes(self, embedder, sample_sequence):
        enriched = embedder.embed_with_uncertainty(sample_sequence)
        assert enriched["embedding"].shape == (LATENT_DIM,)
        assert enriched["mu"].shape == (LATENT_DIM,)
        assert enriched["logvar"].shape == (LATENT_DIM,)
        assert enriched["std"].shape == (LATENT_DIM,)

    def test_uncertainty_is_non_negative_float(self, embedder, sample_sequence):
        enriched = embedder.embed_with_uncertainty(sample_sequence)
        assert isinstance(enriched["uncertainty"], float)
        assert enriched["uncertainty"] >= 0.0

    def test_embedding_equals_mu(self, embedder, sample_sequence):
        enriched = embedder.embed_with_uncertainty(sample_sequence)
        assert np.allclose(enriched["embedding"], enriched["mu"])

    def test_std_is_exp_half_logvar(self, embedder, sample_sequence):
        enriched = embedder.embed_with_uncertainty(sample_sequence)
        assert np.allclose(enriched["std"], np.exp(0.5 * enriched["logvar"]))


class TestDistanceAndSimilarity:
    @pytest.fixture
    def random_embedding(self) -> np.ndarray:
        rng = np.random.default_rng(RANDOM_SEED)
        return rng.random(LATENT_DIM)

    def test_cosine_self_distance_is_zero(self, embedder, random_embedding):
        assert embedder.embedding_distance(
            random_embedding, random_embedding
        ) == pytest.approx(0.0, abs=1e-6)

    def test_euclidean_self_distance_is_zero(self, embedder, random_embedding):
        distance = embedder.embedding_distance(
            random_embedding, random_embedding, metric="euclidean"
        )
        assert distance == pytest.approx(0.0, abs=1e-6)

    def test_distance_is_symmetric(self, embedder, random_embedding):
        rng = np.random.default_rng(RANDOM_SEED + 1)
        other = rng.random(LATENT_DIM)
        forward = embedder.embedding_distance(random_embedding, other)
        backward = embedder.embedding_distance(other, random_embedding)
        assert forward == pytest.approx(backward)

    def test_euclidean_distance_known_value(self, embedder):
        unit_x = np.zeros(LATENT_DIM)
        unit_y = np.zeros(LATENT_DIM)
        unit_x[0] = 1.0
        unit_y[1] = 1.0
        distance = embedder.embedding_distance(unit_x, unit_y, metric="euclidean")
        assert distance == pytest.approx(np.sqrt(2.0))

    def test_unknown_metric_raises(self, embedder, random_embedding):
        with pytest.raises(ValueError, match="Unknown metric"):
            embedder.embedding_distance(
                random_embedding, random_embedding, metric="manhattan"
            )

    def test_self_similarity_is_one(self, embedder, random_embedding):
        assert embedder.embedding_similarity(
            random_embedding, random_embedding
        ) == pytest.approx(1.0, abs=1e-6)

    def test_opposite_vectors_have_similarity_minus_one(self, embedder):
        vector = np.ones(LATENT_DIM)
        assert embedder.embedding_similarity(vector, -vector) == pytest.approx(-1.0)

    def test_orthogonal_vectors_have_similarity_zero(self, embedder):
        unit_x = np.zeros(LATENT_DIM)
        unit_y = np.zeros(LATENT_DIM)
        unit_x[0] = 1.0
        unit_y[1] = 1.0
        assert embedder.embedding_similarity(unit_x, unit_y) == pytest.approx(
            0.0, abs=1e-6
        )


class TestClusterEmbeddings:
    @pytest.fixture
    def embedding_cloud(self) -> np.ndarray:
        rng = np.random.default_rng(RANDOM_SEED)
        return rng.random((60, LATENT_DIM))

    def test_cluster_output_shapes(self, embedder, embedding_cloud):
        clusters = embedder.cluster_embeddings(embedding_cloud, n_clusters=3)
        assert clusters["cluster_centers"].shape == (3, LATENT_DIM)
        assert len(clusters["labels"]) == 60
        assert clusters["n_clusters"] == 3

    def test_labels_within_cluster_range(self, embedder, embedding_cloud):
        clusters = embedder.cluster_embeddings(embedding_cloud, n_clusters=3)
        unique_labels = np.unique(clusters["labels"])
        assert len(unique_labels) <= 3
        assert unique_labels.min() >= 0
        assert unique_labels.max() < 3

    def test_inertia_is_non_negative_float(self, embedder, embedding_cloud):
        clusters = embedder.cluster_embeddings(embedding_cloud, n_clusters=3)
        assert isinstance(clusters["inertia"], float)
        assert clusters["inertia"] >= 0.0

    def test_rejects_fewer_embeddings_than_clusters(self, embedder):
        rng = np.random.default_rng(RANDOM_SEED)
        with pytest.raises(ValueError, match="at least"):
            embedder.cluster_embeddings(rng.random((2, LATENT_DIM)), n_clusters=5)


class TestDecodeEmbedding:
    def test_decode_1d_embedding_returns_sequence_shape(self, embedder):
        rng = np.random.default_rng(RANDOM_SEED)
        reconstruction = embedder.decode_embedding(rng.random(LATENT_DIM))
        assert reconstruction.shape == (SEQ_LEN, INPUT_DIM)

    def test_decode_2d_singleton_embedding(self, embedder):
        rng = np.random.default_rng(RANDOM_SEED)
        reconstruction = embedder.decode_embedding(rng.random((1, LATENT_DIM)))
        assert reconstruction.shape == (SEQ_LEN, INPUT_DIM)

    def test_embed_then_decode_round_trip_shape(self, embedder, sample_sequence):
        embedding = embedder.embed_sequence(sample_sequence)
        reconstruction = embedder.decode_embedding(embedding)
        assert reconstruction.shape == sample_sequence.shape
        assert np.isfinite(reconstruction).all()

    def test_rejects_wrong_latent_dimension(self, embedder):
        with pytest.raises(ValueError, match="dim"):
            embedder.decode_embedding(np.zeros(LATENT_DIM + 1))

    def test_rejects_3d_embedding(self, embedder):
        with pytest.raises(ValueError, match="shape"):
            embedder.decode_embedding(np.zeros((1, 1, LATENT_DIM)))


class TestEnrichedAnomalyResult:
    @pytest.fixture
    def enriched_result(self, sample_sequence) -> EnrichedAnomalyResult:
        return EnrichedAnomalyResult(
            sequence=sample_sequence,
            anomaly_score=0.42,
            embedding=np.arange(LATENT_DIM, dtype=np.float64),
            uncertainty=0.05,
        )

    def test_to_dict_contains_expected_keys(self, enriched_result):
        payload = enriched_result.to_dict()
        assert set(payload) == {
            "anomaly_score",
            "embedding",
            "uncertainty",
            "timestamp",
            "is_confident",
            "embedding_dim",
        }

    def test_to_dict_is_json_serializable_types(self, enriched_result):
        payload = enriched_result.to_dict()
        assert isinstance(payload["anomaly_score"], float)
        assert isinstance(payload["embedding"], list)
        assert all(isinstance(value, float) for value in payload["embedding"])
        assert isinstance(payload["uncertainty"], float)
        assert isinstance(payload["timestamp"], str)
        assert isinstance(payload["is_confident"], bool)
        assert isinstance(payload["embedding_dim"], int)

    def test_embedding_dim_matches_embedding_length(self, enriched_result):
        payload = enriched_result.to_dict()
        assert payload["embedding_dim"] == LATENT_DIM
        assert len(payload["embedding"]) == LATENT_DIM

    def test_is_confident_below_threshold(self, sample_sequence):
        confident = EnrichedAnomalyResult(
            sequence=sample_sequence,
            anomaly_score=0.1,
            embedding=np.zeros(LATENT_DIM),
            uncertainty=EMBEDDING_CONFIDENCE_THRESHOLD - 0.01,
        )
        assert confident.to_dict()["is_confident"] is True

    def test_is_not_confident_at_or_above_threshold(self, sample_sequence):
        uncertain = EnrichedAnomalyResult(
            sequence=sample_sequence,
            anomaly_score=0.1,
            embedding=np.zeros(LATENT_DIM),
            uncertainty=EMBEDDING_CONFIDENCE_THRESHOLD,
        )
        assert uncertain.to_dict()["is_confident"] is False

    def test_default_timestamp_is_timezone_aware(self, enriched_result):
        assert enriched_result.timestamp.tzinfo is not None

    def test_custom_timestamp_serialized_as_isoformat(self, sample_sequence):
        moment = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        enriched = EnrichedAnomalyResult(
            sequence=sample_sequence,
            anomaly_score=0.2,
            embedding=np.zeros(LATENT_DIM),
            uncertainty=0.1,
            timestamp=moment,
        )
        assert enriched.to_dict()["timestamp"] == moment.isoformat()
