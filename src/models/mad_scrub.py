"""Pass-1 MAD robust Z-score scrubbing for cold-start bootstrap.

Uses the Iglewicz–Hoaglin robust Z-score:

    MAD = median(|x_t − median(X)|)
    z_t = 0.6745 · (x_t − median(X)) / MAD

Rows where any channel has |z| > ``BOOTSTRAP_MAD_Z_THRESHOLD`` (default 3.5)
are flagged for culling. When MAD = 0 (constant / quantized channel) that
channel contributes zeros — no culls from that channel alone.
"""

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd

from src.config import BOOTSTRAP_MAD_Z_THRESHOLD

logger = logging.getLogger(__name__)

# Φ⁻¹(0.75) — normal-consistency constant for MAD ↔ σ.
_MAD_CONSISTENCY: float = 0.6745
_CULL_WARN_FRACTION: float = 0.10


def mad_z_scores(values: np.ndarray) -> np.ndarray:
    """Compute per-sample robust Z-scores for a 1-D series.

    NaNs are ignored when computing median/MAD and produce NaN z-scores.
    MAD = 0 → all finite samples get z = 0 (no culls from this channel).
    """
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape, np.nan, dtype=float)
    finite = np.isfinite(arr)
    if not finite.any():
        return out

    subset = arr[finite]
    med = float(np.median(subset))
    mad = float(np.median(np.abs(subset - med)))
    if mad == 0.0:
        out[finite] = 0.0
        return out

    out[finite] = _MAD_CONSISTENCY * (subset - med) / mad
    return out


def compute_mad_flags(
    df: pd.DataFrame,
    columns: Iterable[str] | None = None,
    *,
    threshold: float = BOOTSTRAP_MAD_Z_THRESHOLD,
) -> pd.Series:
    """Flag rows where any channel exceeds the robust Z-score threshold.

    Parameters
    ----------
    df:
        Telemetry frame (index preserved on the returned Series).
    columns:
        Sensor columns to evaluate. Defaults to all numeric columns.
    threshold:
        Absolute robust Z cutoff (default from config, typically 3.5).

    Returns
    -------
    pd.Series[bool]
        ``True`` = cull this row.
    """
    cols = list(columns) if columns is not None else [
        c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])
    ]
    flags = pd.Series(False, index=df.index, dtype=bool)
    if not cols or df.empty:
        return flags

    for col in cols:
        if col not in df.columns:
            continue
        z = mad_z_scores(df[col].to_numpy(dtype=float))
        channel_flag = np.isfinite(z) & (np.abs(z) > threshold)
        flags = flags | pd.Series(channel_flag, index=df.index)

    cull_frac = float(flags.mean()) if len(flags) else 0.0
    logger.info(
        "MAD scrub flagged %.1f%% of rows (threshold=%.2f, channels=%s)",
        100.0 * cull_frac,
        threshold,
        cols,
    )
    if cull_frac > _CULL_WARN_FRACTION:
        logger.warning(
            "MAD scrub cull fraction %.1f%% exceeds %.0f%% — check sensor quality "
            "or raise BOOTSTRAP_MAD_Z_THRESHOLD",
            100.0 * cull_frac,
            100.0 * _CULL_WARN_FRACTION,
        )
    return flags
