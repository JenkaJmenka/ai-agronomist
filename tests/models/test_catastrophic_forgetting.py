"""Unit tests for EWC and Synaptic Intelligence (Gap 3 — Online Learning)."""

from __future__ import annotations

import logging
from typing import List

import pytest
import torch

from src.models.catastrophic_forgetting import (
    ElasticWeightConsolidation,
    SynapticIntelligence,
    _extract_input,
)
from src.models.lstm_vae import LSTMVAE

pytestmark = pytest.mark.unit

# Tiny architecture keeps Fisher / SGD passes fast.
TINY_CONFIG = {
    "input_dim": 2,
    "hidden_dims": [8],
    "latent_dim": 3,
    "seq_len": 6,
    "beta": 0.1,
    "dropout": 0.0,
}


@pytest.fixture
def tiny_model() -> LSTMVAE:
    torch.manual_seed(0)
    return LSTMVAE(**TINY_CONFIG)


def make_batches(n_batches: int = 2, batch_size: int = 4) -> List[torch.Tensor]:
    torch.manual_seed(1)
    return [
        torch.randn(batch_size, TINY_CONFIG["seq_len"], TINY_CONFIG["input_dim"])
        for _ in range(n_batches)
    ]


def perturb(model: torch.nn.Module, delta: float = 0.1) -> None:
    """Shift every parameter by a constant offset."""
    with torch.no_grad():
        for param in model.parameters():
            param.add_(delta)


class TestExtractInput:
    def test_extracts_first_element_from_tuple(self) -> None:
        x = torch.zeros(2, 3)
        assert _extract_input((x, "label")) is x
        assert _extract_input([x]) is x

    def test_passes_through_plain_tensor(self) -> None:
        x = torch.zeros(2, 3)
        assert _extract_input(x) is x


class TestElasticWeightConsolidation:
    def test_penalty_zero_when_fisher_not_computed(self, tiny_model: LSTMVAE) -> None:
        ewc = ElasticWeightConsolidation(tiny_model)
        penalty = ewc.penalty()
        assert isinstance(penalty, torch.Tensor)
        assert penalty.item() == 0.0
        assert ewc.fisher is None

    def test_snapshots_all_named_parameters(self, tiny_model: LSTMVAE) -> None:
        ewc = ElasticWeightConsolidation(tiny_model)
        expected = {name for name, _ in tiny_model.named_parameters()}
        assert set(ewc.params.keys()) == expected

    def test_compute_fisher_from_tensor_batches(self, tiny_model: LSTMVAE) -> None:
        ewc = ElasticWeightConsolidation(tiny_model)
        ewc.compute_fisher(make_batches())
        assert ewc.fisher is not None
        expected = {name for name, _ in tiny_model.named_parameters()}
        assert set(ewc.fisher.keys()) == expected
        # Fisher is a sum of squared gradients: non-negative everywhere,
        # strictly positive somewhere.
        total = 0.0
        for value in ewc.fisher.values():
            assert torch.all(value >= 0.0)
            total += value.sum().item()
        assert total > 0.0

    def test_compute_fisher_from_tuple_batches(self, tiny_model: LSTMVAE) -> None:
        ewc = ElasticWeightConsolidation(tiny_model)
        ewc.compute_fisher([(x, None) for x in make_batches()])
        assert ewc.fisher is not None

    def test_compute_fisher_empty_dataloader_warns_and_skips(
        self, tiny_model: LSTMVAE, caplog: pytest.LogCaptureFixture
    ) -> None:
        ewc = ElasticWeightConsolidation(tiny_model)
        with caplog.at_level(
            logging.WARNING, logger="src.models.catastrophic_forgetting"
        ):
            ewc.compute_fisher([])
        assert ewc.fisher is None
        assert any("empty dataloader" in r.getMessage() for r in caplog.records)

    def test_penalty_zero_at_snapshot_parameters(self, tiny_model: LSTMVAE) -> None:
        ewc = ElasticWeightConsolidation(tiny_model)
        ewc.compute_fisher(make_batches())
        # Parameters unchanged since snapshot -> zero drift penalty.
        assert ewc.penalty().item() == pytest.approx(0.0)

    def test_penalty_positive_after_parameter_drift(self, tiny_model: LSTMVAE) -> None:
        ewc = ElasticWeightConsolidation(tiny_model)
        ewc.compute_fisher(make_batches())
        perturb(tiny_model)
        penalty = ewc.penalty()
        assert penalty.item() > 0.0

    def test_penalty_scales_with_lambda(self, tiny_model: LSTMVAE) -> None:
        ewc = ElasticWeightConsolidation(tiny_model, ewc_lambda=1.0)
        ewc.compute_fisher(make_batches())
        perturb(tiny_model)
        base = ewc.penalty().item()
        ewc.ewc_lambda = 2.0
        assert ewc.penalty().item() == pytest.approx(2.0 * base, rel=1e-5)


class TestSynapticIntelligence:
    def test_penalty_zero_at_anchor(self, tiny_model: LSTMVAE) -> None:
        si = SynapticIntelligence(tiny_model)
        penalty = si.penalty()
        assert isinstance(penalty, torch.Tensor)
        assert penalty.item() == pytest.approx(0.0)

    def test_initial_importance_and_path_integral_zero(
        self, tiny_model: LSTMVAE
    ) -> None:
        si = SynapticIntelligence(tiny_model)
        for tensor in si.importance.values():
            assert torch.all(tensor == 0.0)
        for tensor in si._path_integral.values():
            assert torch.all(tensor == 0.0)

    def test_accumulate_importance_skips_when_no_gradients(
        self, tiny_model: LSTMVAE
    ) -> None:
        si = SynapticIntelligence(tiny_model)
        si.accumulate_importance()  # No backward yet; all grads are None.
        for tensor in si._path_integral.values():
            assert torch.all(tensor == 0.0)

    def _run_training_steps(
        self, model: LSTMVAE, si: SynapticIntelligence, steps: int = 3
    ) -> None:
        """Plain-SGD path-integral loop following the documented SI usage."""
        optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
        for batch in make_batches(n_batches=steps):
            recon, mu, logvar, _ = model(batch)
            loss = model.loss_function(recon, batch, mu, logvar)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            si.accumulate_importance()
            si.track_step()

    def test_consolidate_builds_importance_and_resets_path_integral(
        self, tiny_model: LSTMVAE
    ) -> None:
        si = SynapticIntelligence(tiny_model)
        self._run_training_steps(tiny_model, si)
        si.consolidate()
        importance_total = sum(t.sum().item() for t in si.importance.values())
        assert importance_total > 0.0
        for tensor in si._path_integral.values():
            assert torch.all(tensor == 0.0)

    def test_penalty_zero_right_after_consolidation(self, tiny_model: LSTMVAE) -> None:
        si = SynapticIntelligence(tiny_model)
        self._run_training_steps(tiny_model, si)
        si.consolidate()
        # Reference re-anchored at current parameters -> zero penalty.
        assert si.penalty().item() == pytest.approx(0.0)

    def test_penalty_positive_after_drift_from_consolidated_anchor(
        self, tiny_model: LSTMVAE
    ) -> None:
        si = SynapticIntelligence(tiny_model)
        self._run_training_steps(tiny_model, si)
        si.consolidate()
        perturb(tiny_model)
        assert si.penalty().item() > 0.0

    def test_track_step_updates_prev_params(self, tiny_model: LSTMVAE) -> None:
        si = SynapticIntelligence(tiny_model)
        perturb(tiny_model)
        si.track_step()
        for name, param in tiny_model.named_parameters():
            assert torch.equal(si._prev_params[name], param.data)
