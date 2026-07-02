"""
Tests for AnomalyAttributor batched logic and anomaly_error caching.
"""

from typing import Tuple
from unittest.mock import MagicMock
import numpy as np
import pytest
import torch

from src.config import (
    MODEL_CONFIG,
    SEQ_LEN,
)
from src.models.anomaly_attributor import AnomalyAttributor
from src.models.lstm_vae import LSTMVAE


@pytest.fixture
def model() -> LSTMVAE:
    torch.manual_seed(42)
    return LSTMVAE(**MODEL_CONFIG)


@pytest.fixture
def attributor(model: LSTMVAE) -> AnomalyAttributor:
    attributor = AnomalyAttributor(model, device="cpu")
    rng = np.random.default_rng(42)
    baseline = rng.random((10, SEQ_LEN, MODEL_CONFIG["input_dim"]))
    attributor.set_baseline(baseline)
    return attributor


@pytest.fixture
def anomalous_sequence() -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.random((SEQ_LEN, MODEL_CONFIG["input_dim"]))


def test_mathematical_equivalence(attributor: AnomalyAttributor, anomalous_sequence: np.ndarray):
    """
    Assert that the new batched implementation yields mathematically identical
    results to the original sequential implementation.
    """
    # 1. Compute sequential attribution manually inside the test (original logic)
    sequence_validated = attributor._validate_sequence(anomalous_sequence)
    anomaly_error = attributor._reconstruction_error(attributor._to_batch_tensor(sequence_validated))

    import torch.nn.functional as F
    raw_contributions_seq = []
    for sensor_idx in range(sequence_validated.shape[1]):
        masked_sequence = sequence_validated.copy()
        masked_sequence[:, sensor_idx] = attributor.baseline_sequences[:, :, sensor_idx].mean()
        x = attributor._to_batch_tensor(masked_sequence)
        
        if attributor.model.training:
            attributor.model.eval()
        with torch.no_grad():
            mu, _ = attributor.model.encode(x)
            reconstruction = attributor.model.decode(mu)
            masked_error = float(F.mse_loss(reconstruction, x).item())
        
        raw_contributions_seq.append(max(0.0, anomaly_error - masked_error))

    attribution_seq, is_fallback_seq = attributor._normalize_contributions(raw_contributions_seq)

    # 2. Compute via the optimized attribute_sequence (batched)
    result_batched = attributor.attribute_sequence(anomalous_sequence)

    # 3. Assert equivalence
    assert np.allclose(result_batched["anomaly_error"], anomaly_error)
    assert result_batched["is_fallback"] == is_fallback_seq
    for k in attribution_seq:
        assert np.allclose(result_batched["attribution"][k], attribution_seq[k], atol=1e-6)
    for c_seq, c_batch in zip(raw_contributions_seq, result_batched["sensor_contributions"]):
        assert np.allclose(c_seq, c_batch, atol=1e-6)


def test_anomaly_error_precomputed_skips_forward_pass(attributor: AnomalyAttributor, anomalous_sequence: np.ndarray):
    """
    Assert that when anomaly_error is passed to attribute_sequence,
    only 1 forward pass is executed (for the batched masked sequences),
    whereas 2 forward passes are executed when anomaly_error is not provided.
    """
    # Test case 1: anomaly_error is NOT passed -> 2 encode calls (1 unmasked, 1 batched)
    original_encode = attributor.model.encode
    mock_encode_1 = MagicMock(side_effect=original_encode)
    attributor.model.encode = mock_encode_1

    _ = attributor.attribute_sequence(anomalous_sequence)
    assert mock_encode_1.call_count == 2

    # Reset model encode
    attributor.model.encode = original_encode

    # Test case 2: anomaly_error IS passed -> 1 encode call (only the batched masked sequences)
    mock_encode_2 = MagicMock(side_effect=original_encode)
    attributor.model.encode = mock_encode_2

    preset_error = 0.45
    result = attributor.attribute_sequence(anomalous_sequence, anomaly_error=preset_error)
    
    assert result["anomaly_error"] == preset_error
    assert mock_encode_2.call_count == 1
