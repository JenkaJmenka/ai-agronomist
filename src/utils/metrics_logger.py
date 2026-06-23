import json
import platform
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import torch
from torch.utils.tensorboard import SummaryWriter

class MetricsLogger:
    """
    Centralized metrics tracking for training processes.
    Supports TensorBoard logging, weight distribution histograms, 
    and JSON-based training summary generation.
    
    Designed to be thread-safe for multi-tenant or parallel training scenarios.
    """

    def __init__(self, log_dir: str):
        """
        Initialize the MetricsLogger.

        Args:
            log_dir (str): Directory where TensorBoard logs and JSON summaries will be stored.
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.writer = SummaryWriter(log_dir=str(self.log_dir))
        self._lock = threading.Lock()
        
        # Internal tracking for the final JSON summary
        self._history: Dict[str, list] = {}
        self._start_time = datetime.now()
        self._closed = False

    def __enter__(self) -> "MetricsLogger":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit, ensures writer is closed."""
        self.close()

    def log_scalar(self, tag: str, value: float, step: int) -> None:
        """
        Logs a scalar value (e.g., loss, accuracy) to TensorBoard and internal history.

        Args:
            tag (str): The metric name (e.g., 'train_loss', 'val_accuracy').
            value (float): The scalar value to log.
            step (int): The current training step or epoch index.
        """
        if self._closed:
            raise RuntimeError("Cannot log to a closed MetricsLogger.")
        with self._lock:
            # Log to TensorBoard
            self.writer.add_scalar(tag, value, step)
            
            # Update internal history for JSON summary
            if tag not in self._history:
                self._history[tag] = []
            
            self._history[tag].append({
                "step": step,
                "value": float(value),
                "timestamp": datetime.now().isoformat()
            })

    def log_histogram(self, tag: str, values: torch.Tensor) -> None:
        """
        Logs a histogram of weight distributions or gradients to TensorBoard.

        Args:
            tag (str): The name of the parameter/layer (e.g., 'layer1.weight').
            values (torch.Tensor): The tensor containing values to bin.
        """
        with self._lock:
            # TensorBoard handles the histogram computation
            self.writer.add_histogram(tag, values, 0)

    def log_training_summary(self, summary_data: Dict[str, Any], filename: Optional[str] = None) -> None:
        """
        Saves the final training summary to a JSON file, including the 
        full history of logged scalars and metadata.

        Args:
            summary_data (Dict[str, Any]): Dictionary containing final epoch metrics 
                                          or custom training statistics.
            filename (Optional[str]): Custom filename. Defaults to 'training_summary.json'.
        """
        if filename is None:
            filename = "training_summary.json"
            
        summary_path = self.log_dir / filename
        
        with self._lock:
            end_time = datetime.now()
            duration = (end_time - self._start_time).total_seconds()
            
            payload = {
                "metadata": {
                    "timestamp": end_time.isoformat(),
                    "duration_seconds": duration,
                    "log_dir": str(self.log_dir),
                    "python_version": platform.python_version(),
                },
                "training_history": self._history,
                "final_metrics": summary_data
            }
            
            with open(summary_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=4)

    def close(self) -> None:
        """
        Flushes and closes the TensorBoard writer.
        """
        with self._lock:
            if self.writer:
                self.writer.flush()
                self.writer.close()
                self._closed = True