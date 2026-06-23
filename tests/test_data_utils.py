import numpy as np
import pandas as pd
import torch
import pytest
import os
from src.models.data_utils import (
    load_greenhouse_data,
    preprocess_sequences,
    create_data_loaders,
    denormalize_data,
)
from src.config import MODEL_CONFIG, DATA_CONFIG

@pytest.fixture
def sample_csv(tmp_path):
    """Creates a temporary CSV file with sensor data."""
    csv_path = tmp_path / "test_sensor_data.csv"
    # Ensure we have enough rows for a sequence
    rows = 50
    data = {
        "temp": np.random.uniform(20, 30, rows),
        "humidity": np.random.uniform(50, 80, rows),
        "co2": np.random.uniform(300, 500, rows),
        "light": np.random.uniform(0, 1000, rows),
    }
    df = pd.DataFrame(data)
    df.to_csv(csv_path, index=False)
    return str(csv_path)

@pytest.fixture
def raw_data_sequence():
    """Provides a dummy sequence of shape (100, 4)."""
    return np.random.rand(100, 4).astype(np.float32)

def test_load_greenhouse_data_from_csv(sample_csv):
    """Tests loading data from an existing CSV file."""
    normalized_data, params = load_greenhouse_data(sample_csv)

    assert normalized_data.shape[1] == MODEL_CONFIG["input_dim"]
    assert "min" in params
    assert "max" in params
    assert params["min"].shape == (MODEL_CONFIG["input_dim"],)

    # Check if normalization is within [0, 1]
    assert np.all(normalized_data >= 0.0)
    assert np.all(normalized_data <= 1.0)

def test_load_greenhouse_data_synthetic():
    """Tests loading data when the file does not exist (synthetic fallback)."""
    non_existent_path = "non_existent_file_12345.csv"
    normalized_data, params = load_greenhouse_data(non_existent_path)

    assert normalized_data.shape[1] == MODEL_CONFIG["input_dim"]
    assert "min" in params
    assert "max" in params
    assert np.all(normalized_data >= 0.0)
    assert np.all(normalized_data <= 1.0)

def test_preprocess_sequences_valid(raw_data_sequence):
    """Tests preprocessing with valid input."""
    seq_len = 20
    sequences = preprocess_sequences(raw_data_sequence, seq_len=seq_len)

    # 100 - 20 + 1 = 81
    expected_num_seq = raw_data_sequence.shape[0] - seq_len + 1
    assert sequences.shape == (expected_num_seq, seq_len, MODEL_CONFIG["input_dim"])

def test_preprocess_sequences_already_windowed():
    """Tests that if data is already (N, seq_len, input_dim), it returns as is."""
    windowed_data = np.random.rand(10, 20, 4).astype(np.float32)
    sequences = preprocess_sequences(windowed_data)
    assert sequences.shape == (10, 20, 4)

def test_preprocess_sequences_invalid_length(raw_data_sequence):
    """Tests error handling for data length shorter than seq_len."""
    short_data = np.random.rand(10, 4).astype(np.float32)
    seq_len = 20
    with pytest.raises(ValueError, match=f"Data length 10 is less than seq_len {seq_len}"):
        preprocess_sequences(short_data, seq_len=seq_len)

def test_create_data_loaders(raw_data_sequence):
    """Tests creation of PyTorch DataLoaders."""
    seq_len = 20
    sequences = preprocess_sequences(raw_data_sequence, seq_len=seq_len)

    batch_size = 8
    train_split = 0.7
    train_loader, val_loader = create_data_loaders(sequences, batch_size=batch_size, train_split=train_split)

    # Check if we can iterate and get tensors
    for batch in train_loader:
        # batch is a list/tuple of tensors from TensorDataset
        assert isinstance(batch[0], torch.Tensor)
        assert batch[0].shape[1:] == (seq_len, 4)
        break

    for batch in val_loader:
        assert isinstance(batch[0], torch.Tensor)
        assert batch[0].shape[1:] == (seq_len, 4)
        break

def test_denormalize_data_roundtrip(sample_csv):
    """Tests if denormalization reverses the normalization performed in load_greenhouse_data."""
    # 1. Load and get normalized data + params
    normalized_data, params = load_greenhouse_data(sample_csv)

    # 2. Denormalize
    denormalized_data = denormalize_data(normalized_data, params)

    # 3. Check if it matches the original data from CSV
    original_data = pd.read_csv(sample_csv).to_numpy(dtype=np.float32)

    # The denormalized data should match the original data
    assert denormalized_data.shape == original_data.shape
    np.testing.assert_allclose(denormalized_data, original_data, rtol=1e-5)
