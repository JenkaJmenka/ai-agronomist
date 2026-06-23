"""
Tests for the SHAP-based anomaly attributor (Week 1 Phase 1.1).

Spec: ``implementation plan/Phase 1.9.1/WEEK_1_PHASE_1_1_ANOMALY_ATTRIBUTOR.md``.
Seeds are fixed because LSTMVAE weights are random at init and attribution
quality on an untrained model is noise-sensitive. Stub models with
deterministic encode/decode are used to force specific attribution shapes
(zero contributions, single dominant sensor, NaN outputs) without training.
"""

from typing import Tuple

import numpy as np
import pytest
import torch

from src.config import (
    ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD,
    MODEL_CONFIG,
    SENSOR_NAMES,
    SEQ_LEN,
)
from src.models.anomaly_attributor import AnomalyAttributor, AttributionStrength
from src.models.lstm_vae import LSTMVAE

BASELINE_NUM_SEQUENCES = 100
BATCH_NUM_SEQUENCES = 10
ATTRIBUTION_SUM_TOLERANCE = 1e-6
NUM_SENSORS = MODEL_CONFIG["input_dim"]
UNIFORM_CONTRIBUTION = 1.0 / NUM_SENSORS


class _IdentityModel(torch.nn.Module):
    """Stub whose reconstruction is perfect: error is always exactly 0."""

    def encode(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        return x, torch.zeros_like(x)

    def decode(self, mu: torch.Tensor) -> torch.Tensor:
        return mu


class _FirstChannelErrorModel(torch.nn.Module):
    """Stub whose reconstruction error comes entirely from sensor channel 0."""

    def encode(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        return x, torch.zeros_like(x)

    def decode(self, mu: torch.Tensor) -> torch.Tensor:
        reconstruction = mu.clone()
        reconstruction[:, :, 0] = 0.0
        return reconstruction


class _NaNDecodeModel(torch.nn.Module):
    """Stub that can be switched to emit all-NaN reconstructions.

    Simulates corrupted/diverged weights (e.g. after a bad online-learning
    update) — including the case where corruption happens AFTER a healthy
    baseline was established (``should_emit_nan`` flipped at runtime).
    """

    def __init__(self) -> None:
        super().__init__()
        self.should_emit_nan = False

    def encode(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        return x, torch.zeros_like(x)

    def decode(self, mu: torch.Tensor) -> torch.Tensor:
        if self.should_emit_nan:
            return torch.full_like(mu, float("nan"))
        return mu


@pytest.fixture
def model() -> LSTMVAE:
    """Untrained LSTM-VAE with the production architecture, seeded."""
    torch.manual_seed(42)
    return LSTMVAE(**MODEL_CONFIG)


@pytest.fixture
def attributor(model: LSTMVAE) -> AnomalyAttributor:
    """Attributor with a random-normal baseline already set."""
    attributor = AnomalyAttributor(model, device="cpu")
    rng = np.random.default_rng(42)
    baseline = rng.random((BASELINE_NUM_SEQUENCES, SEQ_LEN, NUM_SENSORS))
    attributor.set_baseline(baseline)
    return attributor

@pytest.fixture
def anomalous_sequence() -> np.ndarray:
    """A single random sequence to attribute."""
    rng = np.random.default_rng(7)
    return rng.random((SEQ_LEN, NUM_SENSORS))


@pytest.fixture
def identity_attributor() -> AnomalyAttributor:
    """Attributor whose model reconstructs perfectly (all errors zero)."""
    attributor = AnomalyAttributor(_IdentityModel(), device="cpu")
    rng = np.random.default_rng(3)
    attributor.set_baseline(rng.random((5, SEQ_LEN, NUM_SENSORS)))
    return attributor


@pytest.fixture
def dominant_attributor() -> AnomalyAttributor:
    """Attributor with a zero baseline and channel-0-only error model."""
    attributor = AnomalyAttributor(_FirstChannelErrorModel(), device="cpu")
    attributor.set_baseline(np.zeros((5, SEQ_LEN, NUM_SENSORS), dtype=np.float32))
    return attributor


@pytest.fixture
def first_channel_spike_sequence() -> np.ndarray:
    """All-zero sequence with sensor channel 0 pinned to 1.0."""
    spike_sequence = np.zeros((SEQ_LEN, NUM_SENSORS), dtype=np.float32)
    spike_sequence[:, 0] = 1.0
    return spike_sequence


# ---------------------------------------------------------------- smoke tests


def test_attribution_sums_to_one(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Attribution should sum to 1.0."""
    attribution_result = attributor.attribute_sequence(anomalous_sequence)
    total = sum(attribution_result["attribution"].values())
    assert abs(total - 1.0) < ATTRIBUTION_SUM_TOLERANCE


def test_affected_sensors_threshold(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Affected sensors should each exceed the contribution threshold."""
    attribution_result = attributor.attribute_sequence(anomalous_sequence)
    for sensor_name in attribution_result["affected_sensors"]:
        contribution = attribution_result["attribution"][sensor_name]
        assert contribution > ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD


def test_confidence_in_range(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Confidence should be within [0, 1]."""
    attribution_result = attributor.attribute_sequence(anomalous_sequence)
    assert 0 <= attribution_result["confidence"] <= 1


def test_batch_attribution(attributor: AnomalyAttributor) -> None:
    """Should handle a batch of sequences."""
    rng = np.random.default_rng(11)
    batch = rng.random((BATCH_NUM_SEQUENCES, SEQ_LEN, NUM_SENSORS))
    attribution_results = attributor.attribute_batch(batch)
    assert len(attribution_results) == BATCH_NUM_SEQUENCES
    assert all(
        "attribution" in attribution_result
        for attribution_result in attribution_results
    )


def test_explanation_generated(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Should generate a natural-language explanation."""
    attribution_result = attributor.attribute_sequence(anomalous_sequence)
    explanation = attributor.get_explanation(attribution_result, anomalous_sequence)
    assert isinstance(explanation, str)
    assert len(explanation) > 10


def test_baseline_set(attributor: AnomalyAttributor) -> None:
    """Baseline error should be computed and positive."""
    assert attributor.baseline_error is not None
    assert attributor.baseline_error > 0


# -------------------------------------------------------------- construction


def test_should_default_sensor_names_to_config(model: LSTMVAE) -> None:
    """Sensor names should default to config.SENSOR_NAMES."""
    assert AnomalyAttributor(model).sensor_names == list(SENSOR_NAMES)


def test_should_use_custom_sensor_names_as_attribution_keys() -> None:
    """Custom sensor names should appear as attribution keys."""
    custom_names = ["s1", "s2", "s3", "s4"]
    attributor = AnomalyAttributor(_IdentityModel(), sensor_names=custom_names)
    attributor.set_baseline(np.zeros((2, SEQ_LEN, NUM_SENSORS)))
    attribution_result = attributor.attribute_sequence(
        np.zeros((SEQ_LEN, NUM_SENSORS))
    )
    assert list(attribution_result["attribution"].keys()) == custom_names


def test_should_put_model_in_eval_mode(model: LSTMVAE) -> None:
    """Attribution is inference-only: dropout must be disabled."""
    assert AnomalyAttributor(model).model.training is False


def test_should_restore_eval_mode_after_external_train_call(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """A shared pipeline calling model.train() must not re-enable dropout."""
    attributor.model.train()
    attribution_result = attributor.attribute_sequence(anomalous_sequence)
    assert attributor.model.training is False
    assert "attribution" in attribution_result


# -------------------------------------------------------------- set_baseline


def test_should_raise_when_baseline_is_empty(model: LSTMVAE) -> None:
    """An (0, seq_len, input_dim) baseline must be rejected."""
    with pytest.raises(ValueError, match="non-empty"):
        AnomalyAttributor(model).set_baseline(np.empty((0, SEQ_LEN, NUM_SENSORS)))


def test_should_raise_when_baseline_is_not_3d(model: LSTMVAE) -> None:
    """A 2D baseline must be rejected."""
    with pytest.raises(ValueError, match="non-empty"):
        AnomalyAttributor(model).set_baseline(np.zeros((SEQ_LEN, NUM_SENSORS)))


def test_should_raise_when_baseline_sensor_count_mismatches(model: LSTMVAE) -> None:
    """A baseline with the wrong sensor axis must be rejected."""
    with pytest.raises(ValueError, match="expected"):
        AnomalyAttributor(model).set_baseline(
            np.zeros((3, SEQ_LEN, NUM_SENSORS + 1))
        )


@pytest.mark.parametrize("bad_value", [np.nan, np.inf, -np.inf])
def test_should_raise_when_baseline_contains_non_finite_values(
    model: LSTMVAE, bad_value: float
) -> None:
    """NaN/Inf in the baseline must be rejected loudly, not propagated."""
    corrupted_baseline = np.zeros((3, SEQ_LEN, NUM_SENSORS), dtype=np.float32)
    corrupted_baseline[1, 0, 0] = bad_value
    with pytest.raises(ValueError, match="non-finite"):
        AnomalyAttributor(model).set_baseline(corrupted_baseline)


def test_should_raise_when_model_output_is_non_finite_at_baseline() -> None:
    """A model emitting NaN reconstructions must fail set_baseline loudly."""
    nan_model = _NaNDecodeModel()
    nan_model.should_emit_nan = True
    attributor = AnomalyAttributor(nan_model, device="cpu")
    with pytest.raises(ValueError, match="Non-finite reconstruction error"):
        attributor.set_baseline(np.zeros((3, SEQ_LEN, NUM_SENSORS)))
    assert attributor.baseline_sequences is None
    assert attributor.baseline_error is None


# -------------------------------------------------------- attribute_sequence


def test_should_raise_when_baseline_not_set(
    model: LSTMVAE, anomalous_sequence: np.ndarray
) -> None:
    """Attribution without set_baseline() must fail fast."""
    with pytest.raises(ValueError, match="set_baseline"):
        AnomalyAttributor(model).attribute_sequence(anomalous_sequence)


def test_should_raise_when_sequence_is_1d(attributor: AnomalyAttributor) -> None:
    """A 1D sequence must be rejected."""
    with pytest.raises(ValueError, match="shape"):
        attributor.attribute_sequence(np.zeros(SEQ_LEN))


def test_should_raise_when_sequence_sensor_count_mismatches(
    attributor: AnomalyAttributor,
) -> None:
    """A sequence with the wrong sensor axis must be rejected."""
    with pytest.raises(ValueError, match="shape"):
        attributor.attribute_sequence(np.zeros((SEQ_LEN, NUM_SENSORS + 1)))


@pytest.mark.parametrize("bad_value", [np.nan, np.inf, -np.inf])
def test_should_raise_when_sequence_contains_non_finite_values(
    attributor: AnomalyAttributor, bad_value: float
) -> None:
    """NaN/Inf in the attributed sequence must be rejected loudly."""
    corrupted_sequence = np.zeros((SEQ_LEN, NUM_SENSORS), dtype=np.float32)
    corrupted_sequence[0, 0] = bad_value
    with pytest.raises(ValueError, match="non-finite"):
        attributor.attribute_sequence(corrupted_sequence)


def test_should_raise_when_model_output_becomes_non_finite() -> None:
    """Weights corrupted AFTER baseline (e.g. bad online update) must raise."""
    nan_model = _NaNDecodeModel()
    attributor = AnomalyAttributor(nan_model, device="cpu")
    attributor.set_baseline(np.zeros((3, SEQ_LEN, NUM_SENSORS)))
    nan_model.should_emit_nan = True
    with pytest.raises(ValueError, match="Non-finite reconstruction error"):
        attributor.attribute_sequence(np.zeros((SEQ_LEN, NUM_SENSORS)))


def test_should_return_all_result_keys(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Result dict should expose the full documented contract."""
    attribution_result = attributor.attribute_sequence(anomalous_sequence)
    expected_keys = {
        "attribution",
        "sensor_contributions",
        "affected_sensors",
        "confidence",
        "anomaly_error",
        "baseline_error",
        "is_fallback",
    }
    assert expected_keys <= set(attribution_result.keys())
    assert attribution_result["baseline_error"] == attributor.baseline_error


def test_should_report_non_negative_raw_contributions(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Raw contributions are clamped at zero, one per sensor."""
    attribution_result = attributor.attribute_sequence(anomalous_sequence)
    raw_contributions = attribution_result["sensor_contributions"]
    assert len(raw_contributions) == NUM_SENSORS
    assert all(contribution >= 0.0 for contribution in raw_contributions)


def test_should_be_deterministic_across_calls(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Two attributions of the same sequence must match exactly."""
    first_result = attributor.attribute_sequence(anomalous_sequence)
    second_result = attributor.attribute_sequence(anomalous_sequence)
    assert first_result["attribution"] == second_result["attribution"]
    assert first_result["anomaly_error"] == second_result["anomaly_error"]


# ----------------------------------------------- zero / dominant attribution


def test_should_fall_back_to_uniform_when_all_contributions_zero(
    identity_attributor: AnomalyAttributor,
) -> None:
    """Perfect reconstruction → zero contributions → flagged uniform fallback."""
    rng = np.random.default_rng(5)
    attribution_result = identity_attributor.attribute_sequence(
        rng.random((SEQ_LEN, NUM_SENSORS))
    )
    assert attribution_result["sensor_contributions"] == [0.0] * NUM_SENSORS
    for contribution in attribution_result["attribution"].values():
        assert contribution == pytest.approx(UNIFORM_CONTRIBUTION)
    # Fallback is flagged and confidence floored to 0.0 — distinguishable
    # from a genuine four-way split (which would carry confidence >= 0.5).
    assert attribution_result["is_fallback"] is True
    assert attribution_result["confidence"] == 0.0


def test_should_attribute_everything_to_single_dominant_sensor(
    dominant_attributor: AnomalyAttributor,
    first_channel_spike_sequence: np.ndarray,
) -> None:
    """All error in channel 0 → 100% attribution to the first sensor."""
    attribution_result = dominant_attributor.attribute_sequence(
        first_channel_spike_sequence
    )
    attribution = attribution_result["attribution"]
    assert attribution[SENSOR_NAMES[0]] == pytest.approx(1.0)
    for other_sensor in SENSOR_NAMES[1:]:
        assert attribution[other_sensor] == pytest.approx(0.0)
    assert attribution_result["affected_sensors"] == [SENSOR_NAMES[0]]
    assert attribution_result["confidence"] == pytest.approx(1.0)


def test_should_not_flag_fallback_for_normal_attribution(
    dominant_attributor: AnomalyAttributor,
    first_channel_spike_sequence: np.ndarray,
) -> None:
    """A genuine non-zero attribution must carry is_fallback=False."""
    attribution_result = dominant_attributor.attribute_sequence(
        first_channel_spike_sequence
    )
    assert attribution_result["is_fallback"] is False


# ------------------------------------------------------------ attribute_batch


def test_should_return_empty_list_for_empty_batch(
    attributor: AnomalyAttributor,
) -> None:
    """A (0, seq_len, input_dim) batch yields no results."""
    assert attributor.attribute_batch(np.empty((0, SEQ_LEN, NUM_SENSORS))) == []


def test_should_handle_batch_of_one(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """A single-element batch yields exactly one attribution dict."""
    attribution_results = attributor.attribute_batch(
        anomalous_sequence[np.newaxis, :]
    )
    assert len(attribution_results) == 1
    assert "attribution" in attribution_results[0]


# ------------------------------------------------------------ get_explanation


def test_should_explain_nothing_when_no_affected_sensors(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Empty affected_sensors → 'no significant deviation' message."""
    explanation = attributor.get_explanation(
        {"affected_sensors": [], "attribution": {}}, anomalous_sequence
    )
    assert explanation == "No significant sensor deviation detected"


def test_should_explain_nothing_when_affected_sensors_key_missing(
    attributor: AnomalyAttributor, anomalous_sequence: np.ndarray
) -> None:
    """Missing affected_sensors key is treated as no affected sensors."""
    explanation = attributor.get_explanation({}, anomalous_sequence)
    assert explanation == "No significant sensor deviation detected"


def test_should_explain_nothing_when_baseline_missing(
    model: LSTMVAE, anomalous_sequence: np.ndarray
) -> None:
    """Without a baseline there is nothing to compute deviations against."""
    attributor_without_baseline = AnomalyAttributor(model)
    explanation = attributor_without_baseline.get_explanation(
        {"affected_sensors": [SENSOR_NAMES[0]], "attribution": {}},
        anomalous_sequence,
    )
    assert explanation == "No significant sensor deviation detected"


def test_should_report_unknown_cause_when_deviation_below_threshold(
    dominant_attributor: AnomalyAttributor,
) -> None:
    """Affected sensor with ~zero deviation from baseline → 'Unknown cause'."""
    crafted_result = {
        "affected_sensors": [SENSOR_NAMES[0]],
        "attribution": {SENSOR_NAMES[0]: 1.0},
    }
    explanation = dominant_attributor.get_explanation(
        crafted_result, np.zeros((SEQ_LEN, NUM_SENSORS))
    )
    assert explanation == "Unknown cause"


def test_should_format_deviation_and_contribution_in_explanation(
    dominant_attributor: AnomalyAttributor,
    first_channel_spike_sequence: np.ndarray,
) -> None:
    """Explanation includes signed deviation and percent contribution."""
    attribution_result = dominant_attributor.attribute_sequence(
        first_channel_spike_sequence
    )
    explanation = dominant_attributor.get_explanation(
        attribution_result, first_channel_spike_sequence
    )
    assert f"{SENSOR_NAMES[0]} deviation: +1.00" in explanation
    assert "(100% contribution)" in explanation


# ------------------------------------------------------- AttributionStrength


def test_gini_should_be_zero_for_zero_sum_distribution() -> None:
    """All-zero attribution has zero Gini impurity by definition."""
    zero_attribution = {name: 0.0 for name in SENSOR_NAMES}
    assert AttributionStrength.gini_impurity(zero_attribution) == 0.0


def test_gini_should_be_zero_for_empty_dict() -> None:
    """An empty attribution dict degrades gracefully to 0.0."""
    assert AttributionStrength.gini_impurity({}) == 0.0


def test_gini_should_be_maximal_for_uniform_distribution() -> None:
    """Uniform over 4 sensors → 1 - 4 * (1/4)^2 = 0.75."""
    uniform_attribution = {name: UNIFORM_CONTRIBUTION for name in SENSOR_NAMES}
    assert AttributionStrength.gini_impurity(uniform_attribution) == pytest.approx(
        0.75
    )


def test_gini_should_be_zero_for_one_hot_distribution() -> None:
    """One dominant sensor → Gini impurity of 0 (perfectly clear)."""
    one_hot_attribution = {name: 0.0 for name in SENSOR_NAMES}
    one_hot_attribution[SENSOR_NAMES[0]] = 1.0
    assert AttributionStrength.gini_impurity(one_hot_attribution) == pytest.approx(
        0.0
    )


def test_gini_should_normalize_unnormalized_values() -> None:
    """Values not summing to 1 are normalized before computing Gini."""
    unnormalized_attribution = {"a": 2.0, "b": 2.0}
    assert AttributionStrength.gini_impurity(
        unnormalized_attribution
    ) == pytest.approx(0.5)


def test_entropy_should_be_zero_for_zero_sum_distribution() -> None:
    """All-zero attribution has zero entropy by definition."""
    zero_attribution = {name: 0.0 for name in SENSOR_NAMES}
    assert AttributionStrength.entropy(zero_attribution) == 0.0


def test_entropy_should_be_two_bits_for_uniform_distribution() -> None:
    """Uniform over 4 sensors → log2(4) = 2 bits."""
    uniform_attribution = {name: UNIFORM_CONTRIBUTION for name in SENSOR_NAMES}
    assert AttributionStrength.entropy(uniform_attribution) == pytest.approx(2.0)


def test_entropy_should_be_zero_for_one_hot_distribution() -> None:
    """One dominant sensor → zero entropy (no uncertainty)."""
    one_hot_attribution = {name: 0.0 for name in SENSOR_NAMES}
    one_hot_attribution[SENSOR_NAMES[0]] = 1.0
    assert AttributionStrength.entropy(one_hot_attribution) == pytest.approx(0.0)


def test_entropy_should_filter_negligible_probabilities() -> None:
    """Near-zero contributions are filtered, avoiding log2(0) NaNs."""
    near_one_hot_attribution = {"a": 1.0, "b": 1e-12}
    entropy_bits = AttributionStrength.entropy(near_one_hot_attribution)
    assert np.isfinite(entropy_bits)
    assert entropy_bits == pytest.approx(0.0, abs=1e-9)
