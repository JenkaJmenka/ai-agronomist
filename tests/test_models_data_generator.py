import numpy as np
import torch
import pytest
from src.models.data_generator import SyntheticDataGenerator

def test_data_generator_shapes():
    """Test that the generated data has the correct dimensions."""
    num_samples = 5
    seq_len = 20
    input_dim = 4
    generator = SyntheticDataGenerator(
        num_samples=num_samples,
        seq_len=seq_len,
        input_dim=input_dim,
        seed=42
    )

    tensor_data, params = generator.generate()

    assert tensor_data.shape == (num_samples, seq_len, input_dim)
    assert len(params) == input_dim
    assert tensor_data.max() <= 1.0
    assert tensor_data.min() >= 0.0

def test_data_generator_reproducibility():
    """Test that the same seed produces the same data."""
    seed = 123
    gen1 = SyntheticDataGenerator(seed=seed)
    gen2 = SyntheticDataGenerator(seed=seed)

    data1, _ = gen1.generate()
    data2, _ = gen2.generate()

    assert torch.allclose(data1, data2)

def test_data_generator_bounds():
    """Test that the generated data respects the physical bounds (after normalization)."""
    generator = SyntheticDataGenerator(num_samples=2, seq_len=10, input_dim=4)
    raw_data = generator.generate_normal_data()

    # Check that raw data is within the expected config bounds
    # (This is a bit of a white-box test but ensures the logic is sound)
    from src import config

    # Temp range check
    assert np.all(raw_data[:, :, 0] >= config.TEMP_RANGE[0] - 5) # Allow some noise margin
    assert np.all(raw_data[:, :, 0] <= config.TEMP_RANGE[1] + 5)

    # Humidity range check
    assert np.all(raw_data[:, :, 1] >= config.HUMIDITY_RANGE[0] - 5)
    assert np.all(raw_data[:, :, 1] <= config.HUMIDITY_RANGE[1] + 5)

def test_data_generator_normalization():
    """Test the normalization process."""
    generator = SyntheticDataGenerator(num_samples=2, seq_len=10, input_dim=4)
    raw_data = generator.generate_normal_data()
    normalized, params = generator.normalize_data(raw_data)

    assert normalized.max() <= 1.0
    assert normalized.min() >= 0.0
    assert len(params) == 4
    assert "min" in params[0]
    assert "max" in params[0]
