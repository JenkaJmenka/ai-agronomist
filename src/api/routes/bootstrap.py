"""Bootstrap pipeline FastAPI route.

POST /api/v1/bootstrap/run — Run the two-pass cold-start bootstrap pipeline
on an uploaded SCADA CSV and return per-row anomaly classification results.

Pass 1 cleans the data using physics-informed IsolationForest (Tetens
psychrometric check + sklearn IsolationForest).  Pass 2 trains an LSTM-VAE
on the clean subset, then runs full inference with 5-classifier root-cause
tagging (spike, stuck, drift, noise, dropout).

Heavy sync calls (sklearn, PyTorch) are offloaded to a thread pool via
asyncio.to_thread() so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import tempfile

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from src.api.auth import enforce_tenant_access, require_api_key
from src.models.bootstrap import (
    BootstrapRowResult as EngineBootstrapRowResult,
    ColdStartBootstrap,
    InsufficientDataError,
    Pass2BootstrapDetector,
)
from src.api.schemas.bootstrap import BootstrapRowResult, BootstrapRunResponse
from src.config import BOOTSTRAP_ANOMALY_PATH, BOOTSTRAP_CLEAN_PATH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/bootstrap", tags=["bootstrap"])


def _ts_to_iso(ts: object) -> str:
    """Convert a pandas Timestamp to an ISO 8601 UTC string.

    Replicates the _timestamp_to_iso logic from pass2_bootstrap_detect:
    tz-naive timestamps are first localized to UTC before formatting.
    """
    if isinstance(ts, pd.Timestamp):
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.isoformat()
    return str(ts)


@router.post("/run", response_model=BootstrapRunResponse)
async def run_bootstrap(
    file: UploadFile = File(...),
    tenant_id: str = Form(...),
    auth_tenant: str | None = Depends(require_api_key),
) -> BootstrapRunResponse:
    """Run the two-pass cold-start bootstrap pipeline on a raw SCADA CSV.

    The uploaded CSV is written to a temporary file, processed through
    Pass 1 (physics + IsolationForest cleaning) and Pass 2 (LSTM-VAE
    training + full inference), and the merged per-row result is returned.

    Args:
        file: Raw SCADA CSV upload.
        tenant_id: Tenant identifier used for multi-tenant output isolation.

    Returns:
        BootstrapRunResponse with ingestion counts and per-row results.

    Raises:
        HTTPException 422: CSV could not be read (FileNotFoundError).
        HTTPException 400: Fewer than 100 clean rows after Pass 1.
        HTTPException 500: Unexpected pipeline or training failure.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    # Write upload to disk with delete=False — the file must persist across
    # the subsequent asyncio.to_thread call after the with-block closes.
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            while chunk := await file.read(1024 * 1024):
                tmp.write(chunk)
            tmp_path = tmp.name
    except Exception as exc:
        logger.error("Upload write failed for tenant %s: %s", tenant_id, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to process uploaded file",
        ) from exc

    try:
        # ------------------------------------------------------------------
        # Build tenant-scoped output paths to prevent data races between
        # concurrent requests from different tenants.
        # ------------------------------------------------------------------
        clean_path = BOOTSTRAP_CLEAN_PATH.replace(".csv", f"_{tenant_id}.csv")
        anomaly_path = BOOTSTRAP_ANOMALY_PATH.replace(".csv", f"_{tenant_id}.csv")

        # ------------------------------------------------------------------
        # Pass 1 — physics-informed cleaning
        # ------------------------------------------------------------------
        try:
            pass1_result = await asyncio.to_thread(
                ColdStartBootstrap(
                    clean_path=clean_path,
                    anomaly_path=anomaly_path,
                ).run,
                tmp_path,
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid CSV file"
            ) from exc
        except InsufficientDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(
                "Bootstrap Pass 1 failed for tenant %s: %s", tenant_id, exc, exc_info=True
            )
            raise HTTPException(
                status_code=500, detail="Bootstrap pipeline error"
            ) from exc

        # ------------------------------------------------------------------
        # Pass 2 — train LSTM-VAE on Pass 1 clean subset
        # ------------------------------------------------------------------
        detector = Pass2BootstrapDetector()
        try:
            await asyncio.to_thread(detector.train, clean_path)
        except Exception as exc:
            logger.error(
                "Bootstrap Pass 2 training failed for tenant %s: %s",
                tenant_id,
                exc,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail="Model training failed",
            ) from exc

        # Reconstruct the full resampled DataFrame from Pass 1 results so
        # inference sees the same 5-min cadence, interpolation, and column
        # names that the model was trained on — not the raw vendor CSV.
        full_df = pd.concat(
            [pass1_result.clean_df, pass1_result.anomaly_df]
        ).sort_index()

        # Run inference — offload heavy PyTorch forward pass to thread pool.
        raw_results: list[EngineBootstrapRowResult] = await asyncio.to_thread(
            detector.infer, full_df
        )

        # ------------------------------------------------------------------
        # Overlay Pass 1 flags (anomaly_df.index → pass1_filtered=True)
        # ------------------------------------------------------------------
        pass1_anomaly_timestamps: set[str] = {
            _ts_to_iso(ts) for ts in pass1_result.anomaly_df.index
        }
        for result in raw_results:
            result.pass1_filtered = result.timestamp in pass1_anomaly_timestamps

        # ------------------------------------------------------------------
        # Overlay physics validation status
        # physics_flags is a pd.Series[bool]: True = failed psychrometric check
        # ------------------------------------------------------------------
        physics_failed_timestamps: set[str] = {
            _ts_to_iso(ts)
            for ts, flagged in pass1_result.physics_flags.items()
            if flagged
        }
        for result in raw_results:
            if result.timestamp in physics_failed_timestamps:
                result.physics_validation_status = "FAILED_PSYCHROMETRIC_LIMIT"

        # ------------------------------------------------------------------
        # Convert engine dataclasses → Pydantic response models
        # Field names are identical so asdict() unpacks cleanly.
        # ------------------------------------------------------------------
        pydantic_results: list[BootstrapRowResult] = [
            BootstrapRowResult(**dataclasses.asdict(r)) for r in raw_results
        ]

        return BootstrapRunResponse(
            rows_ingested=len(full_df),
            pass1_clean_rows=len(pass1_result.clean_df),
            pass1_anomaly_rows=len(pass1_result.anomaly_df),
            results=pydantic_results,
        )

    finally:
        # Always clean up the temp file regardless of success or failure.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
