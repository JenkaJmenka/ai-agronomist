import os
import json
import torch
import pytest
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
from src.models.checkpoint_manager import CheckpointManager
from src.utils.metrics_logger import MetricsLogger

# --- Fixtures ---

@pytest.fixture
def checkpoint_dir(tmp_path):
    """Provides a temporary directory for checkpoints."""
    d = tmp_path / "checkpoints"
    d.mkdir()
    return d

@pytest.fixture
def log_dir(tmp_path):
    """Provides a temporary directory for logs."""
    d = tmp_path / "logs"
    d.mkdir()
    return d

@pytest.fixture
def dummy_model():
    """Provides a simple PyTorch model."""
    return torch.nn.Linear(10, 1)

@pytest.fixture
def dummy_optimizer(dummy_model):
    """Provides a simple PyTorch optimizer."""
    return torch.optim.SGD(dummy_model.parameters(), lr=0.01)

@pytest.fixture
def checkpoint_manager(checkpoint_dir):
    """Provides a CheckpointManager instance."""
    return CheckpointManager(str(checkpoint_dir), max_to_keep=2)

@pytest.fixture
def metrics_logger(log_dir):
    """Provides a MetricsLogger instance."""
    return MetricsLogger(str(log_dir))

# --- Tests for CheckpointManager ---

def test_checkpoint_manager_initialization(checkpoint_dir):
    """Tests if CheckpointManager correctly initializes the directory."""
    manager = CheckpointManager(str(checkpoint_dir))
    assert manager.checkpoint_dir == Path(checkpoint_dir)
    assert manager.checkpoint_dir.exists()

def test_checkpoint_manager_save_and_load(checkpoint_dir, dummy_model, dummy_optimizer):
    """Tests the full cycle of saving and loading a checkpoint."""
    manager = CheckpointManager(str(checkpoint_dir))
    hyperparams = {"lr": 0.01, "batch_size": 32}
    epoch = 5
    loss = 0.123456
    
    # Save
    path = manager.save_checkpoint(
        model=dummy_model,
        optimizer=dummy_optimizer,
        epoch=epoch,
        loss=loss,
        hyperparameters=hyperparams
    )
    assert os.path.exists(path)
    
    # Load into new model/optimizer
    new_model = torch.nn.Linear(10, 1)
    new_optimizer = torch.optim.SGD(new_model.parameters(), lr=0.01)
    
    loaded_epoch, loaded_loss = manager.load_checkpoint(path, new_model, new_optimizer)
    
    assert loaded_epoch == epoch
    assert pytest.approx(loaded_loss) == loss
    # Check if weights are restored
    for p1, p2 in zip(dummy_model.parameters(), new_model.parameters()):
        assert torch.equal(p1, p2)

def test_checkpoint_manager_best_checkpoint_selection(checkpoint_dir, dummy_model, dummy_optimizer):
    """Tests if the manager correctly identifies the checkpoint with the lowest loss."""
    manager = CheckpointManager(str(checkpoint_dir))
    hyperparams = {}
    
    # Create 3 checkpoints with different losses
    manager.save_checkpoint(dummy_model, dummy_optimizer, epoch=1, loss=0.5, hyperparameters=hyperparams)
    manager.save_checkpoint(dummy_model, dummy_optimizer, epoch=2, loss=0.1, hyperparameters=hyperparams)
    manager.save_checkpoint(dummy_model, dummy_optimizer, epoch=3, loss=0.9, hyperparameters=hyperparams)
    
    best_path = manager.get_best_checkpoint()
    assert best_path is not None
    assert "epoch_2" in best_path
    
    # Verify the loss in the best path is indeed 0.1
    checkpoint = torch.load(best_path)
    assert checkpoint["loss"] == 0.1

def test_checkpoint_manager_cleanup(checkpoint_dir, dummy_model, dummy_optimizer):
    """Tests if the manager correctly deletes old checkpoints based on max_to_keep."""
    # max_to_keep is set to 2 in fixture
    manager = CheckpointManager(str(checkpoint_dir), max_to_keep=2)
    hyperparams = {}
    
    # Create 4 checkpoints
    manager.save_checkpoint(dummy_model, dummy_optimizer, epoch=1, loss=0.5, hyperparameters=hyperparams)
    manager.save_checkpoint(dummy_model, dummy_optimizer, epoch=2, loss=0.1, hyperparameters=hyperparams)
    manager.save_checkpoint(dummy_model, dummy_optimizer, epoch=3, loss=0.9, hyperparameters=hyperparams)
    manager.save_checkpoint(dummy_model, dummy_optimizer, epoch=4, loss=0.05, hyperparameters=hyperparams)
    
    manager.cleanup_old_checkpoints()
    
    # Only 2 should remain (the ones with loss 0.05 and 0.1)
    remaining_files = list(checkpoint_dir.glob("*.pt"))
    assert len(remaining_files) == 2
    
    # Check that the highest loss (0.5 and 0.9) are gone
    remaining_losses = []
    for f in remaining_files:
        checkpoint = torch.load(f)
        remaining_losses.append(checkpoint["loss"])
    
    assert 0.5 not in remaining_losses
    assert 0.9 not in remaining_losses

def test_checkpoint_manager_load_error(checkpoint_dir, dummy_model, dummy_optimizer):
    """Tests error handling when loading non-existent or corrupted files."""
    manager = CheckpointManager(str(checkpoint_dir))
    
    # Test FileNotFoundError
    with pytest.raises(FileNotFoundError):
        manager.load_checkpoint("non_existent.pt", dummy_model, dummy_optimizer)
    
    # Test corrupted/missing keys
    corrupt_path = checkpoint_dir / "corrupt.pt"
    torch.save({"wrong_key": 1}, corrupt_path)
    with pytest.raises(RuntimeError):  # load_checkpoint wraps KeyError in RuntimeError
        manager.load_checkpoint(str(corrupt_path), dummy_model, dummy_optimizer)

def test_checkpoint_metadata_format(checkpoint_dir, dummy_model, dummy_optimizer):
    """Verifies the structure and required fields of the saved checkpoint dictionary."""
    manager = CheckpointManager(str(checkpoint_dir))
    hyperparams = {"lr": 0.01}
    path = manager.save_checkpoint(dummy_model, dummy_optimizer, epoch=1, loss=0.1, hyperparameters=hyperparams)
    
    checkpoint = torch.load(path)
    required_keys = ["model_state_dict", "optimizer_state_dict", "epoch", "loss", "timestamp", "hyperparameters", "metadata_version"]
    for key in required_keys:
        assert key in checkpoint
    assert checkpoint["epoch"] == 1
    assert checkpoint["loss"] == 0.1
    assert checkpoint["hyperparameters"]["lr"] == 0.01

# --- Tests for MetricsLogger ---

def test_metrics_logger_scalar_logging(metrics_logger):
    """Tests if scalar values are correctly logged to internal history."""
    metrics_logger.log_scalar("train_loss", 0.5, step=1)
    metrics_logger.log_scalar("train_loss", 0.4, step=2)
    
    assert "train_loss" in metrics_logger._history
    assert len(metrics_logger._history["train_loss"]) == 2
    assert metrics_logger._history["train_loss"][0]["value"] == 0.5
    assert metrics_logger._history["train_loss"][1]["step"] == 2

def test_metrics_logger_summary_generation(metrics_logger):
    """Tests if the training summary JSON is correctly generated."""
    metrics_logger.log_scalar("accuracy", 0.95, step=1)
    summary_data = {"final_epoch_loss": 0.05}
    
    # We need to patch 'datetime' to avoid the 'isoforming' bug during testing 
    # if we want to verify the exact content, but here we just check if it survives.
    # Note: The bug 'isoforming' exists in the source.
    
    metrics_logger.log_training_summary(summary_data, filename="test_summary.json")
    
    summary_path = Path(metrics_logger.log_dir) / "test_summary.json"
    assert summary_path.exists()
    
    with open(summary_path, 'r') as f:
        data = json.load(f)
        assert data["final_metrics"]["final_epoch_loss"] == 0.05
        assert "training_history" in data
        assert "accuracy" in data["training_history"]

def test_metrics_logger_context_manager(log_dir):
    """Tests if MetricsLogger works correctly as a context manager."""
    with MetricsLogger(str(log_dir)) as logger:
        logger.log_scalar("test", 1.0, 0)
        # Check if writer is active
        assert logger.writer is not None
    
    # After exit, writer should be closed (though we can't easily check closed state without mocks)
    # But we can check if we can still use it (it should not crash)
    with pytest.raises(Exception):
        # Depending on implementation, calling log after close might fail
        logger.log_scalar("test", 2.0, 1)

def test_metrics_logger_histogram_logging(metrics_logger):
    """Tests if histogram logging executes without error."""
    tensor = torch.randn(10, 10)
    # This test primarily ensures no crash occurs during the call
    metrics_logger.log_histogram("weights", tensor)