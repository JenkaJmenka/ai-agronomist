from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
import logging

from src.db.session import get_db
from src.models.model_registry import ModelRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])

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

@router.post("/{customer_id}/train")
async def train_customer_model(customer_id: str, db: AsyncSession = Depends(get_db)):
    """Queue a portioned retraining job on the worker. Poll /training-status.

    Replaces the in-process BackgroundTasks runner: the job record is durable
    across API restarts and the heavy torch training runs on the worker
    service, streaming data from the DB in portions.
    """
    from src.db.jobs import create_job
    from src.mq.publisher import publish_json
    from src.mq.schemas import RetrainJobMessage
    from src.mq.topology import TRAINING_RETRAIN_QUEUE

    active = await ModelRegistry(db).get_active_model(customer_id)
    baseline_checkpoint = active["checkpoint_path"] if active else None

    job = await create_job(
        db,
        tenant_id=customer_id,
        job_type="training_retrain",
        payload={"baseline_checkpoint": baseline_checkpoint},
    )
    await publish_json(
        TRAINING_RETRAIN_QUEUE,
        RetrainJobMessage(
            job_id=job.id,
            tenant_id=customer_id,
            baseline_checkpoint=baseline_checkpoint,
        ),
    )
    return {
        "status": "started",
        "customer_id": customer_id,
        "job_id": job.id,
        "message": "Retraining job queued for the worker service.",
        "poll_url": f"/api/models/{customer_id}/training-status",
    }


@router.get("/{customer_id}/training-status")
async def get_training_status(customer_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Return the status of the most recent training job for this customer.

    status values: "pending" | "running" | "completed" | "failed" | "unknown"
    """
    from src.db.jobs import list_jobs

    jobs = await list_jobs(
        db, tenant_id=customer_id, job_type="training_retrain", limit=1
    )
    if not jobs:
        return {
            "status": "unknown",
            "customer_id": customer_id,
            "message": "No training job found.",
        }
    job = jobs[0]
    return {
        "status": job.status,
        "customer_id": customer_id,
        "job_id": job.id,
        "started_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "model_id": (job.result or {}).get("model_id"),
        "error": job.error,
        "progress": (job.result or {}).get("progress"),
    }

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
