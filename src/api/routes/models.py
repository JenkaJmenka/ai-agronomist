from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
import logging

from src.db.session import get_db, AsyncSessionLocal
from src.models.model_registry import ModelRegistry
from src.models.training_pipeline import PerCustomerTrainingPipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])

# In-process training status store keyed by customer_id.
# Holds the most recent job per customer; replaced on each new run.
_training_status: Dict[str, Dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class RollbackRequest(BaseModel):
    model_id: str

@router.get("/{customer_id}", response_model=Dict[str, Any])
async def get_active_model(customer_id: str, db: AsyncSession = Depends(get_db)):
    registry = ModelRegistry(db)
    model = await registry.get_active_model(customer_id)
    if not model:
        raise HTTPException(status_code=404, detail="No active model found for customer")
    return model

@router.get("/{customer_id}/versions", response_model=List[Dict[str, Any]])
async def list_model_versions(customer_id: str, db: AsyncSession = Depends(get_db)):
    registry = ModelRegistry(db)
    return await registry.list_models(customer_id)

async def _run_training_bg(customer_id: str) -> None:
    """Run the training pipeline in a background task with status tracking."""
    _training_status[customer_id] = {
        "status": "running",
        "customer_id": customer_id,
        "started_at": _now(),
        "finished_at": None,
        "model_id": None,
        "error": None,
    }
    try:
        async with AsyncSessionLocal() as session:
            pipeline = PerCustomerTrainingPipeline(session)
            model_id = await pipeline.train_customer_model(customer_id)
        _training_status[customer_id].update({
            "status": "completed",
            "finished_at": _now(),
            "model_id": model_id,
        })
        logger.info(f"Training completed for {customer_id}: model_id={model_id}")
    except Exception as exc:
        _training_status[customer_id].update({
            "status": "failed",
            "finished_at": _now(),
            "error": str(exc),
        })
        logger.error(f"Background training failed for {customer_id}: {exc}", exc_info=True)

@router.post("/{customer_id}/train")
async def train_customer_model(customer_id: str, background_tasks: BackgroundTasks):
    """Trigger an asynchronous training pipeline. Poll /training-status for updates."""
    background_tasks.add_task(_run_training_bg, customer_id)
    return {
        "status": "started",
        "customer_id": customer_id,
        "message": "Training pipeline has been dispatched in the background.",
        "poll_url": f"/api/models/{customer_id}/training-status",
    }


@router.get("/{customer_id}/training-status")
async def get_training_status(customer_id: str) -> Dict[str, Any]:
    """Return the status of the most recent training job for this customer.

    status values: "running" | "completed" | "failed" | "unknown"
    """
    return _training_status.get(
        customer_id,
        {"status": "unknown", "customer_id": customer_id, "message": "No training job found."},
    )

@router.post("/{customer_id}/rollback")
async def rollback_model(
    customer_id: str, 
    request: RollbackRequest,
    db: AsyncSession = Depends(get_db)
):
    registry = ModelRegistry(db)
    success = await registry.rollback_model(customer_id, request.model_id)
    if not success:
        raise HTTPException(status_code=404, detail="Model not found or invalid customer")
        
    return {
        "status": "success",
        "model_id": request.model_id,
        "message": f"Successfully rolled back {customer_id} to {request.model_id}"
    }
