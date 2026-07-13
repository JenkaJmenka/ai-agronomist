"""
Replay buffers for online learning (Gap 3 — Online Learning Pipeline).

Stores recent sensor sequences so the online learner can periodically run
mini-batch SGD updates over a mixture of recent data instead of overfitting
to the single most recent sequence. Supports uniform sampling, prioritized
experience replay (high-loss sequences sampled more often, with importance
weights to de-bias the gradient), and label-filtered sampling.
"""

import logging
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import numpy as np

from src.config import (
    ANOMALY_BUFFER_CAPACITY,
    IMPORTANCE_WEIGHT_BETA,
    NUMERICAL_EPSILON,
    ONLINE_BATCH_SIZE,
    PRIORITY_ALPHA,
    REPLAY_BUFFER_CAPACITY,
)

logger = logging.getLogger(__name__)

UNLABELED: str = "unlabeled"


class ReplayBuffer:
    """
    Stores recent sequences and samples mini-batches for online learning.

    Example:
        >>> buffer = ReplayBuffer(capacity=1000)
        >>> buffer.add(np.random.rand(20, 4), loss=0.12)
        >>> batch = buffer.sample(batch_size=32)
    """

    def __init__(self, capacity: int = REPLAY_BUFFER_CAPACITY) -> None:
        """
        Args:
            capacity: Maximum number of sequences to retain (FIFO eviction).
        """
        self.capacity = capacity
        self.sequences: deque = deque(maxlen=capacity)
        self.losses: deque = deque(maxlen=capacity)
        self.timestamps: deque = deque(maxlen=capacity)
        self.labels: deque = deque(maxlen=capacity)
        self._capacity_warned = False

    def add(
        self,
        sequence: np.ndarray,
        loss: Optional[float] = None,
        timestamp: Optional[float] = None,
        label: Optional[str] = None,
    ) -> None:
        """
        Add a sequence to the replay buffer.

        Args:
            sequence: Array of shape (seq_len, input_dim).
            loss: Reconstruction error in [0, inf); used as sampling priority.
            timestamp: Unix timestamp of observation (defaults to now, UTC).
            label: Optional label such as 'confirmed_anomaly' or 'normal'.
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).timestamp()

        self.sequences.append(sequence)
        self.losses.append(loss if loss is not None else 0.0)
        self.timestamps.append(timestamp)
        self.labels.append(label if label is not None else UNLABELED)

        if len(self.sequences) == self.capacity and not self._capacity_warned:
            self._capacity_warned = True
            logger.warning(
                "Replay buffer at capacity (%d); oldest sequences will be discarded",
                self.capacity,
            )

    def add_batch(self, sequences: np.ndarray) -> None:
        """Add multiple sequences of shape (n, seq_len, input_dim)."""
        for seq in sequences:
            self.add(seq)

    def __len__(self) -> int:
        """Number of sequences currently stored."""
        return len(self.sequences)

    def sample(self, batch_size: int = ONLINE_BATCH_SIZE) -> np.ndarray:
        """
        Sample a random batch with uniform probability (no replacement).

        Returns all stored sequences if fewer than batch_size are available.

        Returns:
            Array of shape (<=batch_size, seq_len, input_dim).
        """
        if len(self.sequences) < batch_size:
            return np.array(list(self.sequences))

        indices = np.random.choice(len(self.sequences), batch_size, replace=False)
        return np.array([self.sequences[i] for i in indices])

    def sample_prioritized(
        self,
        batch_size: int = ONLINE_BATCH_SIZE,
        alpha: float = PRIORITY_ALPHA,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Prioritized experience replay: high-loss sequences sampled more often.

        Args:
            batch_size: Number of sequences to sample (with replacement).
            alpha: Priority exponent in [0, 1]; 0 = uniform, 1 = full priority.

        Returns:
            Tuple of (batch, weights):
              - batch: (batch_size, seq_len, input_dim)
              - weights: (batch_size,) importance weights, normalized to max 1,
                to be multiplied into per-sample losses to de-bias gradients.

        Raises:
            ValueError: If the buffer is empty.
        """
        if len(self.sequences) == 0:
            raise ValueError("Replay buffer is empty")

        losses = np.array(list(self.losses), dtype=np.float64)
        priorities = (losses + NUMERICAL_EPSILON) ** alpha
        probabilities = priorities / priorities.sum()

        indices = np.random.choice(
            len(self.sequences), size=batch_size, replace=True, p=probabilities
        )
        batch = np.array([self.sequences[i] for i in indices])

        # Importance-sampling weights: w_i = (N * P(i))^-beta, normalized so
        # the largest possible weight is 1 (standard PER normalization).
        n = len(self.sequences)
        max_weight = (1.0 / (n * probabilities.min())) ** IMPORTANCE_WEIGHT_BETA
        weights = (1.0 / (n * probabilities[indices])) ** IMPORTANCE_WEIGHT_BETA
        weights /= max_weight

        return batch, weights

    def sample_by_label(
        self, label: str, batch_size: int = ONLINE_BATCH_SIZE
    ) -> np.ndarray:
        """
        Sample sequences with a specific label (without replacement).

        Raises:
            ValueError: If no sequences carry the requested label.
        """
        indices = [i for i, lbl in enumerate(self.labels) if lbl == label]
        if not indices:
            raise ValueError(f"No sequences with label '{label}'")

        if len(indices) <= batch_size:
            selected = indices
        else:
            selected = np.random.choice(indices, batch_size, replace=False)

        return np.array([self.sequences[i] for i in selected])

    def get_statistics(self) -> Dict[str, Any]:
        """
        Return buffer statistics: size, fill ratio, loss stats, label counts.
        """
        if len(self.sequences) == 0:
            return {"size": 0, "warning": "Empty buffer"}

        losses = np.array(list(self.losses), dtype=np.float64)
        return {
            "size": len(self.sequences),
            "capacity": self.capacity,
            "fill_ratio": len(self.sequences) / self.capacity,
            "loss_stats": {
                "min": float(losses.min()),
                "max": float(losses.max()),
                "mean": float(losses.mean()),
                "std": float(losses.std()),
            },
            "label_distribution": dict(Counter(self.labels)),
        }

    def clear(self) -> None:
        """Remove all stored sequences and metadata."""
        self.sequences.clear()
        self.losses.clear()
        self.timestamps.clear()
        self.labels.clear()
        self._capacity_warned = False


class AnomalyBuffer(ReplayBuffer):
    """Specialized buffer for anomalous sequences, tracking anomaly scores."""

    def __init__(self, capacity: int = ANOMALY_BUFFER_CAPACITY) -> None:
        super().__init__(capacity)
        self.anomaly_scores: deque = deque(maxlen=capacity)

    def add(
        self,
        sequence: np.ndarray,
        loss: Optional[float] = None,
        timestamp: Optional[float] = None,
        label: Optional[str] = None,
    ) -> None:
        """Add one sequence, keeping ``anomaly_scores`` index-aligned.

        Overriding the base ``add`` (rather than only appending in
        ``add_anomaly``) means a caller mixing plain ``add`` with
        ``add_anomaly`` can never desync the parallel deque — the score
        defaults to ``loss`` (0.0 when unlabeled).
        """
        super().add(sequence, loss=loss, timestamp=timestamp, label=label)
        self.anomaly_scores.append(loss if loss is not None else 0.0)

    def add_anomaly(
        self,
        sequence: np.ndarray,
        anomaly_score: float,
        is_true_anomaly: Optional[bool] = None,
    ) -> None:
        """
        Add a sequence flagged as anomalous.

        Label semantics (tri-state on is_true_anomaly):
          - None  -> 'suspected_anomaly' (no human feedback yet)
          - True  -> 'confirmed_anomaly'
          - False -> 'false_positive'
        """
        if is_true_anomaly is None:
            label = "suspected_anomaly"
        elif is_true_anomaly:
            label = "confirmed_anomaly"
        else:
            label = "false_positive"

        self.add(sequence, loss=anomaly_score, label=label)


class SeasonalBuffer(ReplayBuffer):
    """Buffer organized by season, enabling seasonal baseline sampling."""

    _UNTAGGED_SEASON = "__untagged__"

    def __init__(self, capacity: int = REPLAY_BUFFER_CAPACITY) -> None:
        super().__init__(capacity)
        self.seasons: deque = deque(maxlen=capacity)

    def add(
        self,
        sequence: np.ndarray,
        loss: Optional[float] = None,
        timestamp: Optional[float] = None,
        label: Optional[str] = None,
    ) -> None:
        """Add one sequence, keeping ``seasons`` index-aligned.

        A plain ``add`` (no season) records a sentinel season so mixing it
        with ``add_seasonal`` cannot misalign the parallel deque and make
        ``sample_season`` return sequences from the wrong season.
        """
        super().add(sequence, loss=loss, timestamp=timestamp, label=label)
        self.seasons.append(self._UNTAGGED_SEASON)

    def add_seasonal(self, sequence: np.ndarray, season: str, **kwargs: Any) -> None:
        """Add a sequence tagged with a season (e.g. 'spring', 'summer')."""
        self.add(sequence, **kwargs)
        self.seasons[-1] = season

    def sample_season(
        self, season: str, batch_size: int = ONLINE_BATCH_SIZE
    ) -> Optional[np.ndarray]:
        """
        Sample sequences from a specific season (without replacement).

        Returns:
            Array of shape (<=batch_size, seq_len, input_dim), or None if no
            sequences are stored for the requested season.
        """
        indices = [i for i, s in enumerate(self.seasons) if s == season]
        if not indices:
            return None

        selected = np.random.choice(
            indices, min(batch_size, len(indices)), replace=False
        )
        return np.array([self.sequences[i] for i in selected])
