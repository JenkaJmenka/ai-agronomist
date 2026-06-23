import torch
from torch.utils.data import DataLoader
import numpy as np
from src.data.synthetic_generator import GreenhouseDataset, AnomalyConfig, AnomalyType

def test_dataset_output_shapes():
    """Test output tensor shapes for GreenhouseDataset."""
    num_samples = 5
    seq_len = 20
    input_dim = 4
    dataset = GreenhouseDataset(num_samples=num_samples, seq_len=seq_len, input_dim=input_dim)

    assert len(dataset) == num_samples
    sample = dataset[0]
    assert sample.shape == (seq_len, input_dim)

def test_anomaly_injection():
    """Test anomaly injection (SPIKE, DROPOUT)."""
    num_samples = 20
    seq_len = 20
    input_dim = 4

    anomalies = [
        AnomalyConfig(type=AnomalyType.SPIKE,
                      channel_idx=0, start_idx=5, end_idx=7, magnitude=100.0),
        AnomalyConfig(type=AnomalyType.DROPOUT,
                      channel_idx=2, start_idx=10, end_idx=12, magnitude=0.0)
    ]

    dataset = GreenhouseDataset(num_samples=num_samples, seq_len=seq_len, anomalies=anomalies, seed=42)

    # Check all samples
    data = torch.stack([dataset[i] for i in range(num_samples)])

    # Check Spike on channel 0 (spike is additive)
    assert torch.any(data[:, :, 0] > 50.0)

    # Check Dropout on channel 2 (dropout sets to 0.0)
    assert torch.any(data[:, :, 2] == 0.0)

def test_reproducibility_via_seed():
    """Test reproducibility via seed."""
    num_samples = 5
    seq_len = 20
    input_dim = 4

    dataset1 = GreenhouseDataset(num_samples=num_samples, seq_len=seq_len, seed=42)
    dataset2 = GreenhouseDataset(num_samples=num_samples, seq_len=seq_len, seed=42)

    for i in range(num_samples):
        assert torch.allclose(dataset1[i], dataset2[i])

def test_dataloader_compatibility():
    """Test DataLoader compatibility."""
    num_samples = 10
    seq_len = 20
    input_dim = 4
    dataset = GreenhouseDataset(num_samples=num_samples, seq_len=seq_len, input_dim=input_dim)
    loader = DataLoader(dataset, batch_size=2, shuffle=True)

    batch = next(iter(loader))
    assert batch.shape == (2, seq_len, input_dim)
