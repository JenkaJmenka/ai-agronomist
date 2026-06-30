"""Unit tests for time-series data utilities."""

import numpy as np
import pytest
import torch
from torch.utils.data import DataLoader
from src.models.data_utils import create_data_loaders, sanitize_training_data


class TestDataUtils:
    """Verifies PyTorch DataLoader configuration and shape constraints."""

    def test_create_data_loaders(self) -> None:
        """Verifies DataLoader partitioning matches the split parameter."""
        # Create random input sequence data (100 sequences of length 20, 4 features)
        data = np.random.rand(100, 20, 4)
        
        train_loader, val_loader = create_data_loaders(
            data,
            batch_size=32,
            train_split=0.8
        )
        
        assert isinstance(train_loader, DataLoader)
        assert isinstance(val_loader, DataLoader)
        
        # Verify dataset splits
        assert len(train_loader.dataset) == 80
        assert len(val_loader.dataset) == 20

    def test_loader_batch_shape(self) -> None:
        """Verifies that DataLoader yields batches of correct shapes."""
        data = np.random.rand(100, 20, 4)
        train_loader, _ = create_data_loaders(data, batch_size=32)
        
        # Dataloader yields a tuple (tensor,) because it wraps a TensorDataset
        batch_tuple = next(iter(train_loader))
        assert isinstance(batch_tuple, (list, tuple))
        assert len(batch_tuple) == 1
        
        batch_tensor = batch_tuple[0]
        assert batch_tensor.shape[0] <= 32
        assert batch_tensor.shape[1] == 20
        assert batch_tensor.shape[2] == 4

    def test_loader_outputs_tensors(self) -> None:
        """Verifies that DataLoader yields standard PyTorch FloatTensors."""
        data = np.random.rand(100, 20, 4)
        train_loader, _ = create_data_loaders(data, batch_size=32)
        
        batch_tuple = next(iter(train_loader))
        batch_tensor = batch_tuple[0]
        
        assert isinstance(batch_tensor, torch.Tensor)
        assert batch_tensor.dtype == torch.float32

    def test_sanitize_training_data(self) -> None:
        """Test that robust data sanitization drops the most anomalous rows."""
        # Create normal data (500 rows)
        normal_data = np.random.normal(loc=20.0, scale=1.0, size=(500, 4))
        
        # Create anomalies (20 rows)
        anomalies = np.random.normal(loc=100.0, scale=10.0, size=(20, 4))
        
        # Combine
        combined = np.vstack([normal_data, anomalies])
        
        # Sanitize with 5% contamination
        sanitized = sanitize_training_data(combined, contamination=0.05)
        
        # The length should drop approximately by 5%
        # combined length is 520, 5% of 520 is 26.
        assert sanitized.shape[0] < combined.shape[0]
        assert sanitized.shape[0] >= 490
        
        # Ensure the massive anomalies (mean near 100) are gone
        # Max value should be much lower than 100
        assert sanitized.max() < 50.0
