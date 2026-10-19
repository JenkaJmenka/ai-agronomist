"""
Implementation of the LSTM-Variational Autoencoder (LSTM-VAE) for greenhouse anomaly detection.
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal

from src.config import (
    INPUT_DIM,
    SEQ_LEN,
    LATENT_DIM,
    HIDDEN_DIMS,
    DROPOUT_RATE,
    BETA,
)


class LSTMVAE(nn.Module):
    """
    LSTM-Variational Autoencoder (LSTM-VAE) for time-series anomaly detection.

    This model uses LSTM layers to encode temporal dependencies into a latent
    distribution, which is then sampled and decoded to reconstruct the input
    sequence. The loss function includes a beta-weighted KL divergence term
    to encourage a disentangled latent space.

    Attributes:
        input_dim (int): Number of input features per time step.
        hidden_dims (List[int]): Dimensions of the LSTM hidden layers.
        latent_dim (int): Dimension of the latent space.
        seq_len (int): Length of the input sequence.
        beta (float): KL divergence weight.
        dropout (int): Dropout probability.
    """

    def __init__(
        self,
        input_dim: int = INPUT_DIM,
        hidden_dims: List[int] = HIDDEN_DIMS,
        latent_dim: int = LATENT_DIM,
        seq_len: int = SEQ_LEN,
        beta: float = BETA,
        dropout: float = DROPOUT_RATE,
    ) -> None:
        """
        Initializes the LSTM-VAE model.

        Args:
            input_dim: Number of input features.
            hidden_dims: List of dimensions for the LSTM layers.
            latent_dim: Dimension of the latent space.
            seq_len: Length of the input sequence.
            beta: KL divergence weight.
            dropout: Dropout probability.
        """
        super().__init__()

        self.input_dim = input_dim
        self.hidden_dims = hidden_dims
        self.latent_dim = latent_dim
        self.seq_len = seq_len
        self.beta = beta
        self.dropout = dropout

        # --- Encoder ---
        # We use a ModuleList to handle an arbitrary number of LSTM layers
        self.encoder_layers = nn.ModuleList()
        current_dim = input_dim
        for h_dim in hidden_dims:
            self.encoder_layers.append(
                nn.LSTM(input_size=current_dim, hidden_size=h_dim, batch_first=True)
            )
            current_dim = h_dim

        self.encoder_dropout = nn.Dropout(dropout)

        # Latent space projections: mapping the last hidden state to mu and log_var
        self.fc_mu = nn.Linear(hidden_dims[-1], latent_dim)
        self.fc_logvar = nn.Linear(hidden_dims[-1], latent_dim)

        # --- Decoder ---
        self.decoder_layers = nn.ModuleList()
        # We'll use the latent vector as an input sequence of length 1, then expand it.
        current_dim = latent_dim
        for h_dim in hidden_dims:
            self.decoder_layers.append(
                nn.LSTM(input_size=current_dim, hidden_size=h_dim, batch_first=True)
            )
            current_dim = h_dim

        self.decoder_dropout = nn.Dropout(dropout)
        self.fc_out = nn.Linear(hidden_dims[-1], input_dim)

    def reparameterize(self, mu: torch.Tensor, logvar: torch.Tensor) -> torch.Tensor:
        """
        Applies the reparameterization trick: z = mu + std * epsilon, where epsilon ~ N(0, 1).

        In training mode, samples from the latent distribution to enable
        gradient flow through the KL divergence term.  At inference
        (``self.training is False``), returns ``mu`` directly so that
        scoring is deterministic across calls.

        Args:
            mu: Mean of the latent distribution.
            logvar: Log variance of the latent distribution.

        Returns:
            Sampled latent vector z (training) or the mean mu (inference).
        """
        if not self.training:
            return mu
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return mu + eps * std

    def encode(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Encodes the input sequence into mu and log_var.

        Args:
            x: Input tensor of shape (batch_size, seq_len, input_dim).

        Returns:
            A tuple containing (mu, log_var).
        """
        h = x
        for layer in self.encoder_layers:
            # LSTM returns (output, (h_n, c_n))
            h, _ = layer(h)

        # Use the last hidden state of the last LSTM layer as the summary of the sequence
        # h shape: (batch_size, seq_len, hidden_dim_last)
        # We take the last time step: (batch_size, hidden_dim_last)
        last_h = h[:, -1, :]
        last_h = self.encoder_dropout(last_h)

        mu = self.fc_mu(last_h)
        logvar = self.fc_logvar(last_h)

        return mu, logvar

    def decode(self, z: torch.Tensor) -> torch.Tensor:
        """
        Decodes the latent vector back into the original sequence.

        Args:
            z: Latent vector of shape (batch_size, latent_dim).

        Returns:
            Reconstructed tensor of shape (batch_size, seq_len, input_dim).
        """
        # We expand the latent vector z to cover the sequence length
        # (batch_size, latent_dim) -> (batch_size, 1, latent_dim) -> (batch_size, seq_len, latent_dim)
        z_expanded = z.unsqueeze(1).repeat(1, self.seq_len, 1)
        h = z_expanded

        for layer in self.decoder_layers:
            h, _ = layer(h)
            h = self.decoder_dropout(h)

        # Final projection to the input dimension (batch_size, seq_len, input_dim)
        reconstruction = self.fc_out(h)
        return reconstruction

    def forward(
        self, x: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Performs the full forward pass: Encode -> Reparameterize -> Decode.

        Args:
            x: Input tensor of shape (batch_size, seq_len, input_dim).

        Returns:
            A tuple containing (reconstruction, mu, log_var, z), where z is the
            sampled latent vector of shape (batch_size, latent_dim).
        """
        mu, logvar = self.encode(x)
        z = self.reparameterize(mu, logvar)
        reconstruction = self.decode(z)

        return reconstruction, mu, logvar, z

    def loss_function(
        self,
        recon_x: torch.Tensor,
        x: torch.Tensor,
        mu: torch.Tensor,
        logvar: torch.Tensor,
    ) -> torch.Tensor:
        """
        Computes the beta-VAE loss: reconstruction loss + beta * KL divergence.

        The reconstruction term is the mean squared error between the
        reconstruction and the input. The KL divergence uses the standard
        closed form for a Gaussian latent against a unit Gaussian prior,
        averaged over the batch so it is reduced consistently with the MSE.

        Args:
            recon_x: Reconstructed tensor of shape (batch_size, seq_len, input_dim).
            x: Original input tensor of shape (batch_size, seq_len, input_dim).
            mu: Mean of the latent distribution (batch_size, latent_dim).
            logvar: Log variance of the latent distribution (batch_size, latent_dim).

        Returns:
            torch.Tensor: A scalar tensor containing the total loss.
        """
        recon_loss = F.mse_loss(recon_x, x, reduction="mean")

        batch_size = x.size(0)
        kl_divergence = -0.5 * torch.sum(
            1 + logvar - mu.pow(2) - logvar.exp()
        ) / batch_size

        return recon_loss + self.beta * kl_divergence

    def save_checkpoint(
        self,
        path: str,
        epoch: int,
        loss: float,
        accuracy: float,
        timestamp: Optional[datetime] = None,
        model_config: Optional[Dict] = None,
    ) -> None:
        """
        Saves the model state dict along with training metadata to disk.

        Args:
            path: Destination file path for the checkpoint.
            epoch: The epoch index associated with this checkpoint.
            loss: The loss value recorded at this checkpoint.
            accuracy: The accuracy value recorded at this checkpoint.
            timestamp: Optional timestamp; defaults to datetime.now().
            model_config: Optional model configuration; defaults to one built
                from the model's own attributes.
        """
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        if timestamp is None:
            timestamp = datetime.now()

        if model_config is None:
            model_config = {
                "input_dim": self.input_dim,
                "hidden_dims": self.hidden_dims,
                "latent_dim": self.latent_dim,
                "seq_len": self.seq_len,
                "beta": self.beta,
                "dropout": self.dropout,
            }

        checkpoint: Dict = {
            "model_state_dict": self.state_dict(),
            "epoch": epoch,
            "loss": loss,
            "accuracy": accuracy,
            "timestamp": timestamp,
            "model_config": model_config,
        }
        torch.save(checkpoint, path)

    def load_checkpoint(self, path: str) -> Dict:
        """
        Loads a checkpoint from disk into the model and returns its metadata.

        Args:
            path: Path to the checkpoint file produced by save_checkpoint.

        Returns:
            Dict: The full checkpoint dictionary, including metadata such as
                epoch, loss, accuracy, timestamp, and the model config.
        """
        from src.config import DEVICE
        checkpoint = torch.load(path, map_location=DEVICE, weights_only=False)
        self.load_state_dict(checkpoint["model_state_dict"])
        return checkpoint


if __name__ == "__main__":
    # Small test script to verify the shape contracts and instantiation
    import torch

    # Use config values for testing
    DEVICE = torch.device("cpu")
    TEST_BATCH_SIZE = 32

    model = LSTMVAE(
        input_dim=INPUT_DIM,
        hidden_dims=HIDDEN_DIMS,
        latent_dim=LATENT_DIM,
        seq_len=SEQ_LEN,
        beta=BETA,
        dropout=DROPOUT_RATE
    ).to(DEVICE)

    dummy_input = torch.randn(TEST_BATCH_SIZE, SEQ_LEN, INPUT_DIM).to(DEVICE)

    try:
        reconstruction, mu, logvar, z = model(dummy_input)

        print(f"Input shape:  {dummy_input.shape}")
        print(f"Recon shape:  {reconstruction.shape}")
        print(f"Mu shape:     {mu.shape}")
        print(f"Logvar shape: {logvar.shape}")
        print(f"Z shape:      {z.shape}")

        assert reconstruction.shape == dummy_input.shape
        assert mu.shape == (TEST_BATCH_SIZE, LATENT_DIM)
        assert logvar.shape == (TEST_BATCH_SIZE, LATENT_DIM)
        assert z.shape == (TEST_BATCH_SIZE, LATENT_DIM)

        loss = model.loss_function(reconstruction, dummy_input, mu, logvar)
        print(f"Loss:         {loss.item():.4f}")
        assert loss.dim() == 0
        print("Verification successful: Shape contracts match!")
    except Exception as e:
        print(f"Verification failed: {e}")
        raise e
