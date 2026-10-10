from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
import logging

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.services.detector_cache import clear_detector_cache
from src.db.session import get_db
from src.models.model_registry import ModelRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])

class RollbackRequest(BaseModel):
    model_id: str

class ActivateRequest(BaseModel):
    model_id: str

@router.get("/{tenant_id}", response_model=Dict[str, Any])
async def get_active_model(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    enforce_tenant_access(auth_tenant, tenant_id)
    registry = ModelRegistry(db)
    model = await registry.get_active_model(tenant_id)
    if not model:
        raise HTTPException(status_code=404, detail="No active model found for customer")
    return model

@router.get("/{tenant_id}/versions", response_model=List[Dict[str, Any]])
async def list_model_versions(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    enforce_tenant_access(auth_tenant, tenant_id)
    registry = ModelRegistry(db)
    return await registry.list_models(tenant_id)

@router.post("/{tenant_id}/train")
async def train_customer_model(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    """Queue a portioned retraining job on the worker. Poll /training-status.

    Replaces the in-process BackgroundTasks runner: the job record is durable
    across API restarts and the heavy torch training runs on the worker
    service, streaming data from the DB in portions.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    from src.db.jobs import create_job
    from src.mq.publisher import publish_json
    from src.mq.schemas import RetrainJobMessage
    from src.mq.topology import TRAINING_RETRAIN_QUEUE

    active = await ModelRegistry(db).get_active_model(tenant_id)
    baseline_checkpoint = active["checkpoint_path"] if active else None

    job = await create_job(
        db,
        tenant_id=tenant_id,
        job_type="training_retrain",
        payload={"baseline_checkpoint": baseline_checkpoint},
    )
    await publish_json(
        TRAINING_RETRAIN_QUEUE,
        RetrainJobMessage(
            job_id=job.id,
            tenant_id=tenant_id,
            baseline_checkpoint=baseline_checkpoint,
        ),
    )
    return {
        "status": "started",
        "customer_id": tenant_id,
        "job_id": job.id,
        "message": "Retraining job queued for the worker service.",
        "poll_url": f"/api/models/{tenant_id}/training-status",
    }


@router.get("/{tenant_id}/training-status")
async def get_training_status(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> Dict[str, Any]:
    """Return the status of the most recent training job for this customer.

    status values: "pending" | "running" | "completed" | "failed" | "unknown"
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    from src.db.jobs import list_jobs

    jobs = await list_jobs(
        db, tenant_id=tenant_id, job_type="training_retrain", limit=1
    )
    if not jobs:
        return {
            "status": "unknown",
            "customer_id": tenant_id,
            "message": "No training job found.",
        }
    job = jobs[0]
    return {
        "status": job.status,
        "customer_id": tenant_id,
        "job_id": job.id,
        "started_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "model_id": (job.result or {}).get("model_id"),
        "error": job.error,
        "progress": (job.result or {}).get("progress"),
    }

@router.post("/{tenant_id}/rollback")
async def rollback_model(
    tenant_id: str,
    request: RollbackRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    enforce_tenant_access(auth_tenant, tenant_id)
    registry = ModelRegistry(db)
    success = await registry.rollback_model(tenant_id, request.model_id)
    if not success:
        raise HTTPException(status_code=404, detail="Model not found or invalid customer")

    clear_detector_cache()
    return {
        "status": "success",
        "model_id": request.model_id,
        "message": f"Successfully rolled back {tenant_id} to {request.model_id}"
    }


@router.post("/{tenant_id}/activate")
async def activate_model(
    tenant_id: str,
    request: ActivateRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    """Set a previously trained model as the active deployment."""
    enforce_tenant_access(auth_tenant, tenant_id)
    registry = ModelRegistry(db)
    success = await registry.activate_model(tenant_id, request.model_id)
    if not success:
        raise HTTPException(status_code=404, detail="Model not found or invalid customer")

    clear_detector_cache()
    return {
        "status": "success",
        "model_id": request.model_id,
        "message": f"Activated model {request.model_id} for {tenant_id}",
    }


@router.delete("/{tenant_id}/versions/{model_id}")
async def delete_model_version(
    tenant_id: str,
    model_id: str,
    force: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    """Delete one model version. Use force=true to delete the only active model."""
    enforce_tenant_access(auth_tenant, tenant_id)
    registry = ModelRegistry(db)
    success = await registry.delete_model(tenant_id, model_id, force=force)
    if not success:
        raise HTTPException(
            status_code=409,
            detail="Model not found, or it is the only active model (pass force=true)",
        )

    clear_detector_cache()
    return {
        "status": "success",
        "model_id": model_id,
        "message": f"Deleted model {model_id} for {tenant_id}",
    }


@router.delete("/{tenant_id}/versions")
async def delete_all_model_versions(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    """Delete all model versions for a customer."""
    enforce_tenant_access(auth_tenant, tenant_id)
    registry = ModelRegistry(db)
    count = await registry.clear_models(tenant_id)
    clear_detector_cache()
    return {
        "status": "success",
        "deleted_versions": count,
        "message": f"Deleted {count} model(s) for {tenant_id}",
    }
