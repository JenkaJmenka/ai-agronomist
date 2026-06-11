"""Shared pytest fixtures for model and pipeline testing."""

import os

# Limit OpenMP threads to avoid pytest shutdown deadlock from leaked torch threads.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

# Ensure test DB is used globally before any app imports
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_global.db"

import pytest
import torch
from src.models.lstm_vae import LSTMVAE
from src.config import MODEL_CONFIG


@pytest.fixture
def device() -> str:
    """Returns the primary target device for model testing.
    
    Using CPU ensures consistent results across different CI environments.
    """
    return "cpu"


@pytest.fixture
def model(device: str) -> LSTMVAE:
    """Returns a newly initialized instance of LSTMVAE mapped to the test device.
    
    This fixture ensures clean, isolated state for every test case.
    """
    test_model = LSTMVAE(**MODEL_CONFIG)
    return test_model.to(device)


@pytest.fixture
def sample_input() -> torch.Tensor:
    """Returns a random synthetic tensor representing a batch of greenhouse readings.
    
    Shape: (batch_size=8, seq_len, input_dim).
    """
    batch_size = 8
    seq_len = MODEL_CONFIG["seq_len"]
    input_dim = MODEL_CONFIG["input_dim"]
    return torch.randn(batch_size, seq_len, input_dim)
