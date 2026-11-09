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
from src.models.shadow_report import (  # noqa: E402
    MIN_SEEDS,
    ScoredRun,
    SeedSpread,
    pass1_metrics,
    seed_everything,
    spread_entry,
)


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
        "--seeds",
        type=int,
        default=MIN_SEEDS,
        help=(
            f"Number of training seeds per model (minimum {MIN_SEEDS}). "
            "Results are reported as a range across seeds, never as one value."
        ),
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Write the full report as JSON to this path (stdout always prints).",
    )
    args = parser.parse_args(argv)
    if args.seeds < MIN_SEEDS:
        parser.error(
            f"--seeds must be at least {MIN_SEEDS}: a single run cannot "
            f"distinguish a real change from training noise."
        )
    return args


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
    seed: int,
) -> ScoredRun:
    """Score *window* and count alerts, without persisting anything."""
    results = detector.infer(window)
    return ScoredRun(
        label=label,
        seed=seed,
        threshold=detector.calibration_threshold,
        rows_scored=len(results),
        alerts=sum(1 for row in results if row.is_anomaly),
    )


def run_pass1(csv_path: str, workdir: str) -> tuple[BootstrapPass1Result, str]:
    """Run the real Pass 1 once; return its result and the clean-rows path.

    Pass 1 runs once rather than per seed because it is deterministic: its only
    stochastic component is `IsolationForest`, pinned at `random_state=42`.
    Measured across repeated runs on the tenant_1 corpus, the discard fraction
    is 0.046187 every time.
    """
    clean_path = os.path.join(workdir, "shadow_clean.csv")
    pass1 = ColdStartBootstrap(
        clean_path=clean_path,
        anomaly_path=os.path.join(workdir, "shadow_anomalies.csv"),
    ).run(csv_path)
    return pass1, clean_path


def train_seed(clean_path: str, seed: int) -> Pass2BootstrapDetector:
    """Train one Pass-2 model on *clean_path* under *seed*."""
    seed_everything(seed)
    detector = Pass2BootstrapDetector()
    detector.train(clean_path)
    if detector.trained_model is None:
        raise ValueError(
            "Pass 2 trained no model — too few clean rows after Pass 1 filtering."
        )
    return detector


def build_report(args: argparse.Namespace, workdir: str) -> dict[str, object]:
    """Train and score every seed, and assemble the report.

    No side effects outside *workdir*: nothing is registered or activated.
    """
    pass1, clean_path = run_pass1(args.csv, workdir)
    frame = pd.concat([pass1.clean_df, pass1.anomaly_df]).sort_index()
    window = select_window(frame, args.window_start, args.window_end)

    seeds = tuple(range(args.seeds))
    new_runs: list[ScoredRun] = []
    old_runs: list[ScoredRun] = []
    for seed in seeds:
        logger.info("Seed %d of %d", seed + 1, len(seeds))
        detector = train_seed(clean_path, seed)
        new_runs.append(score_with(detector, window, "new", seed))
        if args.old_checkpoint:
            # Both sides get the NEW model's adaptive ranges on purpose: the
            # checkpoint does not carry the ranges its own training used, so
            # letting them differ would measure the normalization, not the
            # models. See Pass2BootstrapDetector.from_checkpoint.
            old = Pass2BootstrapDetector.from_checkpoint(
                args.old_checkpoint, ranges=detector.sensor_ranges
            )
            old_runs.append(score_with(old, window, "old", seed))

    spreads = [SeedSpread(label="new", runs=tuple(new_runs))]
    if old_runs:
        spreads.append(SeedSpread(label="old", runs=tuple(old_runs)))

    return {
        "corpus": os.path.abspath(args.csv),
        "corpus_span": [str(frame.index.min()), str(frame.index.max())],
        "window": [args.window_start, args.window_end],
        "window_rows": len(window),
        "old_checkpoint": args.old_checkpoint,
        "seeds": list(seeds),
        "normalization_note": (
            "Both models scored with the new model's adaptive sensor ranges; "
            "checkpoints do not persist their training-time ranges."
        ),
        "spreads": [spread_entry(spread) for spread in spreads],
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
    print(f"seeds       : {report['seeds']}  "
          f"({len(report['seeds'])} training runs per model)")
    print()
    print("RANGES ACROSS SEEDS — a single value would be meaningless here.")
    print(f"{'model':<7}{'threshold min..max':>26}{'spread':>11}"
          f"{'alerts min..max':>19}{'spread':>8}")
    for entry in report["spreads"]:  # type: ignore[union-attr]
        thr, alerts = entry["threshold"], entry["alerts"]
        print(
            f"{entry['label']:<7}"
            f"{thr['min']:>11.6f}..{thr['max']:<13.6f}"
            f"{thr['spread']:>11.6f}"
            f"{alerts['min']:>9d}..{alerts['max']:<9d}"
            f"{alerts['spread']:>8d}"
        )
        print(f"       median threshold {thr['median']:.6f}, median alerts "
              f"{alerts['median']:.1f} of {entry['rows_scored']} rows"
              + ("  [identical across all seeds]"
                 if entry["identical_across_seeds"] else ""))
    print()
    print("Compare the two models' RANGES, not their medians. If the ranges")
    print("overlap, the difference is training noise and not a signal.")
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
