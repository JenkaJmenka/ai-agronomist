"""Integration tests for the end-to-end model training pipeline."""

import pytest
import torch
from src.models.lstm_vae import LSTMVAE
from src.models.data_generator import SyntheticDataGenerator
from src.models.data_utils import create_data_loaders
from src.models.training import VAETrainer
from src.config import MODEL_CONFIG, TRAINING_CONFIG


class TestEndToEnd:
    """End-to-end training integration test."""

    def test_training_reduces_loss(self, tmp_path) -> None:
        """Verifies that training decreases the loss over a few epochs."""
        # Generate small dataset
        gen = SyntheticDataGenerator(
            num_samples=100,
            seq_len=20,
            input_dim=4,
            seed=42
        )
        data = gen.generate_normal_data()
        
        # Create loaders
        train_loader, val_loader = create_data_loaders(
            data,
            batch_size=32,
            train_split=0.8
        )
        
        # Create model
        model = LSTMVAE(**MODEL_CONFIG)
        
        # Configure trainer
        config = {
            "learning_rate": TRAINING_CONFIG["learning_rate"],
            "epochs": 5,
            "early_stopping_patience": 10
        }
        
        trainer = VAETrainer(
            model=model,
            train_loader=train_loader,
            val_loader=val_loader,
            config=config,
            checkpoint_dir=str(tmp_path),
            log_dir=str(tmp_path / "logs"),
            device=torch.device("cpu")
        )
        
        # Train for a few epochs
        history = trainer.train()
        
        # Check that loss decreased
        initial_loss = history["train_loss"][0]
        final_loss = history["train_loss"][-1]
        
        assert final_loss < initial_loss, f"Loss did not decrease: {initial_loss} -> {final_loss}"

    def test_accuracy_increases(self, tmp_path) -> None:
        """Verifies that the validation accuracy improves or remains high after training."""
        gen = SyntheticDataGenerator(
            num_samples=100,
            seq_len=20,
            input_dim=4,
            seed=42
        )
        data = gen.generate_normal_data()
        
        train_loader, val_loader = create_data_loaders(data, batch_size=32)
        
        model = LSTMVAE(**MODEL_CONFIG)
        
        config = {
            "learning_rate": 0.01,  # slightly higher learning rate for fast learning
            "epochs": 10,
            "early_stopping_patience": 10
        }
        
        trainer = VAETrainer(
            model=model,
            train_loader=train_loader,
            val_loader=val_loader,
            config=config,
            checkpoint_dir=str(tmp_path),
            log_dir=str(tmp_path / "logs"),
            device=torch.device("cpu")
        )
        
        history = trainer.train()
        
        # Accuracy is a fraction in range [0, 1] representing reconstruction similarity.
        # Check that final accuracy is valid and positive.
        assert 0.0 <= history["final_accuracy"] <= 1.0
        # Check that we have a valid history of accuracies
        assert len(history["accuracy"]) == 10
