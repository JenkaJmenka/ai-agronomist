"""
Catastrophic forgetting prevention (Gap 3 — Online Learning Pipeline).

Fine-tuning a pretrained LSTM-VAE on streaming customer data can overwrite
knowledge learned during pre-training. Both regularizers here add a penalty
on parameter drift, weighted by how important each parameter was:

    L = task_loss + lambda/2 * sum_i( importance_i * (theta_i - theta*_i)^2 )

- ElasticWeightConsolidation estimates importance via the Fisher information
  (squared gradients over a reference dataset).
- SynapticIntelligence tracks importance online from the loss path integral,
  with no separate Fisher pass.
"""

import logging
from typing import Dict, Iterable, Optional

import torch
import torch.nn as nn

from src.config import DEVICE, EWC_LAMBDA, SI_DAMPING, SI_LAMBDA

logger = logging.getLogger(__name__)


def _extract_input(batch: object) -> torch.Tensor:
    """Return the input tensor from a DataLoader batch (tensor or tuple/list)."""
    if isinstance(batch, (tuple, list)):
        return batch[0]
    return batch  # type: ignore[return-value]


class ElasticWeightConsolidation:
    """
    EWC regularization for online fine-tuning.

    Usage:
        ewc = ElasticWeightConsolidation(model, ewc_lambda=0.4)
        ewc.compute_fisher(pretraining_dataloader)
        ...
        loss = task_loss + ewc.penalty()
    """

    def __init__(
        self,
        model: nn.Module,
        ewc_lambda: float = EWC_LAMBDA,
        device: str = DEVICE,
    ) -> None:
        """
        Args:
            model: Network whose pre-training knowledge should be protected.
            ewc_lambda: Regularization strength.
            device: "cpu" or "cuda".
        """
        self.model = model
        self.ewc_lambda = ewc_lambda
        self.device = torch.device(device)

        # Snapshot of parameters at consolidation time (theta*).
        self.params: Dict[str, torch.Tensor] = {
            name: param.data.clone().detach()
            for name, param in model.named_parameters()
        }

        # Fisher information (parameter importance); None until computed.
        self.fisher: Optional[Dict[str, torch.Tensor]] = None

    def compute_fisher(self, dataloader: Iterable) -> None:
        """
        Estimate the diagonal Fisher information from a reference dataset.

        Accumulates squared gradients of the model's own loss over all
        batches, then normalizes by batch count. High Fisher values mark
        parameters that were important to the original task.

        Args:
            dataloader: Iterable of batches; each batch is either a tensor of
                shape (batch, seq_len, input_dim) or a tuple whose first
                element is that tensor.
        """
        fisher = {
            name: torch.zeros_like(param.data)
            for name, param in self.model.named_parameters()
        }

        self.model.eval()
        num_batches = 0

        for batch in dataloader:
            x = _extract_input(batch).to(self.device)

            recon, mu, logvar, _ = self.model(x)
            loss = self.model.loss_function(recon, x, mu, logvar)

            self.model.zero_grad()
            loss.backward()

            for name, param in self.model.named_parameters():
                if param.grad is not None:
                    fisher[name] += param.grad.data ** 2
            num_batches += 1

        if num_batches == 0:
            logger.warning("compute_fisher called with empty dataloader; skipping")
            return

        for name in fisher:
            fisher[name] /= num_batches

        self.model.zero_grad()
        self.fisher = fisher
        logger.info("Computed Fisher information matrix over %d batches", num_batches)

    def penalty(self) -> torch.Tensor:
        """
        EWC penalty for the model's current parameters.

        Returns:
            Scalar tensor: lambda/2 * sum(F_i * (theta_i - theta*_i)^2).
            Zero if the Fisher information has not been computed yet.
        """
        if self.fisher is None:
            return torch.tensor(0.0, device=self.device)

        penalty = torch.tensor(0.0, device=self.device)
        for name, param in self.model.named_parameters():
            if name in self.fisher:
                param_diff = param - self.params[name]
                penalty = penalty + (self.fisher[name] * param_diff ** 2).sum()

        return self.ewc_lambda / 2.0 * penalty


class SynapticIntelligence:
    """
    Synaptic Intelligence (Zenke et al., 2017): online importance tracking.

    Importance is accumulated from the per-step loss path integral
    (-grad * parameter_delta), then consolidated against a stored reference
    snapshot. Unlike EWC, no separate Fisher pass over a dataset is needed.

    Usage:
        si = SynapticIntelligence(model)
        for batch in stream:
            loss = task_loss(batch) + si.penalty()
            loss.backward()
            optimizer.step()
            si.accumulate_importance()   # after step: delta vs pre-step snapshot
            si.track_step()              # re-snapshot params for the next update
        si.consolidate()                 # at task/phase boundary
    """

    def __init__(self, model: nn.Module, si_lambda: float = SI_LAMBDA) -> None:
        """
        Args:
            model: Network to protect.
            si_lambda: Regularization strength.
        """
        self.model = model
        self.si_lambda = si_lambda

        # Reference snapshot (theta*) the penalty is anchored to.
        self.reference_params: Dict[str, torch.Tensor] = {
            name: param.data.clone().detach()
            for name, param in model.named_parameters()
        }
        # Parameter values before the current optimizer step.
        self._prev_params: Dict[str, torch.Tensor] = {
            name: param.data.clone().detach()
            for name, param in model.named_parameters()
        }
        # Path-integral accumulator (w) and consolidated importance (omega).
        self._path_integral: Dict[str, torch.Tensor] = {
            name: torch.zeros_like(param.data)
            for name, param in model.named_parameters()
        }
        self.importance: Dict[str, torch.Tensor] = {
            name: torch.zeros_like(param.data)
            for name, param in model.named_parameters()
        }

    def accumulate_importance(self) -> None:
        """Accumulate -grad * delta into the path integral.

        Call after ``optimizer.step()`` (grads from the last backward are
        still present); before the step, delta vs ``_prev_params`` is zero.
        """
        for name, param in self.model.named_parameters():
            if param.grad is not None:
                delta = param.data - self._prev_params[name]
                self._path_integral[name] += -param.grad.data * delta

    def track_step(self) -> None:
        """Snapshot current parameters as the pre-step values for next update."""
        for name, param in self.model.named_parameters():
            self._prev_params[name] = param.data.clone().detach()

    def consolidate(self, damping: float = SI_DAMPING) -> None:
        """
        Fold the accumulated path integral into the importance weights and
        re-anchor the reference snapshot at the current parameters.

        Args:
            damping: Additive term (xi) preventing division by near-zero
                total parameter displacement.
        """
        for name, param in self.model.named_parameters():
            total_delta = param.data - self.reference_params[name]
            self.importance[name] += self._path_integral[name] / (
                total_delta ** 2 + damping
            )
            self._path_integral[name].zero_()
            self.reference_params[name] = param.data.clone().detach()
        logger.info("Consolidated Synaptic Intelligence importance weights")

    def penalty(self) -> torch.Tensor:
        """
        SI penalty: lambda/2 * sum(omega_i * (theta_i - theta*_i)^2).

        Uses the stored reference snapshot, so the penalty is non-zero once
        parameters drift after consolidation (fixes the spec bug where
        (param - param.data) was identically zero).
        """
        device = next(self.model.parameters()).device
        penalty = torch.tensor(0.0, device=device)
        for name, param in self.model.named_parameters():
            diff = param - self.reference_params[name]
            penalty = penalty + (self.importance[name] * diff ** 2).sum()

        return self.si_lambda / 2.0 * penalty
