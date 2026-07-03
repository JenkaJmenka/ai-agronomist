"""
Unit tests for AnomalyDetector.predict_batch_enriched (Gap 1 integration).

Uses a real LSTM-VAE loaded from a temporary checkpoint. VAE reconstruction
scores are stochastic even in eval mode (reparameterization sampling), so
tests assert shapes, ranges, and the deterministic embedding (encoder mu)
contract — never exact score values.
"""

import numpy as np
import pytest
import torch

from src.config import INPUT_DIM, LATENT_DIM, MODEL_CONFIG, RANDOM_SEED, SEQ_LEN
from src.models.anomaly_embedding import AnomalyEmbedding
from src.models.inference import AnomalyDetector
from src.models.lstm_vae import LSTMVAE

BATCH_SIZE = 6


@pytest.fixture
def checkpoint_path(tmp_path) -> str:
    """Persist a fresh LSTMVAE checkpoint in the expected on-disk format."""
    model = LSTMVAE(**MODEL_CONFIG)
    path = str(tmp_path / "test_model.pt")
    torch.save(
        {"model_config": MODEL_CONFIG, "model_state_dict": model.state_dict()},
        path,
    )
    return path


@pytest.fixture
def detector(checkpoint_path) -> AnomalyDetector:
    instance = AnomalyDetector(checkpoint_path=checkpoint_path)
    yield instance


@pytest.fixture
def sequence_batch() -> np.ndarray:
    rng = np.random.default_rng(RANDOM_SEED)
    return rng.random((BATCH_SIZE, SEQ_LEN, INPUT_DIM)).astype(np.float32)


class TestPredictBatchEnriched:
    def test_returns_scores_embeddings_uncertainties_shapes(
        self, detector, sequence_batch
    ):
        scores, embeddings, uncertainties = detector.predict_batch_enriched(
            sequence_batch
        )
        assert scores.shape == (BATCH_SIZE,)
        assert embeddings.shape == (BATCH_SIZE, LATENT_DIM)
        assert uncertainties.shape == (BATCH_SIZE,)

    def test_accepts_list_of_sequences(self, detector, sequence_batch):
        as_list = [sequence_batch[i] for i in range(BATCH_SIZE)]
        scores, embeddings, uncertainties = detector.predict_batch_enriched(as_list)
        assert scores.shape == (BATCH_SIZE,)
        assert embeddings.shape == (BATCH_SIZE, LATENT_DIM)
        assert uncertainties.shape == (BATCH_SIZE,)

    def test_scores_are_non_negative(self, detector, sequence_batch):
        scores, _, _ = detector.predict_batch_enriched(sequence_batch)
        assert (scores >= 0.0).all()

    def test_uncertainties_are_positive(self, detector, sequence_batch):
        # mean of exp(0.5*logvar) (std) is strictly positive by construction
        _, _, uncertainties = detector.predict_batch_enriched(sequence_batch)
        assert (uncertainties > 0.0).all()

    def test_embeddings_match_encoder_mu(self, detector, sequence_batch):
        """Embeddings must equal AnomalyEmbedding's mu (deterministic in eval)."""
        _, embeddings, _ = detector.predict_batch_enriched(sequence_batch)
        embedder = AnomalyEmbedding(detector.model, device="cpu")
        expected_mu = embedder.embed_batch(sequence_batch)
        assert np.allclose(embeddings, expected_mu, atol=1e-5)

    def test_embeddings_deterministic_across_calls(self, detector, sequence_batch):
        _, first, _ = detector.predict_batch_enriched(sequence_batch)
        _, second, _ = detector.predict_batch_enriched(sequence_batch)
        assert np.allclose(first, second, atol=1e-5)

    def test_rejects_nan_input(self, detector, sequence_batch):
        corrupted = sequence_batch.copy()
        corrupted[0, 0, 0] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            detector.predict_batch_enriched(corrupted)

    def test_rejects_inf_input(self, detector, sequence_batch):
        corrupted = sequence_batch.copy()
        corrupted[0, 0, 0] = np.inf
        with pytest.raises(ValueError, match="NaN or Inf"):
            detector.predict_batch_enriched(corrupted)

    def test_rejects_2d_input(self, detector, sequence_batch):
        with pytest.raises(ValueError, match="3D"):
            detector.predict_batch_enriched(sequence_batch[0])


class TestExistingContractsUnchanged:
    """Gap 1 must not alter the predict / predict_batch contracts."""

    def test_predict_returns_per_sequence_scores(self, detector, sequence_batch):
        scores = detector.predict(sequence_batch)
        assert scores.shape == (BATCH_SIZE,)
        assert (scores >= 0.0).all()

    def test_predict_batch_accepts_list(self, detector, sequence_batch):
        as_list = [sequence_batch[i] for i in range(BATCH_SIZE)]
        scores = detector.predict_batch(as_list)
        assert scores.shape == (BATCH_SIZE,)

    def test_predict_rejects_nan(self, detector, sequence_batch):
        corrupted = sequence_batch.copy()
        corrupted[0, 0, 0] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            detector.predict(corrupted)

    def test_missing_checkpoint_raises_runtime_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="Failed to load model"):
            AnomalyDetector(checkpoint_path=str(tmp_path / "missing.pt"))

    def test_route_cache_reuses_detector_instance(self, checkpoint_path):
        # Instance reuse moved to the route-level LRUDict (single eviction policy).
        from src.api.routes.inference import clear_detector_cache, get_cached_detector

        clear_detector_cache()
        try:
            first = get_cached_detector(checkpoint_path)
            second = get_cached_detector(checkpoint_path)
            assert first is second
            assert first.model is second.model
        finally:
            clear_detector_cache()
