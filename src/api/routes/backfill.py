"""Anomaly score backfill endpoints.

POST /api/backfill/scores — re-score already-persisted SensorReading rows
for a (tenant_id, greenhouse_id, start, end) window.  The endpoint is
idempotent: repeated calls with the same arguments produce the same scores.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.detector_cache import get_cached_detector
from src.api.services.score_backfill import BackfillResult, backfill_scores
from src.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backfill", tags=["Backfill"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class BackfillScoresRequest(BaseModel):
    """Parameters for a score backfill run."""

    tenant_id: str = Field(..., min_length=1, description="Tenant identifier.")
    greenhouse_id: str = Field(
        default="default", description="Greenhouse identifier."
    )
    start: datetime = Field(
        ...,
        description=(
            "Start of the window (inclusive, ISO 8601). "
            "Timezone-naïve values are treated as UTC."
        ),
    )
    end: datetime = Field(
        ...,
        description=(
            "End of the window (inclusive, ISO 8601). "
            "NOT clamped to wall-clock now() — the historical CSV is "
            "future-dated relative to the browser clock."
        ),
    )
    batch_size: int = Field(
        default=5000,
        ge=100,
        le=50_000,
        description="DB rows fetched per page (controls peak memory).",
    )
    checkpoint_path: Optional[str] = Field(
        default=None,
        description=(
            "Optional path to a specific model checkpoint. "
            "Defaults to the current baseline model."
        ),
    )

    @model_validator(mode="after")
    def _start_before_end(self) -> "BackfillScoresRequest":
        if self.end <= self.start:
            raise ValueError("end must be strictly after start")
        return self


class BackfillScoresResponse(BaseModel):
    """Summary of a completed backfill run."""

    tenant_id: str
    greenhouse_id: str
    rows_examined: int = Field(
        description="Non-live SensorReading rows in the window."
    )
    sequences_scored: int = Field(
        description="LSTM sequences formed and scored."
    )
    rows_scored: int = Field(
        description="Rows whose anomaly_score was updated (above threshold)."
    )
    threshold_used: float = Field(
        description="File-level calibration threshold (on the scaled score)."
    )
    threshold_method: str = Field(
        description="Calibration method: 'percentile' or 'pot'."
    )
    live_rows_skipped: int = Field(
        description="Rows excluded because source='live'."
    )
    batches_processed: int = Field(
        description="Number of DB pages processed."
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal issues encountered (e.g. non-finite scores).",
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/scores",
    response_model=BackfillScoresResponse,
    status_code=status.HTTP_200_OK,
    summary="Backfill anomaly scores for persisted readings",
    description=(
        "Re-scores already-persisted SensorReading rows using the LSTM-VAE "
        "model.  Rows with source='live' are excluded.  Safe to re-run — "
        "scores are overwritten on each call, and normal (below-threshold) "
        "rows remain NULL.\n\n"
        "The window is NOT clamped to wall-clock now() — use the full dataset "
        "date range even when it exceeds the current date."
    ),
)
async def post_backfill_scores(
    request: BackfillScoresRequest,
    db: AsyncSession = Depends(get_db),
) -> BackfillScoresResponse:
    """Backfill anomaly scores for a tenant/greenhouse/window."""
    try:
        detector = get_cached_detector(request.checkpoint_path)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Model unavailable: {exc}",
        ) from exc

    try:
        result: BackfillResult = await backfill_scores(
            db=db,
            detector=detector,
            tenant_id=request.tenant_id,
            greenhouse_id=request.greenhouse_id,
            start=request.start,
            end=request.end,
            batch_size=request.batch_size,
        )
    except Exception as exc:
        logger.exception(
            "backfill_scores failed for tenant=%s gh=%s",
            request.tenant_id,
            request.greenhouse_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backfill failed: {exc}",
        ) from exc

    return BackfillScoresResponse(
        tenant_id=result.tenant_id,
        greenhouse_id=result.greenhouse_id,
        rows_examined=result.rows_examined,
        sequences_scored=result.sequences_scored,
        rows_scored=result.rows_scored,
        threshold_used=result.threshold_used,
        threshold_method=result.threshold_method,
        live_rows_skipped=result.live_rows_skipped,
        batches_processed=result.batches_processed,
        warnings=result.warnings,
    )
