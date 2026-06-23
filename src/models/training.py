"""Training pipeline for the LSTM-VAE anomaly detection model."""

import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from src.models.lstm_vae import LSTMVAE

GRAD_CLIP_MAX_NORM: float = 1.0


class VAETrainer:
    """Trains an LSTMVAE model with TensorBoard logging, checkpointing, and early stopping.

    Loaders and config are bound at construction so that train_epoch/validate/train
    can be called without arguments (facilitating mocking in tests).

    Args:
        model: LSTMVAE model to train.
        train_loader: DataLoader for training data.
        val_loader: DataLoader for validation data.
        config: Dict with at minimum: learning_rate, weight_decay,
            early_stopping_patience, epochs.
        checkpoint_dir: Directory where per-epoch checkpoints are saved.
        log_dir: Directory for TensorBoard SummaryWriter output.
        device: Device tensors are moved to during forward/backward passes.
    """

    def __init__(
        self,
        model: LSTMVAE,
        train_loader: DataLoader,
        val_loader: DataLoader,
        config: Dict,
        checkpoint_dir: str,
        log_dir: str,
        device: torch.device,
    ) -> None:
        self.model = model.to(device)
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.config = config
        self.checkpoint_dir = str(checkpoint_dir)
        self.log_dir = str(log_dir)
        self.device = device
        self.patience: int = config.get("early_stopping_patience", 10)
        self.total_epochs: int = config.get("epochs", 100)
        self.best_val_loss: float = float("inf")
        self.patience_counter: int = 0

        os.makedirs(self.checkpoint_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

        self.optimizer = torch.optim.Adam(
            model.parameters(),
            lr=config["learning_rate"],
            weight_decay=config.get("weight_decay", 0.0),
        )
        self.writer = SummaryWriter(log_dir=self.log_dir)

    def train_epoch(self, epoch: int) -> float:
        """Run one training epoch and return mean batch loss.

        Args:
            epoch: Current epoch index (used for TensorBoard logging).

        Returns:
            Mean loss over all training batches.
        """
        self.model.train()
        total_loss = 0.0

        for (batch,) in self.train_loader:
            x = batch.to(self.device)
            self.optimizer.zero_grad()
            recon, mu, logvar, _ = self.model(x)
            loss = self.model.loss_function(recon, x, mu, logvar)
            if loss.requires_grad:
                loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), GRAD_CLIP_MAX_NORM)
                self.optimizer.step()
            total_loss += loss.item()

        return total_loss / max(len(self.train_loader), 1)

    def validate(self, epoch: int) -> float:
        """Evaluate the model on validation data and return mean loss.

        Args:
            epoch: Current epoch index (used for TensorBoard logging).

        Returns:
            Mean validation loss.
        """
        self.model.eval()
        total_loss = 0.0

        with torch.no_grad():
            for (batch,) in self.val_loader:
                x = batch.to(self.device)
                recon, mu, logvar, _ = self.model(x)
                loss = self.model.loss_function(recon, x, mu, logvar)
                total_loss += loss.item()

        return total_loss / max(len(self.val_loader), 1)

    def calculate_accuracy(self, threshold: float = 0.05) -> float:
        """Return fraction of validation samples whose per-sample MSE is below threshold.

        Args:
            threshold: MSE threshold below which reconstruction is "accurate".

        Returns:
            Float in [0, 1] — proportion of accurate reconstructions.
        """
        self.model.eval()
        correct = 0
        total = 0

        with torch.no_grad():
            for (batch,) in self.val_loader:
                x = batch.to(self.device)
                recon, mu, logvar, _ = self.model(x)
                per_sample_mse = ((recon - x) ** 2).mean(dim=tuple(range(1, x.dim())))
                correct += (per_sample_mse < threshold).sum().item()
                total += x.size(0)

        return correct / max(total, 1)

    def train(self) -> Dict:
        """Train the model for up to total_epochs, with early stopping.

        Saves a checkpoint every epoch. Stops when validation loss fails to
        improve for patience consecutive epochs.

        Returns:
            History dict with keys:
                train_loss (List[float]), val_loss (List[float]),
                accuracy (List[float]), epochs (List[int]),
                best_epoch (int), best_val_loss (float),
                training_time (float), final_accuracy (float).
        """
        train_losses: List[float] = []
        val_losses: List[float] = []
        accuracies: List[float] = []
        epoch_indices: List[int] = []
        best_epoch: int = 0

        start_time = time.time()

        for epoch in range(1, self.total_epochs + 1):
            train_loss = self.train_epoch(epoch)
            val_loss = self.validate(epoch)
            accuracy = self.calculate_accuracy()

            train_losses.append(train_loss)
            val_losses.append(val_loss)
            accuracies.append(accuracy)
            epoch_indices.append(epoch)

            self.writer.add_scalar("Loss/train", train_loss, epoch)
            self.writer.add_scalar("Loss/val", val_loss, epoch)
            self.writer.add_scalar("Accuracy/val", accuracy, epoch)

            print(
                f"Epoch {epoch:4d}/{self.total_epochs} — "
                f"train: {train_loss:.4f}  val: {val_loss:.4f}  acc: {accuracy:.4f}"
            )

            ckpt_path = os.path.join(self.checkpoint_dir, f"epoch_{epoch:04d}.pt")
            self.model.save_checkpoint(ckpt_path, epoch, val_loss, accuracy, timestamp=datetime.now())

            if val_loss < self.best_val_loss:
                self.best_val_loss = val_loss
                best_epoch = epoch
                self.patience_counter = 0
            else:
                self.patience_counter += 1
                if self.patience_counter >= self.patience:
                    print(
                        f"Early stopping at epoch {epoch} "
                        f"(no improvement for {self.patience} epochs)"
                    )
                    break

        self.writer.close()
        training_time = time.time() - start_time

        return {
            "train_loss": train_losses,
            "val_loss": val_losses,
            "accuracy": accuracies,
            "epochs": epoch_indices,
            "best_epoch": best_epoch,
            "best_val_loss": self.best_val_loss,
            "training_time": training_time,
            "final_accuracy": accuracies[-1] if accuracies else 0.0,
        }
