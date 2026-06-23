"""Unit tests for the LSTMVAE model architecture and core functionality."""

import pytest
import torch
from pathlib import Path
from src.models.lstm_vae import LSTMVAE
from src.config import MODEL_CONFIG


class TestLSTMVAEArchitecture:
    """Tests verify the LSTM-VAE architecture contract, shapes, and properties."""

    @pytest.fixture
    def model(self) -> LSTMVAE:
        """Fixture providing an instance of the LSTMVAE model."""
        return LSTMVAE(**MODEL_CONFIG)

    @pytest.fixture
    def input_batch(self) -> torch.Tensor:
        """Fixture providing a sample input batch of time-series data."""
        batch_size = 32
        seq_len = MODEL_CONFIG["seq_len"]
        input_dim = MODEL_CONFIG["input_dim"]
        return torch.randn(batch_size, seq_len, input_dim)

    def test_model_instantiation(self, model: LSTMVAE) -> None:
        """Verifies that the model instantiates properly with correct types."""
        assert model is not None
        assert isinstance(model, LSTMVAE)

    def test_forward_pass_shapes(self, model: LSTMVAE, input_batch: torch.Tensor) -> None:
        """Checks if the forward pass output shapes match the configuration."""
        recon_x, mu, log_var, z = model(input_batch)
        
        assert recon_x.shape == input_batch.shape
        assert mu.shape == (input_batch.shape[0], MODEL_CONFIG["latent_dim"])
        assert log_var.shape == (input_batch.shape[0], MODEL_CONFIG["latent_dim"])
        assert z.shape == (input_batch.shape[0], MODEL_CONFIG["latent_dim"])

    def test_forward_pass_no_nan(self, model: LSTMVAE, input_batch: torch.Tensor) -> None:
        """Ensures that no NaN values are produced during the forward pass."""
        recon_x, mu, log_var, z = model(input_batch)
        
        assert not torch.isnan(recon_x).any()
        assert not torch.isnan(mu).any()
        assert not torch.isnan(log_var).any()
        assert not torch.isnan(z).any()

    def test_reconstruction_bounded(self, model: LSTMVAE, input_batch: torch.Tensor) -> None:
        """Checks if the reconstructed output is bounded realistically.
        
        Since LSTMVAE doesn't enforce bounds on output linearly, this test ensures
        it remains finite and doesn't explode.
        """
        recon_x, _, _, _ = model(input_batch)
        assert torch.isfinite(recon_x).all()

    def test_encode(self, model: LSTMVAE, input_batch: torch.Tensor) -> None:
        """Validates that the encoder produces correct latent shape and values."""
        mu, log_var = model.encode(input_batch)
        
        assert mu.shape == (input_batch.shape[0], MODEL_CONFIG["latent_dim"])
        assert log_var.shape == (input_batch.shape[0], MODEL_CONFIG["latent_dim"])
        assert not torch.isnan(mu).any()
        assert not torch.isnan(log_var).any()

    def test_decode(self, model: LSTMVAE) -> None:
        """Validates that the decoder converts latent vectors back to sequence shape."""
        batch_size = 32
        latent_dim = MODEL_CONFIG["latent_dim"]
        z = torch.randn(batch_size, latent_dim)
        
        recon = model.decode(z)
        
        assert recon.shape == (batch_size, MODEL_CONFIG["seq_len"], MODEL_CONFIG["input_dim"])
        assert not torch.isnan(recon).any()

    def test_reparameterization(self, model: LSTMVAE, input_batch: torch.Tensor) -> None:
        """Validates the reparameterization trick behavior and stochasticity."""
        mu, log_var = model.encode(input_batch)
        z = model.reparameterize(mu, log_var)
        
        assert z.shape == mu.shape
        assert not torch.isnan(z).any()
        
        # Two stochastic passes should yield distinct random values
        z2 = model.reparameterize(mu, log_var)
        assert not torch.allclose(z, z2)

    def test_loss_calculation(self, model: LSTMVAE, input_batch: torch.Tensor) -> None:
        """Verifies that the loss is a non-nan positive scalar."""
        recon_x, mu, log_var, _ = model(input_batch)
        loss = model.loss_function(recon_x, input_batch, mu, log_var)
        
        assert loss.item() > 0
        assert not torch.isnan(loss)

    def test_loss_components(self, model: LSTMVAE, input_batch: torch.Tensor) -> None:
        """Ensures that the total loss accounts for the reconstruction (MSE) loss component."""
        recon_x, mu, log_var, _ = model(input_batch)
        loss = model.loss_function(recon_x, input_batch, mu, log_var)
        
        # Reconstruction loss (MSE) should be positive
        recon_loss = torch.nn.functional.mse_loss(recon_x, input_batch)
        assert recon_loss > 0
        
        # Total loss should be >= reconstruction loss
        assert loss >= recon_loss


class TestCheckpointSaving:
    """Verifies that checkpoint save and load workflows operate correctly."""

    @pytest.fixture
    def model(self) -> LSTMVAE:
        return LSTMVAE(**MODEL_CONFIG)

    @pytest.fixture
    def temp_dir(self, tmp_path) -> Path:
        """Create temporary directory for checkpoints."""
        return tmp_path / "checkpoints"

    def test_checkpoint_save(self, model: LSTMVAE, temp_dir) -> None:
        """Verifies checkpoint files can be successfully created on disk."""
        temp_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = temp_dir / "test_checkpoint.pt"
        
        model.save_checkpoint(
            path=str(checkpoint_path),
            epoch=1,
            loss=0.05,
            accuracy=85.0
        )
        
        assert checkpoint_path.exists()

    def test_checkpoint_metadata(self, model: LSTMVAE, temp_dir) -> None:
        """Verifies checkpoint file contains all required metadata fields."""
        temp_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = temp_dir / "test_checkpoint.pt"
        
        model.save_checkpoint(
            path=str(checkpoint_path),
            epoch=5,
            loss=0.03,
            accuracy=90.0
        )
        
        checkpoint = torch.load(checkpoint_path, weights_only=False)
        
        assert "epoch" in checkpoint
        assert "loss" in checkpoint
        assert "accuracy" in checkpoint
        assert "timestamp" in checkpoint
        assert checkpoint["epoch"] == 5
        assert checkpoint["loss"] == 0.03
        assert checkpoint["accuracy"] == 90.0

    def test_checkpoint_load(self, model: LSTMVAE, temp_dir) -> None:
        """Verifies saved checkpoint metadata can be successfully loaded back."""
        temp_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = temp_dir / "test_checkpoint.pt"
        
        model.save_checkpoint(
            path=str(checkpoint_path),
            epoch=10,
            loss=0.02,
            accuracy=95.0
        )
        
        metadata = model.load_checkpoint(str(checkpoint_path))
        
        assert metadata["epoch"] == 10
        assert metadata["loss"] == 0.02
        assert metadata["accuracy"] == 95.0
