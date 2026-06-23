import torch
import pytest
from src.models.lstm_vae import LSTMVAE

def test_lstm_vae_forward_pass():
    """Test that the forward pass produces correct shapes and components."""
    batch_size = 8
    seq_len = 30
    input_dim = 4
    latent_dim = 16
    device = torch.device("cpu")

    model = LSTMVAE(
        input_dim=input_dim,
        hidden_dims=[32],
        latent_dim=latent_dim,
        seq_len=seq_len,
    ).to(device)

    x = torch.randn(batch_size, seq_len, input_dim).to(device)
    recon, mu, logvar, _ = model(x)

    assert recon.shape == (batch_size, seq_len, input_dim)
    assert mu.shape == (batch_size, latent_dim)
    assert logvar.shape == (batch_size, latent_dim)

def test_lstm_vae_loss_function():
    """Test the loss function implementation."""
    batch_size = 4
    seq_len = 10
    input_dim = 4
    latent_dim = 8
    device = torch.device("cpu")

    model = LSTMVAE(
        input_dim=input_dim,
        hidden_dims=[16],
        latent_dim=latent_dim,
        seq_len=seq_len,
    ).to(device)

    x = torch.randn(batch_size, seq_len, input_dim).to(device)
    recon, mu, logvar, _ = model(x)

    loss = model.loss_function(recon, x, mu, logvar)

    assert loss.dim() == 0  # Scalar loss
    assert loss.item() >= 0  # Loss should be non-negative

def test_lstm_vae_reparameterize():
    """Test the reparameterization trick (sampling)."""
    batch_size = 4
    latent_dim = 8
    device = torch.device("cpu")

    model = LSTMVAE(
        input_dim=4,
        hidden_dims=[16],
        latent_dim=latent_dim,
    ).to(device)

    mu = torch.randn(batch_size, latent_dim).to(device)
    logvar = torch.randn(batch_size, latent_dim).to(device)

    z = model.reparameterize(mu, logvar)

    assert z.shape == (batch_size, latent_dim)
