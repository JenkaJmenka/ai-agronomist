import os
import torch
import glob
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List
from pathlib import Path

from src.config import DEVICE

# Configure logging for the checkpoint manager
logger = logging.getLogger(__name__)

class CheckpointManager:
    """
    Manages saving, loading, and cleaning up PyTorch model checkpoints.
    
    Ensures training reproducibility and provides mechanisms to track the 
    best performing models based on validation loss.
    """

    def __init__(self, checkpoint_dir: str, max_to_keep: int = 5):
        """
        Initializes the CheckpointManager.

        Args:
            checkpoint_template: The directory where checkpoints will be stored.
            max_to_keep: The number of best checkpoints (by loss) to retain.
        """
        self.checkpoint_dir = Path(checkpoint_dir)
        self.max_to_keep = max_to_keep
        
        # Ensure the directory exists
        try:
            self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Checkpoint directory initialized at: {self.checkpoint_dir}")
        except Exception as e:
            logger.error(f"Failed to create checkpoint directory: {e}")
            raise

    def save_checkpoint(
        self,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        epoch: int,
        loss: float,
        hyperparameters: Dict[str, Any],
        timestamp: Optional[str] = None
    ) -> str:
        """
        Saves the current training state to a .pt file.

        Args:
            model: The PyTorch model to save.
            optimizer: The optimizer state to save.
            epoch: The current training epoch.
            loss: The validation loss (used for identifying the best model).
            hyperparameters: Dictionary of hyperparameters used for this run.
            timestamp: Optional ISO format timestamp.

        Returns:
            str: The path to the saved checkpoint.
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()

        filename = f"checkpoint_epoch_{epoch}.pt"
        checkpoint_path = self.checkpoint_dir / filename

        checkpoint_data = {
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "epoch": epoch,
            "loss": loss,
            "timestamp": timestamp,
            "hyperparameters": hyperparameters,
            "metadata_version": "1.0"
        }

        try:
            torch.save(checkpoint_data, checkpoint_path)
            logger.info(f"Checkpoint saved successfully: {checkpoint_path} (Loss: {loss:.6f})")
            return str(checkpoint_path)
        except Exception as e:
            logger.error(f"Failed to save checkpoint at epoch {epoch}: {e}")
            raise

    def load_checkpoint(
        self, 
        checkpoint_path: str, 
        model: torch.nn.Module, 
        optimizer: torch.optim.Optimizer
    ) -> Tuple[int, float]:
        """
        Loads a checkpoint and restores the model and optimizer state.

        Args:
            checkpoint_path: Path to the .pt file.
            model: The model to restore.
            optimizer: The optimizer to restore.

        Returns:
            Tuple[int, float]: The epoch and loss from the loaded checkpoint.

        Raises:
            FileNotFoundError: If the checkpoint file does not exist.
            RuntimeError: If the checkpoint file is corrupted or incompatible.
        """
        path = Path(checkpoint_path)
        if not path.exists():
            raise FileNotFoundError(f"No checkpoint found at {checkpoint_path}")

        try:
            checkpoint = torch.load(checkpoint_path, map_location=DEVICE)
            
            # Metadata validation
            required_keys = ["model_state_dict", "optimizer_state_dict", "epoch", "loss"]
            for key in required_keys:
                if key not in checkpoint:
                    raise KeyError(f"Checkpoint missing required key: {key}")

            model.load_state_dict(checkpoint["model_state_dict"])
            optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
            
            epoch = checkpoint["epoch"]
            loss = checkpoint["loss"]
            
            logger.info(f"Successfully loaded checkpoint: {checkpoint_path} (Epoch: {epoch}, Loss: {loss:.6f})")
            return epoch, loss

        except Exception as e:
            logger.error(f"Error loading checkpoint {checkpoint_path}: {e}")
            raise RuntimeError(f"Failed to restore training state: {e}")

    def get_best_checkpoint(self) -> Optional[str]:
        """
        Identifies the checkpoint file with the lowest recorded loss.

        Returns:
            Optional[str]: Path to the best checkpoint, or None if no checkpoints found.
        """
        checkpoint_files = glob.glob(str(self.checkpoint_rollout_pattern()))
        if not checkpoint_files:
            return None

        best_path = None
        min_loss = float('inf')

        for path in checkpoint_files:
            try:
                # We use weights_only=True if possible for security, 
                # but for metadata we need the dict structure.
                checkpoint = torch.load(path, map_location=DEVICE)
                current_loss = checkpoint.get("loss", float('inf'))
                
                if current_loss < min_loss:
                    min_loss = current_loss
                    best_path = path
            except Exception as e:
                logger.warning(f"Skipping corrupted checkpoint {path}: {e}")
                continue

        return best_path

    def cleanup_old_checkpoints(self) -> None:
        """
        Keeps only the top N checkpoints with the lowest loss.
        
        Deletes all other checkpoint files in the directory to save space.
        """
        checkpoint_files = glob.glob(str(self.checkpoint_rollout_pattern()))
        if not checkpoint_files:
            return

        checkpoint_metadata_list: List[Tuple[float, str]] = []

        for path in checkpoint_files:
            try:
                checkpoint = torch.load(path, map_location=DEVICE)
                loss = checkpoint.get("loss", float('inf'))
                checkpoint_metadata_list.append((loss, path))
            except Exception as e:
                logger.warning(f"Could not parse metadata for {path} during cleanup: {e}")

        # Sort by loss ascending (best first)
        checkpoint_metadata_list.sort(key=lambda x: x[0])

        # Identify files to delete (those beyond the max_to_keep limit)
        files_to_delete = checkpoint_metadata_list[self.max_to_keep:]

        for loss, path in files_to_delete:
            try:
                os.remove(path)
                logger.info(f"Cleaned up old checkpoint: {path} (Loss: {loss:.6f})")
            except OSError as e:
                logger.error(f"Failed to delete checkpoint {path}: {e}")

    def checkpoint_rollout_pattern(self) -> Path:
        """Returns the glob pattern for finding checkpoints."""
        return self.checkpoint_dir / "checkpoint_epoch_*.pt"