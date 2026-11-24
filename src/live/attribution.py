"""Per-channel reconstruction-error attribution for live anomaly scoring.

Provides:
- ``forward_pass_sync``: single LSTM-VAE forward pass returning both the
  scaled anomaly score and per-channel MSE (called via ``asyncio.to_thread``).
  Since ADR-014 A6 it is ``ModelScorer`` plus the core's ``STREAM_PROFILE``
  scale; this module holds no loss maths of its own.
- ``compute_channel_attribution``: derives affected channels and a
  one-sentence reasoning from per-channel reconstruction errors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

import numpy as np

from src.config import INPUT_DIM, SENSOR_NAMES
from src.models.inference import ModelScorer
from src.models.scoring import STREAM_PROFILE
from src.models.scoring.lstm import scaled_scores

if TYPE_CHECKING:
    from src.models.inference import AnomalyDetector

# Channel order matching the model's input_dim layout.
CHANNEL_ORDER: List[str] = list(SENSOR_NAMES[:INPUT_DIM])

# Minimum contribution fraction for a channel to be reported as "affected".
_AFFECTED_CONTRIBUTION_FLOOR: float = 0.20


def forward_pass_sync(
    detector: AnomalyDetector,
    normalized_window: np.ndarray,
) -> Tuple[float, np.ndarray]:
    """Single synchronous forward pass → ``(scaled_score, per_channel_mse)``.

    The pass is the core ``ModelScorer`` on a batch of one; the scale is
    the core's ``STREAM_PROFILE`` policy (``raw / ANOMALY_SCALING_FACTOR``
    clipped to [0, 1], OQ4 unchanged). Raises ``ValueError`` on a NaN/Inf
    window (``ModelScorer`` validates); the caller logs and skips the tick.
    """
    scores = ModelScorer.from_detector(detector).predict_with_channel_errors(
        normalized_window[np.newaxis, ...].astype(np.float32)
    )
    return scaled_scores(scores, STREAM_PROFILE)[0], scores.per_channel[0]


def compute_channel_attribution(
    per_channel_errors: np.ndarray,
) -> Tuple[List[str], str]:
    """Derive affected channels and a one-sentence reasoning template.

    Channels contributing >= 20 % of total reconstruction error (or at
    minimum the single top channel) are reported as affected.
    """
    total = float(per_channel_errors.sum())
    if total < 1e-9:
        return [], "All channels within normal range"

    contributions = per_channel_errors / total
    sorted_idx = np.argsort(contributions)[::-1]

    affected: List[str] = []
    for idx in sorted_idx:
        if contributions[idx] >= _AFFECTED_CONTRIBUTION_FLOOR or not affected:
            affected.append(CHANNEL_ORDER[int(idx)])

    top_name = CHANNEL_ORDER[int(sorted_idx[0])]
    top_pct = contributions[sorted_idx[0]] * 100
    reasoning = (
        f"Anomaly primarily driven by {top_name} "
        f"({top_pct:.0f}% of reconstruction error)"
    )
    if len(affected) > 1:
        reasoning += f"; {', '.join(affected[1:])} also elevated"
    return affected, reasoning
