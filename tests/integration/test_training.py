"""Integration tests for VAETrainer."""

import math
import os
import pytest
import torch
from torch.utils.data import DataLoader, TensorDataset

from src.models.lstm_vae import LSTMVAE
from src.models.training import VAETrainer

# ---- tiny data parameters ----
NUM_SAMPLES = 20
SEQ_LEN = 5
INPUT_DIM = 4
HIDDEN_DIMS = [8, 4]
LATENT_DIM = 2
BATCH_SIZE = 4


def _make_loader(num_samples: int, batch_size: int, seed: int = 0) -> DataLoader:
    """Return a DataLoader that yields (batch,) tuples matching trainer expectation."""
    torch.manual_seed(seed)
    data = torch.rand(num_samples, SEQ_LEN, INPUT_DIM)
    dataset = TensorDataset(data)
    return DataLoader(dataset, batch_size=batch_size, shuffle=False, drop_last=False)


def _make_trainer(
    tmp_path,
    *,
    epochs: int = 3,
    patience: int = 2,
) -> VAETrainer:
    model = LSTMVAE(
        input_dim=INPUT_DIM,
        hidden_dims=HIDDEN_DIMS,
        latent_dim=LATENT_DIM,
        seq_len=SEQ_LEN,
        beta=0.1,
        dropout=0.0,
    )
    train_loader = _make_loader(NUM_SAMPLES, BATCH_SIZE, seed=0)
    val_loader = _make_loader(NUM_SAMPLES // 4, BATCH_SIZE, seed=1)
    config = {
        "learning_rate": 1e-3,
        "weight_decay": 0.0,
        "early_stopping_patience": patience,
        "epochs": epochs,
    }
    return VAETrainer(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        config=config,
        checkpoint_dir=str(tmp_path / "checkpoints"),
        log_dir=str(tmp_path / "logs"),
        device=torch.device("cpu"),
    )


# ---------------------------------------------------------------------------
# train_epoch / validate — single-call behaviour
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_train_epoch_returns_float(tmp_path) -> None:
    trainer = _make_trainer(tmp_path)
    loss = trainer.train_epoch(epoch=1)
    assert isinstance(loss, float)


@pytest.mark.integration
def test_validate_returns_float(tmp_path) -> None:
    trainer = _make_trainer(tmp_path)
    loss = trainer.validate(epoch=1)
    assert isinstance(loss, float)


@pytest.mark.integration
def test_validate_returns_finite(tmp_path) -> None:
    trainer = _make_trainer(tmp_path)
    loss = trainer.validate(epoch=1)
    assert math.isfinite(loss)


# ---------------------------------------------------------------------------
# Full train() run
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_training_runs_3_epochs(tmp_path) -> None:
    trainer = _make_trainer(tmp_path, epochs=3, patience=5)
    history = trainer.train()
    assert len(history["train_loss"]) == 3


@pytest.mark.integration
def test_history_keys(tmp_path) -> None:
    trainer = _make_trainer(tmp_path, epochs=3, patience=5)
    history = trainer.train()
    required_keys = {
        "train_loss",
        "val_loss",
        "accuracy",
        "epochs",
        "best_epoch",
        "best_val_loss",
        "training_time",
        "final_accuracy",
    }
    assert required_keys.issubset(set(history.keys()))


@pytest.mark.integration
def test_history_lengths(tmp_path) -> None:
    epochs = 3
    trainer = _make_trainer(tmp_path, epochs=epochs, patience=10)
    history = trainer.train()
    list_keys = ["train_loss", "val_loss", "accuracy", "epochs"]
    lengths = {k: len(history[k]) for k in list_keys}
    # All list-valued keys must have the same length
    assert len(set(lengths.values())) == 1


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_checkpoint_saved(tmp_path) -> None:
    trainer = _make_trainer(tmp_path, epochs=3, patience=5)
    trainer.train()
    ckpt_dir = tmp_path / "checkpoints"
    pt_files = list(ckpt_dir.glob("*.pt"))
    assert len(pt_files) > 0


@pytest.mark.integration
def test_checkpoint_loadable(tmp_path) -> None:
    trainer = _make_trainer(tmp_path, epochs=2, patience=5)
    trainer.train()
    ckpt_dir = tmp_path / "checkpoints"
    pt_files = sorted(ckpt_dir.glob("*.pt"))
    assert len(pt_files) > 0
    loaded = trainer.model.load_checkpoint(str(pt_files[0]))
    assert "epoch" in loaded


# ---------------------------------------------------------------------------
# Early stopping
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_early_stopping(tmp_path) -> None:
    """With patience=1 and epochs=50, training must stop before epoch 50.

    We use a deliberately mismatched val dataset (seed=99) so that at some
    point the val loss stops decreasing and early stopping fires.
    """
    model = LSTMVAE(
        input_dim=INPUT_DIM,
        hidden_dims=HIDDEN_DIMS,
        latent_dim=LATENT_DIM,
        seq_len=SEQ_LEN,
        beta=0.1,
        dropout=0.0,
    )
    train_loader = _make_loader(NUM_SAMPLES, BATCH_SIZE, seed=0)
    # Val set uses a different random seed and larger noise → loss will oscillate
    torch.manual_seed(99)
    val_data = torch.randn(4, SEQ_LEN, INPUT_DIM)  # out-of-distribution noise
    val_loader = DataLoader(TensorDataset(val_data), batch_size=4, shuffle=False)
    config = {
        "learning_rate": 1e-3,
        "weight_decay": 0.0,
        "early_stopping_patience": 1,
        "epochs": 50,
    }
    trainer = VAETrainer(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        config=config,
        checkpoint_dir=str(tmp_path / "es_checkpoints"),
        log_dir=str(tmp_path / "es_logs"),
        device=torch.device("cpu"),
    )
    history = trainer.train()
    assert len(history["train_loss"]) < 50


# ---------------------------------------------------------------------------
# Accuracy
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_calculate_accuracy_range(tmp_path) -> None:
    trainer = _make_trainer(tmp_path)
    acc = trainer.calculate_accuracy()
    assert 0.0 <= acc <= 1.0
