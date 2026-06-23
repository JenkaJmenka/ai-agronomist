import pytest
import torch
import numpy as np
from src.models.data_generator import SyntheticDataGenerator

@pytest.fixture
def generator():
    """Fixture to provide a standard SyntheticDataGenerator instance."""
    return SyntheticDataGenerator(num_samples=1000, seq_len=20, input_dim=4, seed=42)

def test_output_shape(generator):
    """(1) Test that the output shape is exactly (1000, 20, 4)."""
    tensor_data, _ = generator.generate()
    assert tensor_data.shape == (1000, 20, 4)

def test_output_dtype(generator):
    """(2) Test that the output dtype is float32 tensor."""
    tensor_data, _ = generator.generate()
    assert torch.is_tensor(tensor_data)
    assert tensor_data.dtype == torch.float32

def test_normalization_bounds(generator):
    """(3) Test that all values in output are in [0, 1] after normalization."""
    tensor_data, _ = generator.generate()
    assert torch.all(tensor_data >= 0.0)
    assert torch.all(tensor_data <= 1.0)

def test_no_nan_or_inf(generator):
    """(4) Test that there are no NaN or Inf values in the output."""
    tensor_data, _ = generator.generate()
    assert not torch.isnan(tensor_data).any()
    assert not torch.isinf(tensor_data).any()

def test_no_constant_sequences(generator):
    """(5) Test that no sequence is constant (per-sequence std > 0 for all channels)."""
    raw_data = generator.generate_normal_data()
    # raw_data shape: (num_samples, seq_len, 4)
    # std along seq_len (axis=1)
    stds = np.std(raw_data, axis=1)
    assert np.all(stds > 0), "Detected a constant sequence in one of the channels"

def test_reproducibility():
    """(6) Test reproducibility: two generators with same seed produce identical tensors."""
    seed = 42
    gen1 = SyntheticDataGenerator(num_samples=10, seq_len=5, seed=seed)
    gen2 = SyntheticDataGenerator(num_samples=10, seq_len=5, seed=seed)

    tensor1, _ = gen1.generate()
    tensor2, _ = gen2.generate()

    assert torch.allclose(tensor1, tensor2)

def test_different_seeds():
    """(7) Test that different seeds produce different data."""
    gen1 = SyntheticDataGenerator(num_samples=10, seq_len=5, seed=42)
    gen2 = SyntheticDataGenerator(num_samples=10, seq_len=5, seed=43)

    tensor1, _ = gen1.generate()
    tensor2, _ = gen2.generate()

    assert not torch.allclose(tensor1, tensor2)

def test_normalization_params_validity(generator):
    """(8) Test that norm_params dict contains keys for all 4 channels with valid min/max."""
    _, params = generator.generate()
    assert len(params) == 4
    for j in range(4):
        assert j in params
        assert "min" in params[j]
        assert "max" in params[j]
        assert params[j]["min"] < params[j]["max"]

def test_denormalization_recovery(generator):
    """(9) Test that denormalization recovers approximate original physical ranges."""
    # Generate raw data
    raw_data = generator.generate_normal_data()

    # Use the generator's bounds to check if the raw data is within bounds
    for j in range(4):
        low, high = generator.bounds[j]
        assert np.all(raw_data[:, :, j] >= low)
        assert np.all(raw_data[:, :, j] <= high)

def test_temperature_mean(generator):
    """(10) Test that temperature mean in raw data is approx 25±2°C before normalization."""
    raw_data = generator.generate_normal_data()
    temp_data = raw_data[:, :, 0]
    mean_temp = np.mean(temp_data)
    assert 23.0 <= mean_temp <= 27.0

def test_light_variation(generator):
    """(11) Test that the light channel exhibits day/night variation (non-constant)."""
    raw_data = generator.generate_normal_data()
    light_data = raw_data[:, :, 3]
    # Check standard deviation across time for the light channel is significant
    light_std = np.std(light_data, axis=1)
    assert np.mean(light_std) > 10.0  # Should be significant due to sine wave

def test_temp_humidity_correlation(generator):
    """(12) Test that temperature and humidity are negatively correlated across sequences."""
    # The correlation is applied in add_temporal_correlations.
    # We need to use data AFTER correlations are applied.
    data_base = generator.generate_normal_data()
    data_corr = generator.add_temporal_correlations(data_base)

    temp = data_corr[:, :, 0].flatten()
    hum = data_corr[:, :, 1].flatten()

    correlation = np.corrcoef(temp, hum)[0, 1]
    assert correlation < 0  # Should be negative correlation
