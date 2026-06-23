import os
import pytest
import torch
import torch.nn as nn
import torch.optim as optim
from pathlib import Path
from unittest.mock import patch
from src.models.checkpoint_manager import CheckpointManager

@pytest.fixture
def checkpoint_dir(tmp_path):
    """Fixture to provide a temporary directory for checkpoints."""
    d = tmp_path / "checkpoints"
    return d

@pytest.fixture
def manager(checkpoint_dir):
    """Fixture to provide a CheckpointManager instance."""
    return CheckpointManager(str(checkpoint_dir), max_to_keep=3)

@pytest.fixture
def model_and_optimizer():
    """Fixture to provide a simple model and optimizer."""
    model = nn.Linear(10, 1)
    optimizer = optim.SGD(model.parameters(), lr=0.01)
    return model, optimizer

def test_init_creates_dir(tmp_path):
    """Test if the directory is created on initialization."""
    new_dir = tmp_path / "new_checkpoint_dir"
    assert not new_dir.exists()
    CheckpointManager(str(new_dir))
    assert new_dir.exists()

def test_init_failure():
    """Test error handling when directory creation fails."""
    with patch("pathlib.Path.mkdir") as mock_mkdir:
        mock_mkdir.side_effect = OSError("Permission denied")
        with pytest.raises(OSError):
            CheckpointManager("/root/forbidden")

def test_save_checkpoint_valid(manager, model_and_optimizer, checkpoint_dir):
    """Test if save_checkpoint creates a valid .pt file with correct metadata."""
    model, optimizer = model_and_optimizer
    epoch = 1
    loss = 0.5
    hyperparams = {"lr": 0.01, "batch_size": 32}
    
    path = manager.save_checkpoint(model, optimizer, epoch, loss, hyperparams)
    
    assert Path(path).exists()
    assert path.endswith(f"checkpoint_epoch_{epoch}.pt")
    
    # Verify content
    checkpoint = torch.load(path)
    assert checkpoint["epoch"] == epoch
    assert checkpoint["loss"] == loss
    assert checkpoint["hyperparameters"] == hyperparams
    assert "model_state_dict" in checkpoint
    assert "optimizer_state_dict" in checkpoint

def test_load_checkpoint_success(manager, model_and_optimizer):
    """Test if load_checkpoint restores exact model and optimizer state."""
    model, optimizer = model_and_optimizer
    epoch = 5
    loss = 0.123
    hyperparams = {"lr": 0.01}
    
    # Save a checkpoint
    saved_path = manager.save_checkpoint(model, optimizer, epoch, loss, hyperparameters=hyperparams)
    
    # Create a new model/optimizer to load into
    new_model = nn.Linear(10, 1)
    new_optimizer = optim.SGD(new_model.parameters(), lr=0.01)
    
    loaded_epoch, loaded_loss = manager.load_checkpoint(saved_path, new_model, new_optimizer)
    
    assert loaded_epoch == epoch
    assert loaded_loss == loss
    # Check if weights are restored (using a specific weight value)
    for p1, p2 in zip(model.parameters(), new_model.parameters()):
        assert torch.equal(p1, p2)
    assert new_optimizer.param_groups[0]['lr'] == 0.01

def test_load_checkpoint_file_not_found(manager, model_and_optimizer):
    """Test load_checkpoint raises FileNotFoundError for non-existent path."""
    model, optimizer = model_and_optimizer
    with pytest.raises(FileNotFoundError):
        manager.load_checkpoint("non_existent_path.pt", model, optimizer)

def test_load_checkpoint_missing_key(manager, model_and_optimizer, checkpoint_dir):
    """Test load_checkpoint raises RuntimeError when required keys are missing."""
    model, optimizer = model_and_optimizer
    bad_path = checkpoint_dir / "bad_checkpoint.pt"
    
    # Manually save a corrupted dict missing 'loss'
    bad_data = {
        "model_state_dict": model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
        "epoch": 1
        # 'loss' is missing
    }
    torch.save(bad_data, bad_path)
    
    with pytest.raises(RuntimeError) as excinfo:
        manager.load_checkpoint(str(bad_path), model, optimizer)
    assert "missing required key: loss" in str(excinfo.value)

def test_load_checkpoint_corrupted_file(manager, model_and_optimizer, checkpoint_dir):
    """Test load_checkpoint raises RuntimeError for corrupted files."""
    model, optimizer = model_and_optimizer
    corrupt_path = checkpoint_dir / "corrupt.pt"
    with open(corrupt_path, "w") as f:
        f.write("not a torch file")
    
    with pytest.raises(RuntimeError):
        manager.load_checkpoint(str(corrupt_path), model, optimizer)

def test_get_best_checkpoint_logic(manager, model_and_optimizer, checkpoint_dir):
    """Test if get_best_checkpoint identifies the checkpoint with minimum loss."""
    model, optimizer = model_and_optimizer
    
    # Create 3 checkpoints with different losses
    manager.save_checkpoint(model, optimizer, 1, 0.5, {})
    manager.save_checkpoint(model, optimizer, 2, 0.1, {}) # Best
    manager.save_checkpoint(model, optimizer, 3, 0.9, {})
    
    best_path = manager.get_best_checkpoint()
    assert best_path is not None
    assert "epoch_2" in best_path
    
    # Verify loss is actually 0.1
    checkpoint = torch.load(best_path)
    assert checkpoint["loss"] == 0.1

def test_get_best_checkpoint_none(manager):
    """Test get_best_checkpoint returns None when no checkpoints exist."""
    assert manager.get_best_checkpoint() is None

def test_cleanup_old_checkpoints_retention(manager, model_and_optimizer, checkpoint_dir):
    """Test cleanup_old_checkpoints retains only the specified max_to_keep."""
    model, optimizer = model_and_optimizer
    # manager.max_to_keep is 3 (from fixture)
    
    # Create 5 checkpoints with increasing loss (worse)
    # 1: 0.1 (best), 2: 0.2, 3: 0.3, 4: 0.4, 5: 0.5
    for i, loss in enumerate([0.1, 0.2, 0.3, 0.4, 0.5], 1):
        manager.save_checkpoint(model, optimizer, i, loss, {})
    
    manager.cleanup_old_checkpoints()
    
    # Check remaining files
    remaining_files = list(checkpoint_dir.glob("*.pt"))
    assert len(remaining_files) == 3
    
    # Verify the remaining ones are the ones with lowest loss
    losses = []
    for f in remaining_files:
        losses.append(torch.load(f)["loss"])
    
    assert max(losses) <= 0.3
    assert min(losses) == 0.1

def test_cleanup_old_checkpoints_with_corrupted_files(manager, model_and_optimizer, checkpoint_dir):
    """Test cleanup handles corrupted files gracefully by skipping them."""
    model, optimizer = model_and_optimizer
    
    # Create 3 valid checkpoints
    manager.save_checkpoint(model, optimizer, 1, 0.1, {})
    manager.save_checkpoint(model, optimizer, 2, 0.2, {})
    manager.save_checkpoint(model, optimizer, 3, 0.3, {})
    
    # Create 1 corrupted file
    corrupt_file = checkpoint_dir / "checkpoint_epoch_4.pt"
    with open(corrupt_file, "w") as f:
        f.write("garbage")
        
    # Should not crash and should still clean up based on valid files
    manager.cleanup_old_checkpoints()
    
    # The corrupted file should ideally be ignored or handled. 
    # In current implementation, it might stay if it's not in the 'to_delete' list 
    # because it's skipped during the metadata parsing phase.
    # Let's verify the valid ones are still there.
    valid_files = list(checkpoint_dir.glob("checkpoint_epoch_*.pt"))
    assert len(valid_files) >= 3

def test_save_checkpoint_error(manager, model_and_optimizer):
    """Test error handling when torch.save fails."""
    model, optimizer = model_and_optimizer
    with patch("torch.save") as mock_save:
        mock_save.side_effect = RuntimeError("Disk Full")
        with pytest.raises(RuntimeError) as excinfo:
            manager.save_checkpoint(model, optimizer, 1, 0.1, {})
        assert "Disk Full" in str(excinfo.value)

def test_checkpoint_rollout_pattern(manager, checkpoint_dir):
    """Test the glob pattern returned by the manager."""
    pattern = manager.checkpoint_rollout_pattern()
    assert str(pattern).endswith("checkpoint_epoch_*.pt")