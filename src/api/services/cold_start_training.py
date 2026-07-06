"""Train and register a tenant cold-start model from an uploaded CSV.

Uses the two-pass bootstrap pipeline (Pass 1 clean + Pass 2 LSTM-VAE) and
persists the checkpoint via the artifact store before registering it as the
active deployment for the tenant.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import BOOTSTRAP_ANOMALY_PATH, BOOTSTRAP_CLEAN_PATH
from src.models.artifact_store import put_checkpoint
from src.models.bootstrap import (
    ColdStartBootstrap,
    InsufficientDataError,
    Pass2BootstrapDetector,
)
from src.models.model_registry import ModelRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColdStartTrainResult:
    model_id: str
    checkpoint_path: str
    pass1_clean_rows: int
    pass1_anomaly_rows: int


def should_train_cold_start(*, force_train: bool, has_active_model: bool) -> bool:
    """Option A: train when no active model, or when the caller forces retrain."""
    return force_train or not has_active_model


def _run_pass1_pass2(csv_path: str, tenant_id: str) -> tuple[Pass2BootstrapDetector, int, int]:
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
    return detector, len(pass1_result.clean_df), len(pass1_result.anomaly_df)


async def train_cold_start_from_csv(
    db: AsyncSession,
    tenant_id: str,
    csv_path: str,
) -> ColdStartTrainResult:
    """Run bootstrap training on *csv_path* and register the checkpoint."""
    try:
        detector, clean_rows, anomaly_rows = await asyncio.to_thread(
            _run_pass1_pass2, csv_path, tenant_id
        )
    except InsufficientDataError:
        raise
    except FileNotFoundError as exc:
        raise ValueError("Invalid or unreadable CSV file") from exc

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pt") as tmp:
        local_path = tmp.name
    try:
        threshold = detector.save_checkpoint(local_path)
        artifact_path = await put_checkpoint(db, f"cold_start/{tenant_id}", local_path)
        registry = ModelRegistry(db)
        model_id = await registry.register_model(
            customer_id=tenant_id,
            model_type="cold_start",
            checkpoint_path=artifact_path,
            baseline_id=None,
            metrics={
                "source": "cold_start_upload",
                "pass1_clean_rows": clean_rows,
                "pass1_anomaly_rows": anomaly_rows,
                "calibration_threshold": threshold,
            },
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
