"""Train and register a tenant cold-start model from an uploaded CSV.

Uses the two-pass bootstrap pipeline (Pass 1 clean + Pass 2 LSTM-VAE) and
persists the checkpoint via the artifact store before registering it as the
active deployment for the tenant.

The Pass-1 observability fields persisted into ``metrics_json`` below follow
the cross-agent contract in ``docs/handoff/backlog14_pass1_observability.md``
(BACKLOG-14 Phase 2). Every fraction is a fraction in [0, 1], never a percent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import BOOTSTRAP_ANOMALY_PATH, BOOTSTRAP_CLEAN_PATH
from src.models.artifact_store import put_checkpoint
from src.models.bootstrap import (
    BootstrapPass1Result,
    ColdStartBootstrap,
    InsufficientDataError,
    Pass2BootstrapDetector,
)
from src.models.mad_scrub_report import MadChannelReport
from src.models.model_registry import ModelRegistry
from src.models.pass1_discard_guard import Pass1DiscardStats

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColdStartTrainResult:
    model_id: str
    checkpoint_path: str
    pass1_clean_rows: int
    pass1_anomaly_rows: int


@dataclass(frozen=True)
class _Pass1TrainingOutcome:
    """Everything the registration step needs out of Pass 1 + Pass 2.

    Carrying the full ``BootstrapPass1Result`` (rather than widening this to
    a many-field tuple) keeps ``_run_pass1_pass2`` single-purpose: it reports
    what Pass 1 measured, and metrics-shaping stays in ``_build_pass1_metrics``.
    """

    detector: Pass2BootstrapDetector
    pass1_result: BootstrapPass1Result


def should_train_cold_start(*, force_train: bool, has_active_model: bool) -> bool:
    """Option A: train when no active model, or when the caller forces retrain."""
    return force_train or not has_active_model


def _run_pass1_pass2(csv_path: str, tenant_id: str) -> _Pass1TrainingOutcome:
    clean_path = BOOTSTRAP_CLEAN_PATH.replace(".csv", f"_{tenant_id}.csv")
    anomaly_path = BOOTSTRAP_ANOMALY_PATH.replace(".csv", f"_{tenant_id}.csv")
    pass1 = ColdStartBootstrap(clean_path=clean_path, anomaly_path=anomaly_path)
    pass1_result = pass1.run(csv_path)
    detector = Pass2BootstrapDetector()
    detector.train(clean_path)
    if detector.trained_model is None:
        raise ValueError(
            "Cold-start training produced no model — not enough clean sequences "
            "after preprocessing."
        )
    return _Pass1TrainingOutcome(detector=detector, pass1_result=pass1_result)


def _finite_or_none(value: float) -> float | None:
    """Coerce a non-finite float to ``None`` before it reaches a JSON column.

    ``MadChannelReport.median``/``.mad``/``.modal_share`` are NaN for a
    skipped channel. ``metrics_json`` is a String column but production is
    Postgres, which rejects the bare ``NaN`` token where SQLite accepts it
    (see ``project_sqlite_postgres_nan_gap``) — match the existing
    finite-or-None guard pattern used across the codebase
    (e.g. ``src/db/incident_memory.py``).
    """
    return value if math.isfinite(value) else None


def _mad_channel_entry(report: MadChannelReport) -> dict[str, object]:
    return {
        "median": _finite_or_none(report.median),
        "mad": _finite_or_none(report.mad),
        "modal_share": _finite_or_none(report.modal_share),
        "finite_rows": report.finite_rows,
        "cull_rows": report.cull_rows,
        "cull_fraction": report.cull_fraction,
        "is_skipped": report.is_skipped,
        "skip_reason": report.skip_reason,
    }


def _discard_stats_entry(stats: Pass1DiscardStats) -> dict[str, object]:
    return {
        "frame_rows": stats.frame_rows,
        "frame_discarded": stats.frame_discarded,
        "frame_discard_fraction": stats.frame_discard_fraction,
        "data_rows": stats.data_rows,
        "data_discarded": stats.data_discarded,
        "data_discard_fraction": stats.data_discard_fraction,
        "status": stats.status,
    }


def _build_pass1_metrics(
    pass1_result: BootstrapPass1Result,
    *,
    clean_rows: int,
    anomaly_rows: int,
    calibration_threshold: float,
) -> dict[str, object]:
    """Shape Pass-1 observability into the ``metrics_json`` dict.

    Guarded with ``json.dumps(..., allow_nan=False)`` so a future non-finite
    value raises here — at the point this dict is built — rather than being
    silently written as the invalid JSON token ``NaN`` (Postgres rejects it,
    SQLite does not; see ``project_sqlite_postgres_nan_gap``).
    ``ModelRegistry.register_model`` (``src/models/model_registry.py:42``) now
    carries its own ``json.dumps(metrics, allow_nan=False)`` guard too
    (commit ``c205265``, pinned by a test there) — this call and that one are
    two independent layers on the same hazard, not one guard standing in for
    the other: this one fails fast with the full ``BootstrapPass1Result`` and
    field names still in scope for the traceback, before the dict ever leaves
    this function.
    """
    metrics: dict[str, object] = {
        "source": "cold_start_upload",
        "pass1_clean_rows": clean_rows,
        "pass1_anomaly_rows": anomaly_rows,
        "calibration_threshold": calibration_threshold,
        "pass1_mad_channels": {
            report.column: _mad_channel_entry(report)
            for report in pass1_result.mad_channels
        },
        "pass1_if_flag_fraction": pass1_result.if_flag_fraction,
        "pass1_discard": _discard_stats_entry(pass1_result.discard_stats),
        "pass1_warnings": [warning.as_dict() for warning in pass1_result.warnings],
    }
    json.dumps(metrics, allow_nan=False)
    return metrics


async def train_cold_start_from_csv(
    db: AsyncSession,
    tenant_id: str,
    csv_path: str,
) -> ColdStartTrainResult:
    """Run bootstrap training on *csv_path* and register the checkpoint."""
    try:
        outcome = await asyncio.to_thread(_run_pass1_pass2, csv_path, tenant_id)
    except InsufficientDataError:
        raise
    except FileNotFoundError as exc:
        raise ValueError("Invalid or unreadable CSV file") from exc

    detector = outcome.detector
    pass1_result = outcome.pass1_result
    clean_rows = len(pass1_result.clean_df)
    anomaly_rows = len(pass1_result.anomaly_df)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pt") as tmp:
        local_path = tmp.name
    try:
        threshold = detector.save_checkpoint(local_path)
        artifact_path = await put_checkpoint(db, f"cold_start/{tenant_id}", local_path)
        registry = ModelRegistry(db)
        metrics = _build_pass1_metrics(
            pass1_result,
            clean_rows=clean_rows,
            anomaly_rows=anomaly_rows,
            calibration_threshold=threshold,
        )
        model_id = await registry.register_model(
            customer_id=tenant_id,
            model_type="cold_start",
            checkpoint_path=artifact_path,
            baseline_id=None,
            metrics=metrics,
        )
        logger.info(
            "Cold-start model %s registered for %s (%d clean rows)",
            model_id,
            tenant_id,
            clean_rows,
        )
        return ColdStartTrainResult(
            model_id=model_id,
            checkpoint_path=artifact_path,
            pass1_clean_rows=clean_rows,
            pass1_anomaly_rows=anomaly_rows,
        )
    finally:
        if os.path.exists(local_path):
            os.unlink(local_path)
