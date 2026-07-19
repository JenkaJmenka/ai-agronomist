"""Per-channel reconstruction-error attribution for live anomaly scoring.

Provides:
- ``forward_pass_sync``: single LSTM-VAE forward pass returning both the
  scaled anomaly score and per-channel MSE (called via ``asyncio.to_thread``).
- ``compute_channel_attribution``: derives affected channels and a
  one-sentence reasoning from per-channel reconstruction errors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

import numpy as np

from src.config import ANOMALY_SCALING_FACTOR, INPUT_DIM, SENSOR_NAMES

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

    Uses the same MSE math as ``AnomalyDetector.predict_batch_enriched``
    but also returns per-channel reconstruction errors in **one** pass.
    The ``torch`` import is deferred so callers can mock this function
    without importing torch.
    """
    import torch

    batch = normalized_window[np.newaxis, ...].astype(np.float32)
    with torch.no_grad():
        tensor = torch.from_numpy(batch).float()
        reconstructed, _, _, _ = detector.model(tensor)
        error = (tensor - reconstructed) ** 2
        raw_score = float(error.mean(dim=[1, 2]).item())
        per_channel = error.mean(dim=1).squeeze(0).numpy()

    scaled = float(np.clip(raw_score / ANOMALY_SCALING_FACTOR, 0.0, 1.0))
    return scaled, per_channel


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
