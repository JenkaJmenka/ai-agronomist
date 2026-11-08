"""Turn MAD-scrub channel reports into logs and tenant-safe warnings.

Split out of ``mad_scrub.py`` to keep that module inside the 300-line limit,
and because this is where the trust boundary sits: everything here decides
what an operator reads in the log versus what a tenant reads in an API
response. See ``pass1_warning`` for the contract.
"""

from __future__ import annotations

import logging

import pandas as pd

from src.config import BOOTSTRAP_CHANNEL_CULL_WARN_FRACTION
from src.models.mad_scrub_report import MadChannelReport, MadScrubOptions
from src.models.pass1_warning import WARNING_CODE_CHANNEL_CULL_HIGH, Pass1Warning

logger = logging.getLogger(__name__)


def build_mad_warnings(
    flags: pd.Series,
    reports: tuple[MadChannelReport, ...],
    options: MadScrubOptions,
) -> tuple[Pass1Warning, ...]:
    """Log the scrub outcome and return the tenant-safe warnings."""
    cull_frac = float(flags.mean()) if len(flags) else 0.0
    scrubbed = [r.column for r in reports if not r.is_skipped]
    logger.info(
        "MAD scrub flagged %.2f%% of rows (threshold=%.2f, scrubbed=%s)",
        100.0 * cull_frac,
        options.threshold,
        scrubbed,
    )

    warnings: list[Pass1Warning] = []
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
        warnings.append(_channel_cull_warning(report))
    return tuple(warnings)


def _channel_cull_warning(report: MadChannelReport) -> Pass1Warning:
    """Log the engineering detail for a high-cull channel; return the safe half.

    The remediation names environment variables and prints the mad/median pair
    that proves raising the z threshold cannot help — all correct for an
    operator reading logs, none of it fit for an API response, since this text
    used to reach the tenant and `web/openapi.json`.
    """
    logger.warning(
        "MAD scrub culled %.1f%% of channel '%s' (%d/%d finite values), above the "
        "per-channel limit of %.0f%%. Check the sensor for a step change or a "
        "second operating mode; if the channel is genuinely multi-modal, add it "
        "to BOOTSTRAP_MAD_EXCLUDED_CHANNELS. Raising BOOTSTRAP_MAD_Z_THRESHOLD "
        "will NOT help when MAD scales with the median (mad=%.6g, median=%.6g).",
        100.0 * report.cull_fraction,
        report.column,
        report.cull_rows,
        report.finite_rows,
        100.0 * BOOTSTRAP_CHANNEL_CULL_WARN_FRACTION,
        report.mad,
        report.median,
    )
    return Pass1Warning(
        code=WARNING_CODE_CHANNEL_CULL_HIGH,
        message=(
            f"Sensor channel '{report.column}' had "
            f"{100.0 * report.cull_fraction:.1f}% of its readings "
            f"({report.cull_rows} of {report.finite_rows}) removed as outliers "
            f"during data cleaning, above the "
            f"{100.0 * BOOTSTRAP_CHANNEL_CULL_WARN_FRACTION:.0f}% level we flag "
            f"for review. A channel that far from its own usual range normally "
            f"means the sensor has a step change or two distinct operating "
            f"modes."
        ),
    )
