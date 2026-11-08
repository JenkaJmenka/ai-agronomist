"""Pass-1 MAD robust Z-score scrubbing for cold-start bootstrap.

Iglewicz–Hoaglin robust Z-score: ``MAD = median(|x_t − median(X)|)`` and
``z_t = 0.6745 · (x_t − median(X)) / MAD``. Rows where any channel has
|z| > ``BOOTSTRAP_MAD_Z_THRESHOLD`` (default 3.5) are flagged for culling.

That statistic assumes a **unimodal** channel and inverts silently when the
assumption fails, so two guards decide whether a channel is scrubbed at all:

* **A named exclusion list** (``BOOTSTRAP_MAD_EXCLUDED_CHANNELS``) for channels
  that are structurally multi-modal. ``light`` is the motivating case: when its
  night zeros sit near half the window, every zero deviates from the median by
  exactly the median, so ``MAD == median`` identically and the cull boundary
  collapses to ``median · (1 + z/0.6745)`` — 6.19 · median at z = 3.5, i.e.
  8.36 W/m² on the tenant_1 corpus, culling 47.4% of all rows and 100% of
  midday (BACKLOG-14). Raising the threshold cannot repair it: MAD scales
  *with* the median, so full sun at 1000 W/m² scores only z ≈ 499 and even
  z > 100 still culls 24.0% of that corpus.
* **A generic degeneracy test**: skip a channel when one value takes more than
  ``BOOTSTRAP_MAD_MAX_MODAL_SHARE`` of its finite samples, or when MAD is at or
  below ``BOOTSTRAP_MAD_MIN_SPREAD``.

Neither guard subsumes the other: ``light`` is 44.0% zeros on the tenant_1
corpus, so the generic test alone would catch it there, but 30.6% on
``tests/fixtures/native_july_clearsky.csv``, where only the exclusion list does.

A channel skipped for any reason contributes no flags, and the reason is
reported on :class:`MadScrubResult` as well as logged. Result shapes live in
``mad_scrub_report``; the Pass-1 contract is in
``docs/handoff/backlog14_pass1_observability.md``.
"""

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd

from src.config import (
    BOOTSTRAP_CHANNEL_CULL_WARN_FRACTION,
    BOOTSTRAP_MAD_MIN_SPREAD,
    BOOTSTRAP_MAD_Z_THRESHOLD,
)
from src.models.mad_scrub_report import (
    SKIP_REASON_EXCLUDED,
    SKIP_REASON_MISSING,
    SKIP_REASON_MODAL_SHARE,
    SKIP_REASON_NO_FINITE,
    SKIP_REASON_NO_SPREAD,
    MadChannelReport,
    MadScrubOptions,
    MadScrubResult,
)

logger = logging.getLogger(__name__)

# Φ⁻¹(0.75) — normal-consistency constant for MAD ↔ σ.
_MAD_CONSISTENCY: float = 0.6745


def modal_share(values: np.ndarray) -> float:
    """Share of finite samples taken by the commonest value; 0.0 if none finite."""
    arr = np.asarray(values, dtype=float)
    subset = arr[np.isfinite(arr)]
    if subset.size == 0:
        return 0.0
    _, counts = np.unique(subset, return_counts=True)
    return float(counts.max() / subset.size)


def mad_z_scores(
    values: np.ndarray,
    *,
    min_spread: float = BOOTSTRAP_MAD_MIN_SPREAD,
) -> np.ndarray:
    """Compute per-sample robust Z-scores for a 1-D series.

    NaNs are ignored when computing median/MAD and produce NaN z-scores.
    MAD at or below ``min_spread`` (which includes MAD = 0 for a constant or
    quantized channel) → all finite samples get z = 0, i.e. no culls from this
    channel. The guard is kept here as well as in :func:`compute_mad_scrub` so
    a direct caller can never divide by zero.
    """
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape, np.nan, dtype=float)
    finite = np.isfinite(arr)
    if not finite.any():
        return out

    subset = arr[finite]
    med = float(np.median(subset))
    mad = float(np.median(np.abs(subset - med)))
    if mad <= min_spread:
        out[finite] = 0.0
        return out

    out[finite] = _MAD_CONSISTENCY * (subset - med) / mad
    return out


def _skip_reason(
    column: str,
    values: np.ndarray,
    options: MadScrubOptions,
) -> tuple[str | None, float, float, float]:
    """Return ``(reason, median, mad, modal_share)``; reason ``None`` = scrub it."""
    if column in options.excluded_columns:
        return SKIP_REASON_EXCLUDED, float("nan"), float("nan"), float("nan")

    subset = values[np.isfinite(values)]
    if subset.size == 0:
        return SKIP_REASON_NO_FINITE, float("nan"), float("nan"), float("nan")

    med = float(np.median(subset))
    mad = float(np.median(np.abs(subset - med)))
    share = modal_share(subset)

    if share > options.max_modal_share:
        return SKIP_REASON_MODAL_SHARE, med, mad, share
    if mad <= options.min_spread:
        return SKIP_REASON_NO_SPREAD, med, mad, share
    return None, med, mad, share


def _evaluate_channel(
    series: pd.Series,
    options: MadScrubOptions,
) -> tuple[MadChannelReport, np.ndarray | None]:
    """Measure one channel and return its report plus its boolean cull mask.

    The mask is ``None`` when the channel was skipped.
    """
    column = str(series.name)
    values = series.to_numpy(dtype=float)
    finite_rows = int(np.isfinite(values).sum())
    reason, med, mad, share = _skip_reason(column, values, options)

    mask: np.ndarray | None = None
    cull_rows = 0
    if reason is None:
        z = mad_z_scores(values, min_spread=options.min_spread)
        mask = np.isfinite(z) & (np.abs(z) > options.threshold)
        cull_rows = int(mask.sum())

    report = MadChannelReport(
        column=column,
        is_skipped=reason is not None,
        skip_reason=reason,
        median=med,
        mad=mad,
        modal_share=share,
        finite_rows=finite_rows,
        cull_rows=cull_rows,
        cull_fraction=cull_rows / finite_rows if finite_rows else 0.0,
    )
    return report, mask


def _missing_column_report(column: str) -> MadChannelReport:
    """Report for a requested column that is not in the frame at all."""
    return MadChannelReport(
        column=column,
        is_skipped=True,
        skip_reason=SKIP_REASON_MISSING,
        median=float("nan"),
        mad=float("nan"),
        modal_share=float("nan"),
        finite_rows=0,
        cull_rows=0,
        cull_fraction=0.0,
    )


def _resolve_columns(df: pd.DataFrame, columns: Iterable[str] | None) -> list[str]:
    if columns is not None:
        return list(columns)
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def compute_mad_scrub(
    df: pd.DataFrame,
    columns: Iterable[str] | None = None,
    *,
    options: MadScrubOptions | None = None,
) -> MadScrubResult:
    """Scrub *df* and return row flags with the per-channel evidence.

    Parameters
    ----------
    df:
        Telemetry frame (index preserved on the returned flag Series).
    columns:
        Sensor columns to consider. Defaults to all numeric columns. A column
        named here but absent from *df* is reported as skipped, not ignored.
    options:
        Threshold / exclusion / degeneracy tunables. Defaults to ``src.config``.

    Returns
    -------
    MadScrubResult
        ``flags`` — ``True`` = cull this row; ``channels`` — one report per
        requested column; ``warnings`` — operator-facing strings, *returned*
        and not merely logged: the log-only warning this replaced fired on
        every tenant_1 run for three months unnoticed.
    """
    opts = options or MadScrubOptions()
    cols = _resolve_columns(df, columns)
    flags = pd.Series(False, index=df.index, dtype=bool)
    if not cols or df.empty:
        return MadScrubResult(flags=flags)

    reports: list[MadChannelReport] = []
    for col in cols:
        if col not in df.columns:
            reports.append(_missing_column_report(col))
            continue
        report, mask = _evaluate_channel(df[col], opts)
        reports.append(report)
        if mask is not None:
            flags = flags | pd.Series(mask, index=df.index)

    return MadScrubResult(
        flags=flags,
        channels=tuple(reports),
        warnings=_build_warnings(flags, tuple(reports), opts),
    )


def _build_warnings(
    flags: pd.Series,
    reports: tuple[MadChannelReport, ...],
    options: MadScrubOptions,
) -> tuple[str, ...]:
    """Log the scrub outcome and return the operator-facing warnings."""
    cull_frac = float(flags.mean()) if len(flags) else 0.0
    scrubbed = [r.column for r in reports if not r.is_skipped]
    logger.info(
        "MAD scrub flagged %.2f%% of rows (threshold=%.2f, scrubbed=%s)",
        100.0 * cull_frac,
        options.threshold,
        scrubbed,
    )

    warnings: list[str] = []
    for report in reports:
        if report.is_skipped:
            logger.info(
                "MAD scrub skipped channel '%s' (%s; modal_share=%.4f, mad=%.6g)",
                report.column,
                report.skip_reason,
                report.modal_share,
                report.mad,
            )
            continue
        if report.cull_fraction <= BOOTSTRAP_CHANNEL_CULL_WARN_FRACTION:
            continue
        message = (
            f"MAD scrub culled {100.0 * report.cull_fraction:.1f}% of channel "
            f"'{report.column}' ({report.cull_rows}/{report.finite_rows} finite "
            f"values), above the per-channel limit of "
            f"{100.0 * BOOTSTRAP_CHANNEL_CULL_WARN_FRACTION:.0f}%. Check the "
            f"sensor for a step change or a second operating mode; if the "
            f"channel is genuinely multi-modal, add it to "
            f"BOOTSTRAP_MAD_EXCLUDED_CHANNELS. Raising BOOTSTRAP_MAD_Z_THRESHOLD "
            f"will NOT help when MAD scales with the median (mad="
            f"{report.mad:.6g}, median={report.median:.6g})."
        )
        logger.warning("%s", message)
        warnings.append(message)
    return tuple(warnings)


def compute_mad_flags(
    df: pd.DataFrame,
    columns: Iterable[str] | None = None,
    *,
    threshold: float = BOOTSTRAP_MAD_Z_THRESHOLD,
) -> pd.Series:
    """Flag rows where any eligible channel exceeds the robust Z-score threshold.

    Thin wrapper over :func:`compute_mad_scrub` for callers that need only the
    row mask (``True`` = cull). Prefer ``compute_mad_scrub`` when the
    per-channel evidence is wanted — the flags alone cannot say which channel
    drove a cull.
    """
    return compute_mad_scrub(
        df, columns, options=MadScrubOptions(threshold=threshold)
    ).flags
