"""Regression guard: LSTM-VAE training mode remains stochastic.

The reparameterize guard (``if not self.training: return mu``) must NOT
suppress sampling in training mode. These tests protect the training path
so nobody "simplifies" the guard into always returning ``mu``.

Also verifies that eval mode (inference) is deterministic at every level:
reparameterize, forward, predict, and predict_batch_enriched.
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from src.config import MODEL_CONFIG
from src.models.lstm_vae import LSTMVAE

from tests.models._vae_deterministic_helpers import real_detector


class TestTrainingStillStochastic:
    """Training mode must remain stochastic -- the guard only applies in eval."""

    def test_reparameterize_stochastic_in_training(self):
        """reparameterize in training mode produces different z values."""
        model = LSTMVAE(**MODEL_CONFIG)
        model.train()
        assert model.training, "Model should be in training mode"

        mu = torch.zeros(16, MODEL_CONFIG["latent_dim"])
        logvar = torch.zeros(16, MODEL_CONFIG["latent_dim"])

        z1 = model.reparameterize(mu, logvar)
        z2 = model.reparameterize(mu, logvar)

        assert not torch.allclose(z1, z2, atol=1e-7), (
            "Two training-mode reparameterize calls returned identical z"
        )

    def test_reparameterize_deterministic_in_eval(self):
        """reparameterize in eval mode returns mu exactly."""
        model = LSTMVAE(**MODEL_CONFIG)
        model.eval()
        assert not model.training, "Model should be in eval mode"

        mu = torch.randn(16, MODEL_CONFIG["latent_dim"])
        logvar = torch.randn(16, MODEL_CONFIG["latent_dim"])

        z = model.reparameterize(mu, logvar)

        assert torch.equal(z, mu), (
            "Eval-mode reparameterize did not return mu exactly"
        )

    def test_forward_deterministic_in_eval(self):
        """Full forward pass in eval mode is deterministic."""
        model = LSTMVAE(**MODEL_CONFIG)
        model.eval()

        x = torch.randn(
            8, MODEL_CONFIG["seq_len"], MODEL_CONFIG["input_dim"]
        )

        with torch.no_grad():
            recon_a, mu_a, logvar_a, z_a = model(x)
            recon_b, mu_b, logvar_b, z_b = model(x)

        assert torch.equal(recon_a, recon_b), (
            "Reconstruction differs in eval mode"
        )
        assert torch.equal(mu_a, mu_b), "Mu differs in eval mode"
        assert torch.equal(logvar_a, logvar_b), (
            "Logvar differs in eval mode"
        )
        assert torch.equal(z_a, z_b), "Z differs in eval mode"
        assert torch.equal(z_a, mu_a), "Z should equal mu in eval mode"

    def test_forward_stochastic_in_training(self):
        """Full forward pass in training mode is stochastic (z differs)."""
        model = LSTMVAE(**MODEL_CONFIG)
        model.train()

        x = torch.randn(
            8, MODEL_CONFIG["seq_len"], MODEL_CONFIG["input_dim"]
        )

        _, _, _, z_a = model(x)
        _, _, _, z_b = model(x)

        assert not torch.allclose(z_a, z_b, atol=1e-7), (
            "Two training forward passes returned identical z"
        )

    def test_predict_deterministic_with_real_checkpoint(self):
        """AnomalyDetector.predict is deterministic with epoch-72."""
        det = real_detector()

        batch = np.random.RandomState(42).randn(
            10, MODEL_CONFIG["seq_len"], MODEL_CONFIG["input_dim"]
        ).astype(np.float32)

        scores_a = det.predict(batch)
        scores_b = det.predict(batch)

        assert np.array_equal(scores_a, scores_b), (
            f"predict() not deterministic: max_diff="
            f"{np.max(np.abs(scores_a - scores_b))}"
        )

    def test_predict_batch_enriched_deterministic(self):
        """predict_batch_enriched is deterministic with epoch-72."""
        det = real_detector()

        rng = np.random.RandomState(42)
        sequences = [
            rng.randn(
                MODEL_CONFIG["seq_len"], MODEL_CONFIG["input_dim"]
            ).astype(np.float32)
            for _ in range(20)
        ]

        scores_a, emb_a, unc_a = det.predict_batch_enriched(sequences)
        scores_b, emb_b, unc_b = det.predict_batch_enriched(sequences)

        assert np.array_equal(scores_a, scores_b), (
            "Scores not deterministic"
        )
        assert np.array_equal(emb_a, emb_b), (
            "Embeddings not deterministic"
        )
        assert np.array_equal(unc_a, unc_b), (
            "Uncertainties not deterministic"
        )
