"""Discard guard for cold-start Pass 1.

Pass 1 flags rows as anomalous before any model is trained, so an over-eager
filter does not fail loudly — it silently narrows what "normal" means. On the
tenant_1 corpus it discarded 50.5% of a year of greenhouse history and the only
symptom was a `pass1_anomaly_rows` count nobody was reading (BACKLOG-14).

This module turns that into a measurement and a gate:

* **The denominator is rows carrying data**, not rows in the frame. A mostly
  empty upload once showed a 44.6% cull of real readings as 3.84% of a
  91.4%-NaN frame. Both numerators and both denominators are recorded so the
  two can never be confused again.
* **At or above the warn fraction** Pass 1 returns a warning; warnings are
  *returned* on the result, not only logged, because a log-only warning in the
  MAD scrub fired on every tenant_1 run from July to October unseen.
* **At or above the fail fraction** the caller raises
  :class:`ExcessivePass1DiscardError` and training stops.

All fractions here are fractions in [0, 1], never percentages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from src.config import (
    BOOTSTRAP_CULL_FAIL_FRACTION,
    BOOTSTRAP_CULL_WARN_FRACTION,
)
from src.models.pass1_warning import (
    WARNING_CODE_DISCARD_EXCESSIVE,
    WARNING_CODE_DISCARD_HIGH,
    Pass1Warning,
)

logger = logging.getLogger(__name__)

DISCARD_STATUS_OK: str = "ok"
DISCARD_STATUS_WARN: str = "warn"
DISCARD_STATUS_FAIL: str = "fail"


class ExcessivePass1DiscardError(ValueError):
    """Pass 1 discarded too large a share of the rows carrying data.

    Deliberately a ``ValueError`` subclass: ``bootstrap.InsufficientDataError``
    *is* ``ValueError``, so ``src/api/routes/bootstrap.py``'s
    ``except InsufficientDataError`` keeps mapping this to HTTP 400 with no
    change on the API side, while the distinct type still lets a caller tell
    "too much was thrown away" from "too little was left".
    """


@dataclass(frozen=True)
class Pass1DiscardStats:
    """How much Pass 1 threw away, against both denominators.

    ``frame_*`` counts every row in the resampled frame. ``data_*`` counts only
    rows carrying at least one finite sensor value, and is the denominator the
    warn/fail thresholds are applied to.
    """

    frame_rows: int = 0
    frame_discarded: int = 0
    frame_discard_fraction: float = 0.0
    data_rows: int = 0
    data_discarded: int = 0
    data_discard_fraction: float = 0.0
    status: str = DISCARD_STATUS_OK
    warnings: tuple[Pass1Warning, ...] = ()

    @property
    def is_failing(self) -> bool:
        return self.status == DISCARD_STATUS_FAIL

    def summary(self) -> str:
        """One-line account of both numerators and both denominators."""
        return (
            f"Pass 1 discarded {self.data_discarded}/{self.data_rows} rows "
            f"carrying data ({100.0 * self.data_discard_fraction:.2f}%); "
            f"{self.frame_discarded}/{self.frame_rows} of the frame "
            f"({100.0 * self.frame_discard_fraction:.2f}%)"
        )


def _fraction(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def evaluate_pass1_discard(
    df: pd.DataFrame,
    discard_flags: pd.Series,
    sensor_columns: Sequence[str],
) -> Pass1DiscardStats:
    """Measure Pass 1's discard rate and decide ok / warn / fail.

    Parameters
    ----------
    df:
        The resampled Pass-1 frame.
    discard_flags:
        ``True`` where the row was discarded (physics | MAD | IsolationForest).
    sensor_columns:
        Columns that count as "carrying data" when any one of them is finite.

    Returns
    -------
    Pass1DiscardStats
        Never raises. The caller raises :class:`ExcessivePass1DiscardError` on
        ``is_failing``, so the measurement stays separable from the policy.
    """
    present = [col for col in sensor_columns if col in df.columns]
    frame_rows = len(df)
    frame_discarded = int(discard_flags.sum()) if frame_rows else 0

    if present:
        carrying = df[present].notna().any(axis=1)
    else:
        carrying = pd.Series(False, index=df.index, dtype=bool)
    data_rows = int(carrying.sum())
    data_discarded = int((discard_flags & carrying).sum()) if frame_rows else 0
    data_fraction = _fraction(data_discarded, data_rows)

    stats = Pass1DiscardStats(
        frame_rows=frame_rows,
        frame_discarded=frame_discarded,
        frame_discard_fraction=_fraction(frame_discarded, frame_rows),
        data_rows=data_rows,
        data_discarded=data_discarded,
        data_discard_fraction=data_fraction,
        status=_status_for(data_fraction),
    )
    if stats.status == DISCARD_STATUS_OK:
        return stats
    return _with_warning(stats)


def _status_for(data_discard_fraction: float) -> str:
    if data_discard_fraction >= BOOTSTRAP_CULL_FAIL_FRACTION:
        return DISCARD_STATUS_FAIL
    if data_discard_fraction >= BOOTSTRAP_CULL_WARN_FRACTION:
        return DISCARD_STATUS_WARN
    return DISCARD_STATUS_OK


def _with_warning(stats: Pass1DiscardStats) -> Pass1DiscardStats:
    """Log the engineering detail for a warn/fail status; attach the safe half.

    The log line keeps both numerators and both denominators and names the
    environment variables that set the limits. The returned ``message`` cannot:
    it reaches the tenant both in ``pass1_warnings`` and — on a fail — as the
    HTTP 400 body, because ``ColdStartBootstrap.run`` raises with this text and
    ``src/api/routes/bootstrap.py`` passes ``str(exc)`` straight to the caller.
    """
    limit = (
        BOOTSTRAP_CULL_FAIL_FRACTION
        if stats.is_failing
        else BOOTSTRAP_CULL_WARN_FRACTION
    )
    logger.warning(
        "%s — %s of %.0f%% (%s). The model would learn 'normal' from the "
        "remainder, selected by the very filter under suspicion. Inspect the "
        "per-channel MAD cull fractions and the IsolationForest flag rate on "
        "the Pass-1 result before retraining.",
        stats.summary(),
        "exceeds the fail limit" if stats.is_failing else "exceeds the warn limit",
        100.0 * limit,
        "BOOTSTRAP_CULL_FAIL_FRACTION"
        if stats.is_failing
        else "BOOTSTRAP_CULL_WARN_FRACTION",
    )
    if stats.is_failing:
        message = (
            f"Data cleaning removed {stats.data_discarded} of {stats.data_rows} "
            f"readings ({100.0 * stats.data_discard_fraction:.1f}%), above the "
            f"{100.0 * limit:.0f}% limit, so training was stopped. Training on "
            f"what remained would have taught the model normal behaviour from "
            f"too small a share of your history. Please check the sensors "
            f"flagged above and upload again."
        )
    else:
        message = (
            f"Data cleaning removed {stats.data_discarded} of {stats.data_rows} "
            f"readings ({100.0 * stats.data_discard_fraction:.1f}%) before "
            f"training, above the {100.0 * limit:.0f}% level we flag for "
            f"review. The model learns normal behaviour only from the readings "
            f"that remain, so a high share narrows how much of your history it "
            f"represents."
        )
    warning = Pass1Warning(
        code=(
            WARNING_CODE_DISCARD_EXCESSIVE
            if stats.is_failing
            else WARNING_CODE_DISCARD_HIGH
        ),
        message=message,
    )
    return Pass1DiscardStats(
        frame_rows=stats.frame_rows,
        frame_discarded=stats.frame_discarded,
        frame_discard_fraction=stats.frame_discard_fraction,
        data_rows=stats.data_rows,
        data_discarded=stats.data_discarded,
        data_discard_fraction=stats.data_discard_fraction,
        status=stats.status,
        warnings=(warning,),
    )
