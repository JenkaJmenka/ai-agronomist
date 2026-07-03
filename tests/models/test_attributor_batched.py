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

INPUT_DIM = MODEL_CONFIG["input_dim"]


class _IdentityModel(torch.nn.Module):
    """Stub whose reconstruction is perfect: error is always exactly 0.

    With zero reconstruction error every masked variant also scores 0, so all
    raw contributions collapse to 0 and the uniform fallback fires --- used to
    exercise the near-zero-error fallback branch through the batched core.
    """

    def encode(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        return x, torch.zeros_like(x)

    def decode(self, mu: torch.Tensor) -> torch.Tensor:
        return mu


@pytest.fixture
def model() -> LSTMVAE:
    torch.manual_seed(42)
    return LSTMVAE(**MODEL_CONFIG)


@pytest.fixture
def attributor(model: LSTMVAE) -> AnomalyAttributor:
    attributor = AnomalyAttributor(model, device="cpu")
    rng = np.random.default_rng(42)
    baseline = rng.random((10, SEQ_LEN, INPUT_DIM))
    attributor.set_baseline(baseline)
    return attributor


@pytest.fixture
def fallback_attributor() -> AnomalyAttributor:
    """Attributor whose model reconstructs perfectly (all errors zero)."""
    attributor = AnomalyAttributor(_IdentityModel(), device="cpu")
    rng = np.random.default_rng(3)
    attributor.set_baseline(rng.random((5, SEQ_LEN, INPUT_DIM)))
    return attributor


@pytest.fixture
def anomalous_sequence() -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.random((SEQ_LEN, INPUT_DIM))


def _assert_result_equivalent(batch_result: dict, loop_result: dict) -> None:
    """Assert two per-sequence attribution dicts are numerically equivalent."""
    assert batch_result["is_fallback"] == loop_result["is_fallback"]
    assert batch_result["affected_sensors"] == loop_result["affected_sensors"]
    assert list(batch_result["attribution"].keys()) == list(
        loop_result["attribution"].keys()
    )
    assert np.allclose(
        batch_result["anomaly_error"], loop_result["anomaly_error"], atol=1e-6
    )
    assert np.allclose(
        batch_result["confidence"], loop_result["confidence"], atol=1e-6
    )
    assert np.allclose(
        list(batch_result["attribution"].values()),
        list(loop_result["attribution"].values()),
        atol=1e-6,
    )
    assert np.allclose(
        batch_result["sensor_contributions"],
        loop_result["sensor_contributions"],
        atol=1e-6,
    )


def _assert_batch_matches_loop(
    attributor: AnomalyAttributor, sequences: np.ndarray
) -> None:
    """attribute_batch(sequences) must match a per-sequence attribute_sequence loop."""
    batch_results = attributor.attribute_batch(sequences)
    loop_results = [attributor.attribute_sequence(seq) for seq in sequences]
    assert len(batch_results) == len(loop_results) == sequences.shape[0]
    for batch_result, loop_result in zip(batch_results, loop_results):
        _assert_result_equivalent(batch_result, loop_result)


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


def test_anomaly_error_precomputed_skips_unmasked_row(attributor: AnomalyAttributor, anomalous_sequence: np.ndarray):
    """
    Batched attribution needs only a single forward pass either way: without a
    precomputed anomaly_error the batch includes one extra unmasked row (whose
    error IS the anomaly_error); with it that row is skipped. Both fit in one
    micro-batch, hence exactly one encode call.
    """
    # Test case 1: anomaly_error is NOT passed -> 1 encode call (masked + unmasked rows batched)
    original_encode = attributor.model.encode
    mock_encode_1 = MagicMock(side_effect=original_encode)
    attributor.model.encode = mock_encode_1

    _ = attributor.attribute_sequence(anomalous_sequence)
    assert mock_encode_1.call_count == 1

    # Reset model encode
    attributor.model.encode = original_encode

    # Test case 2: anomaly_error IS passed -> 1 encode call, unmasked row omitted
    mock_encode_2 = MagicMock(side_effect=original_encode)
    attributor.model.encode = mock_encode_2

    preset_error = 0.45
    result = attributor.attribute_sequence(anomalous_sequence, anomaly_error=preset_error)

    assert result["anomaly_error"] == preset_error
    assert mock_encode_2.call_count == 1


@pytest.mark.parametrize("batch_size", [1, 3, 17])
def test_attribute_batch_equivalent_to_per_sequence_loop(
    attributor: AnomalyAttributor, batch_size: int
) -> None:
    """attribute_batch must equal a per-sequence attribute_sequence loop."""
    rng = np.random.default_rng(batch_size)
    sequences = rng.random((batch_size, SEQ_LEN, INPUT_DIM))
    _assert_batch_matches_loop(attributor, sequences)


@pytest.mark.parametrize("batch_size", [1, 3, 17])
def test_attribute_batch_equivalent_on_fallback(
    fallback_attributor: AnomalyAttributor, batch_size: int
) -> None:
    """Near-zero-error sequences hit the uniform fallback identically in both paths."""
    rng = np.random.default_rng(100 + batch_size)
    sequences = rng.random((batch_size, SEQ_LEN, INPUT_DIM))
    batch_results = fallback_attributor.attribute_batch(sequences)
    assert all(result["is_fallback"] for result in batch_results)
    assert all(result["confidence"] == 0.0 for result in batch_results)
    _assert_batch_matches_loop(fallback_attributor, sequences)


def test_micro_batch_chunking_matches_single_chunk(
    attributor: AnomalyAttributor, monkeypatch
) -> None:
    """Chunking the forward pass by ATTRIBUTION_MAX_BATCH must not change results."""
    rng = np.random.default_rng(123)
    sequences = rng.random((17, SEQ_LEN, INPUT_DIM))
    # Reference: single chunk (default cap far exceeds 17 * (INPUT_DIM + 1) rows).
    reference = attributor.attribute_batch(sequences)
    # Force many small chunks so the concat/reshape path is exercised.
    monkeypatch.setattr("src.models.anomaly_attributor.ATTRIBUTION_MAX_BATCH", 3)
    chunked = attributor.attribute_batch(sequences)
    for reference_result, chunked_result in zip(reference, chunked):
        _assert_result_equivalent(chunked_result, reference_result)
