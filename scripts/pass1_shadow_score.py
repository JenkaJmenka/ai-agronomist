#!/usr/bin/env python
"""Shadow-score one window with an old and a new cold-start model.

Step 2 of ``docs/runbooks/cold-start-retraining.md``. Trains a new model from a
CSV, scores a window with both the new model and an existing checkpoint, and
prints alert counts, both calibration thresholds and the new ``metrics_json``.

**This script decides nothing and activates nothing.** By design it:

* never writes to ``model_version``, ``model_deployment`` or any table — it
  opens no database session at all;
* never calls ``ModelRegistry``, so it cannot activate or roll back a model;
* never opens a network connection, so it cannot reach Railway;
* writes only to a temporary directory it creates, and to stdout.

Activation is a separate, human step that happens after the user has read this
output and approved it.

Usage
-----
::

    ./venv/bin/python scripts/pass1_shadow_score.py \\
        --csv /path/to/corpus.csv \\
        --window-start 2026-12-01 --window-end 2026-12-31 \\
        [--old-checkpoint /path/to/current.pt] \\
        [--json-out /tmp/shadow.json]

``--window-start``/``--window-end`` are required and deliberately have no
default: the development corpus
``~/Projects/ai-agronomist-data/2026_1d_english_clean_fixed.csv`` **ends
2026-12-31**, so "the last 30 days" of it is not production's last 30 days. A
hardcoded or now-relative window would quietly compare two different periods.
A local run is a rehearsal of the procedure, never a production result.

Omitting ``--old-checkpoint`` scores the new model only, which is still useful
as a dry run of step 2 before the current checkpoint has been fetched.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger("pass1_shadow_score")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.models.bootstrap import (  # noqa: E402
    BootstrapPass1Result,
    ColdStartBootstrap,
    Pass2BootstrapDetector,
)


@dataclass(frozen=True)
class ScoredWindow:
    """What one model made of the window."""

    label: str
    threshold: float
    rows_scored: int
    alerts: int

    @property
    def alert_fraction(self) -> float:
        return self.alerts / self.rows_scored if self.rows_scored else 0.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Shadow-score a window with an old and a new cold-start model.",
    )
    parser.add_argument("--csv", required=True, help="Training corpus CSV.")
    parser.add_argument(
        "--window-start",
        required=True,
        help="Scoring window start, inclusive (ISO date or datetime, UTC).",
    )
    parser.add_argument(
        "--window-end",
        required=True,
        help="Scoring window end, inclusive (ISO date or datetime, UTC).",
    )
    parser.add_argument(
        "--old-checkpoint",
        default=None,
        help="Existing .pt to compare against. Omit to score the new model only.",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Write the full report as JSON to this path (stdout always prints).",
    )
    return parser.parse_args(argv)


def select_window(
    frame: pd.DataFrame,
    window_start: str,
    window_end: str,
) -> pd.DataFrame:
    """Return the rows of *frame* inside the inclusive window.

    Raises on an empty selection rather than reporting zero alerts, which would
    otherwise read as "the new model is quiet" instead of "the window missed".
    """
    start = pd.Timestamp(window_start, tz="UTC")
    end = pd.Timestamp(window_end, tz="UTC")
    if start > end:
        raise ValueError(f"window start {start} is after window end {end}")
    window = frame.loc[(frame.index >= start) & (frame.index <= end)]
    if window.empty:
        raise ValueError(
            f"No rows between {start} and {end}. The corpus spans "
            f"{frame.index.min()} to {frame.index.max()} — check the window, "
            f"and remember the development corpus ends 2026-12-31."
        )
    return window


def score_with(
    detector: Pass2BootstrapDetector,
    window: pd.DataFrame,
    label: str,
) -> ScoredWindow:
    """Score *window* and count alerts, without persisting anything."""
    results = detector.infer(window)
    return ScoredWindow(
        label=label,
        threshold=detector.calibration_threshold,
        rows_scored=len(results),
        alerts=sum(1 for row in results if row.is_anomaly),
    )


def pass1_metrics(pass1: BootstrapPass1Result) -> dict[str, object]:
    """The Pass-1 half of what `metrics_json` will carry, for eyeballing.

    Mirrors ``cold_start_training._build_pass1_metrics`` in content but is
    deliberately a separate, read-only shaping: this script must not import the
    API service layer, and a drift between the two is a reason to re-read both
    rather than to trust this one.
    """
    stats = pass1.discard_stats
    return {
        "pass1_clean_rows": len(pass1.clean_df),
        "pass1_anomaly_rows": len(pass1.anomaly_df),
        "pass1_if_flag_fraction": pass1.if_flag_fraction,
        "pass1_mad_cull_fractions": pass1.mad_channel_cull_fractions,
        "pass1_mad_skipped_channels": pass1.mad_skipped_channels,
        "pass1_discard": {
            "frame_rows": stats.frame_rows,
            "frame_discarded": stats.frame_discarded,
            "frame_discard_fraction": stats.frame_discard_fraction,
            "data_rows": stats.data_rows,
            "data_discarded": stats.data_discarded,
            "data_discard_fraction": stats.data_discard_fraction,
            "status": stats.status,
        },
        "pass1_warnings": [warning.as_dict() for warning in pass1.warnings],
    }


def train_new_model(csv_path: str, workdir: str) -> tuple[Pass2BootstrapDetector, BootstrapPass1Result]:
    """Run the real Pass 1 + Pass 2 into *workdir*. Nothing is registered."""
    clean_path = os.path.join(workdir, "shadow_clean.csv")
    anomaly_path = os.path.join(workdir, "shadow_anomalies.csv")
    pass1 = ColdStartBootstrap(
        clean_path=clean_path, anomaly_path=anomaly_path
    ).run(csv_path)
    detector = Pass2BootstrapDetector()
    detector.train(clean_path)
    if detector.trained_model is None:
        raise ValueError(
            "Pass 2 trained no model — too few clean rows after Pass 1 filtering."
        )
    return detector, pass1


def build_report(args: argparse.Namespace, workdir: str) -> dict[str, object]:
    """Train, score, and assemble the report. No side effects outside *workdir*."""
    detector, pass1 = train_new_model(args.csv, workdir)
    frame = pd.concat([pass1.clean_df, pass1.anomaly_df]).sort_index()
    window = select_window(frame, args.window_start, args.window_end)

    scored = [score_with(detector, window, "new")]
    if args.old_checkpoint:
        # Both sides are given the NEW model's adaptive ranges on purpose: the
        # checkpoint does not carry the ranges its own training used, so letting
        # the two differ would measure the normalization, not the models. See
        # Pass2BootstrapDetector.from_checkpoint.
        old = Pass2BootstrapDetector.from_checkpoint(
            args.old_checkpoint, ranges=detector.sensor_ranges
        )
        scored.append(score_with(old, window, "old"))

    return {
        "corpus": os.path.abspath(args.csv),
        "corpus_span": [str(frame.index.min()), str(frame.index.max())],
        "window": [args.window_start, args.window_end],
        "window_rows": len(window),
        "old_checkpoint": args.old_checkpoint,
        "normalization_note": (
            "Both models scored with the new model's adaptive sensor ranges; "
            "checkpoints do not persist their training-time ranges."
        ),
        "scored": [
            {
                "label": entry.label,
                "calibration_threshold": entry.threshold,
                "rows_scored": entry.rows_scored,
                "alerts": entry.alerts,
                "alert_fraction": entry.alert_fraction,
            }
            for entry in scored
        ],
        "new_model_metrics_json": pass1_metrics(pass1),
    }


def print_report(report: dict[str, object]) -> None:
    """Human summary first, then the machine-readable block."""
    print("=" * 72)
    print("SHADOW SCORE — NOTHING WAS ACTIVATED, NOTHING WAS WRITTEN TO THE DB")
    print("=" * 72)
    print(f"corpus      : {report['corpus']}")
    print(f"corpus span : {report['corpus_span'][0]} .. {report['corpus_span'][1]}")
    print(f"window      : {report['window'][0]} .. {report['window'][1]}"
          f"  ({report['window_rows']} rows)")
    print(f"old ckpt    : {report['old_checkpoint'] or '(none — new model only)'}")
    print()
    print(f"{'model':<8}{'threshold':>14}{'rows':>10}{'alerts':>10}{'alert %':>10}")
    for entry in report["scored"]:  # type: ignore[union-attr]
        print(
            f"{entry['label']:<8}{entry['calibration_threshold']:>14.6f}"
            f"{entry['rows_scored']:>10}{entry['alerts']:>10}"
            f"{100.0 * entry['alert_fraction']:>9.2f}%"
        )
    print()
    print(f"NOTE: {report['normalization_note']}")
    print()
    print("new model metrics_json (Pass-1 half):")
    print(json.dumps(report["new_model_metrics_json"], indent=2, allow_nan=False))
    print()
    print("Step 3 of the runbook: STOP. Hand this output to the user for "
          "approval before activating anything.")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s %(message)s",
    )
    args = parse_args(argv)
    workdir = tempfile.mkdtemp(prefix="pass1_shadow_")
    logger.info("Scratch directory: %s", workdir)
    try:
        report = build_report(args, workdir)
    except (ValueError, FileNotFoundError) as exc:
        logger.error("Shadow run failed: %s", exc)
        return 1
    print_report(report)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
        logger.info("Report written to %s", args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
