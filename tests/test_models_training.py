import torch
import pytest
from unittest.mock import MagicMock, patch
from torch.utils.data import DataLoader, TensorDataset
from src.models.training import VAETrainer
from src.models.lstm_vae import LSTMVAE

@pytest.fixture
def training_setup():
    """Fixture to set up a minimal training environment."""
    batch_size = 4
    seq_len = 10
    input_dim = 4
    device = torch.device("cpu")

    # Create dummy model
    model = LSTMVAE(input_dim=input_dim, hidden_dims=[16], latent_dim=8, seq_len=seq_len).to(device)

    # Create dummy datasets
    train_data = torch.randn(batch_size * 2, seq_len, input_dim)
    val_data = torch.randn(batch_size, seq_len, input_dim)

    train_dataset = TensorDataset(train_data)
    val_dataset = TensorDataset(val_data)

    train_loader = DataLoader(train_dataset, batch_size=batch_size)
    val_loader = DataLoader(val_dataset, batch_size=batch_size)

    config = {
        "learning_rate": 0.001,
        "weight_decay": 0.01,
        "early_stopping_patience": 2,
        "epochs": 5
    }

    checkpoint_dir = "/tmp/checkpoints"
    log_dir = "/tmp/logs"

    trainer = VAETrainer(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        config=config,
        checkpoint_dir=checkpoint_dir,
        log_dir=log_dir,
        device=device
    )

    return trainer, train_loader, val_loader

def test_trainer_train_epoch(training_setup):
    """Test that one epoch of training runs without error and returns a loss."""
    trainer, train_loader, _ = training_setup
    loss = trainer.train_epoch(epoch=1)

    assert isinstance(loss, float)
    assert loss > 0

def test_trainer_validate(training_setup):
    """Test that validation runs without error and returns a loss."""
    trainer, _, val_loader = training_setup
    loss = trainer.validate(epoch=1)

    assert isinstance(loss, float)
    assert loss > 0

def test_trainer_calculate_accuracy(training_setup):
    """Test accuracy calculation."""
    trainer, _, _ = training_setup
    # Accuracy should be between 0 and 1
    acc = trainer.calculate_accuracy(threshold=0.5)
    assert 0.0 <= acc <= 1.0

@patch("torch.optim.Adam.step")
@patch("torch.nn.utils.clip_grad_norm_")
def test_trainer_train_loop_early_stopping(mock_clip, mock_step, training_setup):
    """Test that the training loop respects early stopping."""
    trainer, _, _ = training_setup
    # Force a high patience to ensure it doesn't stop immediately
    trainer.patience = 1

    # Mock validate to return increasing loss (which should trigger early stopping)
    # We'll use a side_effect to return different values each time
    with patch.object(trainer, 'validate', side_effect=[0.5, 0.6, 0.7]):
        with patch.object(trainer, 'train_epoch', return_value=0.4):
            history = trainer.train()

            # Check that it stopped early (it should stop after epoch 2 if patience is 1)
            # Epoch 1: val_loss 0.5 (best)
            # Epoch 2: val_loss 0.6 (patience counter becomes 1)
            # Since patience is 1, it should stop after epoch 2
            assert len(history["epochs"]) <= 2
            assert history["best_epoch"] == 1
