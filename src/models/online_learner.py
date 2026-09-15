"""
Online learning (Gap 3 — Online Learning Pipeline).

Continuously adapts a pretrained LSTM-VAE to a tenant's real sensor stream:

1. Periodic mini-batch SGD: one update every N observed sequences.
2. Prioritized experience replay: hard (high-loss) examples sampled more
   often, with importance weights applied to per-sample losses.
3. Catastrophic forgetting prevention via Elastic Weight Consolidation.
4. Loss-trend tracking to flag when full retraining is needed.
"""

import copy
import logging
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

import numpy as np
import torch
import torch.nn.functional as F

from src.config import (
    DEVICE,
    DRIFT_THRESHOLD,
    EWC_FISHER_BATCHES,
    EWC_FISHER_WARMUP_SAMPLES,
    EWC_LAMBDA,
    GRAD_CLIP_NORM,
    NUMERICAL_EPSILON,
    ONLINE_BATCH_SIZE,
    ONLINE_LEARNING_RATE,
    ONLINE_LOG_EVERY_N_UPDATES,
    ONLINE_SGD_MOMENTUM,
    ONLINE_TREND_WINDOW,
    ONLINE_UPDATE_INTERVAL,
    REPLAY_BUFFER_CAPACITY,
)
from src.models.catastrophic_forgetting import ElasticWeightConsolidation
from src.models.lstm_vae import LSTMVAE
from src.models.replay_buffer import ReplayBuffer
from src.utils.lru import LRUDict

logger = logging.getLogger(__name__)

# Minimum observed losses before trend / retraining heuristics activate.
_MIN_TREND_HISTORY: int = 2 * ONLINE_TREND_WINDOW


class OnlineLearner:
    """
    Continuously adapt an LSTM-VAE to a single tenant's streaming data.

    Example:
        learner = OnlineLearner(model=pretrained_vae, tenant_id="gh_001")
        for sequence in stream:                # (seq_len, input_dim) arrays
            loss = learner.observe(sequence)   # learns every N sequences
    """

    def __init__(
        self,
        model: LSTMVAE,
        tenant_id: str,
        device: str = DEVICE,
        learning_rate: float = ONLINE_LEARNING_RATE,
        buffer_capacity: int = REPLAY_BUFFER_CAPACITY,
        update_interval: int = ONLINE_UPDATE_INTERVAL,
        batch_size: int = ONLINE_BATCH_SIZE,
        use_ewc: bool = True,
        ewc_lambda: float = EWC_LAMBDA,
    ) -> None:
        """
        Args:
            model: Pretrained LSTMVAE to adapt (owned by this learner).
            tenant_id: Tenant whose data stream this learner consumes.
            device: "cpu" or "cuda".
            learning_rate: SGD step size for online updates.
            buffer_capacity: Max sequences kept in the replay buffer.
            update_interval: Run one SGD step every N observed sequences.
            batch_size: Mini-batch size sampled from the replay buffer.
            use_ewc: Enable EWC regularization (strength `ewc_lambda`).
            ewc_lambda: EWC regularization strength.
        """
        self.device = torch.device(device)
        self.model = model.to(self.device)
        self.model.eval()
        self.tenant_id = tenant_id
        self.learning_rate = learning_rate
        self.update_interval = update_interval
        self.batch_size = batch_size

        self.buffer = ReplayBuffer(capacity=buffer_capacity)

        self.optimizer = torch.optim.SGD(
            self.model.parameters(),
            lr=learning_rate,
            momentum=ONLINE_SGD_MOMENTUM,
            nesterov=True,
        )

        self.ewc: Optional[ElasticWeightConsolidation] = None
        self._ewc_fisher_ready = False
        if use_ewc:
            self.ewc = ElasticWeightConsolidation(self.model, ewc_lambda, device)
            logger.info(
                "EWC enabled; Fisher computed after %d observed sequences",
                EWC_FISHER_WARMUP_SAMPLES,
            )

        self.step_count = 0
        self.update_count = 0
        self.losses: Deque[float] = deque(maxlen=2000)
        self.update_history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=500))

    def _to_batch_tensor(self, sequence: np.ndarray) -> torch.Tensor:
        """Convert a (seq_len, input_dim) array to a (1, seq_len, input_dim) tensor."""
        return torch.as_tensor(
            sequence[np.newaxis, :], dtype=torch.float32
        ).to(self.device)

    def observe(self, sequence: np.ndarray) -> float:
        """
        Observe a new (seq_len, input_dim) sequence: compute its loss, store
        it in the replay buffer, and run one SGD step every `update_interval`
        observations. Returns the scalar VAE loss (anomaly signal).
        """
        x = self._to_batch_tensor(sequence)

        with torch.no_grad():
            recon, mu, logvar, _ = self.model(x)
            loss = self.model.loss_function(recon, x, mu, logvar).item()

        self.buffer.add(sequence, loss=loss)

        self.step_count += 1
        self._maybe_compute_fisher()
        if self.step_count % self.update_interval == 0:
            self._update_step()

        self.losses.append(loss)
        return loss

    def _maybe_compute_fisher(self) -> None:
        """Compute the EWC Fisher once, after the warm-up window.

        The Fisher is estimated from the tenant's own early sequences (before
        much adaptation has happened), so it marks the parameters that matter
        for reconstructing this tenant's *normal* manifold. Anchoring EWC to
        those parameters is what actually makes ``penalty()`` non-zero — until
        this runs, forgetting protection is only the small LR + replay mixing.
        """
        if (
            self.ewc is None
            or self._ewc_fisher_ready
            or self.step_count < EWC_FISHER_WARMUP_SAMPLES
            or len(self.buffer) < self.batch_size
        ):
            return

        batches = [
            self.buffer.sample(batch_size=self.batch_size)
            for _ in range(EWC_FISHER_BATCHES)
        ]
        loader = [
            torch.as_tensor(batch, dtype=torch.float32).to(self.device)
            for batch in batches
            if len(batch) > 0
        ]
        if not loader:
            return
        self.ewc.compute_fisher(loader)
        self.model.eval()
        self._ewc_fisher_ready = True
        logger.info(
            "[%s] EWC Fisher computed after %d sequences — forgetting penalty active",
            self.tenant_id,
            self.step_count,
        )

    def _per_sample_loss(
        self,
        recon: torch.Tensor,
        x: torch.Tensor,
        mu: torch.Tensor,
        logvar: torch.Tensor,
    ) -> torch.Tensor:
        """
        Per-sample beta-VAE loss of shape (batch,): mirrors
        LSTMVAE.loss_function (mean MSE + beta * per-sample KL sum) without
        the batch reduction, so importance weights can be applied per sample.
        """
        recon_loss = F.mse_loss(recon, x, reduction="none").mean(dim=(1, 2))
        kl = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp(), dim=1)
        return recon_loss + self.model.beta * kl

    def _update_step(self) -> None:
        """One importance-weighted SGD step on a prioritized replay batch."""
        if len(self.buffer) < self.batch_size:
            return  # Not enough data yet.

        batch, weights = self.buffer.sample_prioritized(batch_size=self.batch_size)
        x = torch.as_tensor(batch, dtype=torch.float32).to(self.device)
        w = torch.as_tensor(weights, dtype=torch.float32).to(self.device)

        self.model.train()
        self.optimizer.zero_grad()

        recon, mu, logvar, _ = self.model(x)
        loss_per_sample = self._per_sample_loss(recon, x, mu, logvar)
        weighted_loss = (loss_per_sample * w).mean()

        if self.ewc is not None:
            weighted_loss = weighted_loss + self.ewc.penalty()

        weighted_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), GRAD_CLIP_NORM)
        self.optimizer.step()

        self.model.eval()
        self.update_count += 1
        self.update_history["loss"].append(weighted_loss.item())

        if self.update_count % ONLINE_LOG_EVERY_N_UPDATES == 0:
            logger.info(
                "[%s] Online update #%d: loss=%.6f",
                self.tenant_id,
                self.update_count,
                weighted_loss.item(),
            )

    def predict(self, sequence: np.ndarray) -> Dict[str, Any]:
        """
        Predict for one sequence. Returns dict with anomaly_score
        (reconstruction MSE), embedding (latent mu, LATENT_DIM), uncertainty
        (mean latent std, exp(0.5*logvar)), trend ('increasing'/'stable'/None)
        and updates_performed.
        """
        x = self._to_batch_tensor(sequence)

        with torch.no_grad():
            recon, mu, logvar, _ = self.model(x)
            anomaly_score = F.mse_loss(recon, x).item()
            embedding = mu[0].cpu().numpy()
            uncertainty = float(torch.exp(0.5 * logvar)[0].mean().item())

        trend = None
        if len(self.losses) >= _MIN_TREND_HISTORY:
            # deque does not support slicing; convert to list first
            losses_list = list(self.losses)
            recent_avg = float(np.mean(losses_list[-ONLINE_TREND_WINDOW:]))
            historical_avg = float(np.mean(losses_list[:-ONLINE_TREND_WINDOW]))
            trend = "increasing" if recent_avg > historical_avg else "stable"

        return {
            "anomaly_score": anomaly_score,
            "embedding": embedding,
            "uncertainty": uncertainty,
            "trend": trend,
            "updates_performed": self.update_count,
        }

    def get_statistics(self) -> Dict[str, Any]:
        """Learning statistics: counts, buffer status, loss stats, history tail."""
        return {
            "tenant_id": self.tenant_id,
            "sequences_observed": self.step_count,
            "updates_performed": self.update_count,
            "buffer_status": self.buffer.get_statistics(),
            "loss_statistics": {
                "min": float(np.min(self.losses)) if self.losses else None,
                "max": float(np.max(self.losses)) if self.losses else None,
                "mean": float(np.mean(self.losses)) if self.losses else None,
                "std": float(np.std(self.losses)) if self.losses else None,
            },
            "update_history": {
                key: list(values)[-ONLINE_LOG_EVERY_N_UPDATES:]
                for key, values in self.update_history.items()
            },
        }

    def needs_retraining(self, threshold: float = DRIFT_THRESHOLD) -> bool:
        """
        True if the recent loss average exceeds the historical average by
        more than `threshold` (normalized) — i.e. the model has drifted.
        """
        if len(self.losses) < _MIN_TREND_HISTORY:
            return False

        # deque does not support slicing; convert to list first
        losses_list = list(self.losses)
        recent_avg = float(np.mean(losses_list[-ONLINE_TREND_WINDOW:]))
        historical_avg = float(np.mean(losses_list[:-ONLINE_TREND_WINDOW]))
        drift = (recent_avg - historical_avg) / (historical_avg + NUMERICAL_EPSILON)
        return drift > threshold

    def save_checkpoint(self, path: str) -> None:
        """Save the adapted model's state dict to disk."""
        torch.save(self.model.state_dict(), path)
        logger.info("Saved online-learned model to %s", path)

    def load_checkpoint(self, path: str) -> None:
        """Load an adapted model state dict from disk."""
        state_dict = torch.load(path, map_location=self.device, weights_only=True)
        self.model.load_state_dict(state_dict)
        logger.info("Loaded online-learned model from %s", path)

    def save_full_state(self, path: str) -> None:
        """
        Save the learner's state weights to disk.
        Accepted tradeoff: eviction persists weights only; replay buffer/optimizer momentum/EWC Fisher are lost and re-warm from the live stream. Full-state serialization is a documented follow-up if drift metrics regress.
        """
        self.save_checkpoint(path)

    def load_full_state(self, path: str) -> None:
        """
        Load the learner's state weights from disk.
        Accepted tradeoff: eviction persists weights only; replay buffer/optimizer momentum/EWC Fisher are lost and re-warm from the live stream. Full-state serialization is a documented follow-up if drift metrics regress.
        """
        self.load_checkpoint(path)


class OnlineLearningManager:
    """
    Manage independent online learners for multiple tenants.

    Each learner adapts a deep copy of the base model, so tenants never share
    weights (fixes the spec bug where every learner mutated the same model).

    Example:
        manager = OnlineLearningManager(base_model)
        loss = manager.observe("gh_001", sequence)
        result = manager.predict("gh_002", sequence)
    """

    def __init__(self, base_model: LSTMVAE, device: str = DEVICE) -> None:
        """
        Args:
            base_model: Pretrained starting point for every tenant; deep
                copied (never mutated) when learners are created.
            device: "cpu" or "cuda".
        """
        self.base_model = base_model.to(torch.device(device))
        self.base_model.eval()
        self.device = device

        def _evict_callback(tenant_id: str, learner: OnlineLearner) -> None:
            import os
            checkpoint_dir = "models/checkpoints/online_learners"
            os.makedirs(checkpoint_dir, exist_ok=True)
            path = f"{checkpoint_dir}/{tenant_id}.pt"
            learner.save_full_state(path)

        self.learners: LRUDict = LRUDict(max_size=10, on_evict=_evict_callback)

    def _get_or_restore_learner(self, tenant_id: str) -> Optional[OnlineLearner]:
        """
        Retrieve a learner from the cache or restore it from checkpoint if one exists.
        Accepted tradeoff: eviction persists weights only; replay buffer/optimizer momentum/EWC Fisher are lost and re-warm from the live stream. Full-state serialization is a documented follow-up if drift metrics regress.
        """
        if tenant_id in self.learners:
            return self.learners[tenant_id]

        import os
        checkpoint_path = f"models/checkpoints/online_learners/{tenant_id}.pt"
        if os.path.exists(checkpoint_path):
            logger.info("Restoring online learner for tenant %s from checkpoint", tenant_id)
            model_copy = copy.deepcopy(self.base_model)
            learner = OnlineLearner(model_copy, tenant_id, self.device)
            learner.load_full_state(checkpoint_path)
            self.learners[tenant_id] = learner
            return learner

        return None

    def create_learner(self, tenant_id: str, **kwargs: Any) -> OnlineLearner:
        """Create a learner for a tenant from a deep copy of the base model."""
        model_copy = copy.deepcopy(self.base_model)
        learner = OnlineLearner(model_copy, tenant_id, self.device, **kwargs)
        self.learners[tenant_id] = learner
        logger.info("Created online learner for tenant %s", tenant_id)
        return learner

    def observe(self, tenant_id: str, sequence: np.ndarray) -> float:
        """Observe a sequence for a tenant, creating its learner on demand."""
        learner = self._get_or_restore_learner(tenant_id)
        if learner is None:
            learner = self.create_learner(tenant_id)
        return learner.observe(sequence)

    def predict(self, tenant_id: str, sequence: np.ndarray) -> Dict[str, Any]:
        """
        Predict for a tenant. Falls back to the (frozen) base model when the
        tenant has no learner yet.
        """
        learner = self._get_or_restore_learner(tenant_id)
        if learner is None:
            x = torch.as_tensor(
                sequence[np.newaxis, :], dtype=torch.float32
            ).to(self.base_model.fc_mu.weight.device)
            with torch.no_grad():
                recon, mu, logvar, _ = self.base_model(x)
                return {
                    "anomaly_score": F.mse_loss(recon, x).item(),
                    "embedding": mu[0].cpu().numpy(),
                    "uncertainty": float(torch.exp(0.5 * logvar)[0].mean().item()),
                    "learner_active": False,
                }

        result = learner.predict(sequence)
        result["learner_active"] = True
        return result

    def get_tenant_statistics(self, tenant_id: str) -> Dict[str, Any]:
        """Statistics for a tenant's learner, or a no-learner marker."""
        learner = self._get_or_restore_learner(tenant_id)
        if learner is None:
            return {"status": "no_learner"}
        return learner.get_statistics()
