"""Result and option types for the Pass-1 MAD scrub.

Split out of ``mad_scrub.py`` to keep that module inside the 300-line limit.
The scrub algorithm lives there; this module is data shapes only, so Pass 1's
observability contract (``docs/handoff/backlog14_pass1_observability.md``) can
be read in one place.

**Every fraction in this module is a fraction in [0, 1], never a percentage.**
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.config import (
    BOOTSTRAP_MAD_EXCLUDED_CHANNELS,
    BOOTSTRAP_MAD_MAX_MODAL_SHARE,
    BOOTSTRAP_MAD_MIN_SPREAD,
    BOOTSTRAP_MAD_Z_THRESHOLD,
)

# Skip reasons reported on MadChannelReport.skip_reason.
SKIP_REASON_EXCLUDED: str = "excluded_channel"
SKIP_REASON_MODAL_SHARE: str = "modal_share_above_limit"
SKIP_REASON_NO_SPREAD: str = "mad_at_or_below_min_spread"
SKIP_REASON_NO_FINITE: str = "no_finite_values"
SKIP_REASON_MISSING: str = "column_absent"


@dataclass(frozen=True)
class MadScrubOptions:
    """Tunables for ``compute_mad_scrub``, defaulted from ``src.config``."""

    threshold: float = BOOTSTRAP_MAD_Z_THRESHOLD
    excluded_columns: frozenset = BOOTSTRAP_MAD_EXCLUDED_CHANNELS
    max_modal_share: float = BOOTSTRAP_MAD_MAX_MODAL_SHARE
    min_spread: float = BOOTSTRAP_MAD_MIN_SPREAD


@dataclass(frozen=True)
class MadChannelReport:
    """Per-channel outcome of the MAD scrub.

    ``cull_fraction`` is a fraction of this channel's *finite* values — not of
    the whole frame. ``finite_rows`` is published alongside it so a consumer
    never has to guess the denominator: a 44.6% cull of real readings once
    surfaced as 3.84% because it was divided by a 91.4%-NaN frame.

    ``median``, ``mad`` and ``modal_share`` are NaN when the channel was
    skipped before being measured (an excluded or absent column).
    """

    column: str
    is_skipped: bool
    skip_reason: str | None
    median: float
    mad: float
    modal_share: float
    finite_rows: int
    cull_rows: int
    cull_fraction: float


@dataclass(frozen=True)
class MadScrubResult:
    """Row flags plus the per-channel evidence behind them."""

    flags: pd.Series
    channels: tuple[MadChannelReport, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def cull_fractions(self) -> dict[str, float]:
        """Per-channel cull fraction of that channel's finite values."""
        return {report.column: report.cull_fraction for report in self.channels}

    @property
    def cull_counts(self) -> dict[str, int]:
        """Per-channel absolute cull counts."""
        return {report.column: report.cull_rows for report in self.channels}

    @property
    def finite_counts(self) -> dict[str, int]:
        """Per-channel finite-value counts — the ``cull_fractions`` denominator."""
        return {report.column: report.finite_rows for report in self.channels}

    @property
    def skipped_channels(self) -> dict[str, str]:
        """Skipped channel → reason, for the channels that were not scrubbed."""
        return {
            report.column: report.skip_reason
            for report in self.channels
            if report.is_skipped and report.skip_reason is not None
        }
