"""Result shaping for the shadow-retraining comparison.

Split out of `scripts/pass1_shadow_score.py` so that script stays a thin CLI
(argument parsing, orchestration, printing) and the shaping is importable and
testable. See `docs/runbooks/cold-start-retraining.md`.

**Everything here reports a range across seeds, never a single value.**
Cold-start Pass-2 training is non-deterministic — weight init, the DataLoader
shuffle and dropout are all unseeded — so one run's threshold and alert count
are samples. Two runs on an identical corpus and window measured
0.133601 / 139 alerts and 0.131892 / 149 alerts. A reader given one number
will read a ten-alert difference as a signal, which is why the single-value
form is deliberately not offered.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.models.bootstrap import BootstrapPass1Result

# A comparison needs at least this many training runs per model to say anything
# about whether a difference exceeds the noise.
MIN_SEEDS: int = 3


def seed_everything(seed: int) -> None:
    """Seed the sources of Pass-2 training noise for one run.

    `torch.manual_seed` covers weight init, dropout and the DataLoader's
    shuffle order, because the loader runs with `num_workers=0`. This does not
    make training deterministic in general — that is its own backlog item — it
    makes each run here *reproducible*, so a spread across seeds is a
    measurement rather than an accident.

    Pass 1 needs no seeding: its only stochastic component is
    `IsolationForest`, pinned at `random_state=42`.
    """
    import numpy as np
    import torch

    np.random.seed(seed)
    torch.manual_seed(seed)


@dataclass(frozen=True)
class ScoredRun:
    """What one model made of the window, on one seed."""

    label: str
    seed: int
    threshold: float
    rows_scored: int
    alerts: int

    @property
    def alert_fraction(self) -> float:
        return self.alerts / self.rows_scored if self.rows_scored else 0.0


def _span(values: tuple[float, ...]) -> tuple[float, float, float]:
    """Return ``(min, median, max)`` of *values*."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = (
        ordered[mid]
        if len(ordered) % 2
        else (ordered[mid - 1] + ordered[mid]) / 2.0
    )
    return ordered[0], median, ordered[-1]


@dataclass(frozen=True)
class SeedSpread:
    """One model's results across every seed it was trained under."""

    label: str
    runs: tuple[ScoredRun, ...]

    @property
    def is_deterministic(self) -> bool:
        """True when every seed agreed exactly.

        Expected for a model loaded from a fixed checkpoint: the weights do not
        change, dropout is off in eval mode, and the ranges are derived
        deterministically. Measured rather than assumed, so a surprise here is
        visible instead of silent.
        """
        return len({(run.threshold, run.alerts) for run in self.runs}) == 1

    @property
    def threshold_span(self) -> tuple[float, float, float]:
        return _span(tuple(run.threshold for run in self.runs))

    @property
    def alert_span(self) -> tuple[float, float, float]:
        return _span(tuple(float(run.alerts) for run in self.runs))


def spread_entry(spread: SeedSpread) -> dict[str, object]:
    """Serialize one model's across-seed spread. Ranges, never single values."""
    t_min, t_med, t_max = spread.threshold_span
    a_min, a_med, a_max = spread.alert_span
    return {
        "label": spread.label,
        "seeds_run": len(spread.runs),
        "identical_across_seeds": spread.is_deterministic,
        "threshold": {
            "min": t_min,
            "median": t_med,
            "max": t_max,
            "spread": t_max - t_min,
        },
        "alerts": {
            "min": int(a_min),
            "median": a_med,
            "max": int(a_max),
            "spread": int(a_max - a_min),
        },
        "rows_scored": spread.runs[0].rows_scored,
        "per_seed": [
            {"seed": run.seed, "threshold": run.threshold, "alerts": run.alerts}
            for run in spread.runs
        ],
    }


def pass1_metrics(pass1: BootstrapPass1Result) -> dict[str, object]:
    """The Pass-1 half of what `metrics_json` will carry, for eyeballing.

    Mirrors `cold_start_training._build_pass1_metrics` in content but is
    deliberately a separate, read-only shaping: the shadow tooling must not
    import the API service layer, and a drift between the two is a reason to
    re-read both rather than to trust this one.
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
