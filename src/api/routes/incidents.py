"""
API endpoints for the incident memory system (Gap 2).

Stores detected anomalies with their latent embeddings and provides
semantic search ("find similar past incidents"), fix suggestions mined
from resolved history, resolution tracking, and tenant-level analytics.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sql_delete, select as sql_select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key

from src.config import DEFAULT_NEAREST_NEIGHBORS, LATENT_DIM
from src.db.incident_memory import (
    DEFAULT_MIN_ACTION_CONFIDENCE,
    DEFAULT_MIN_RECURRENCES,
    DEFAULT_SUGGESTION_NEIGHBORS,
    IncidentAlreadyResolvedError,
    IncidentMemory,
)
from src.db.models import Incident, IncidentAction, OperatorFeedback
from src.db.session import get_db
from src.api.schemas.incidents import (
    ActionSuggestion,
    IncidentCreateRequest,
    IncidentResolveRequest,
    IncidentResponse,
    IncidentStoreResponse,
    RecurringIssue,
    RootCauseSuggestion,
    StatisticsResponse,
    SuggestionsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

MAX_SIMILAR_RESULTS = 50


def _validate_query_embedding(embedding: List[float]) -> List[float]:
    """Query-param embeddings cannot use the schema validator; check here."""
    if len(embedding) != LATENT_DIM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Embedding has {len(embedding)} dimensions, expected {LATENT_DIM}",
        )
    return embedding


def _to_incident_response(incident: Incident, distance: Optional[float] = None) -> IncidentResponse:
    response = IncidentResponse.model_validate(incident)
    if distance is not None:
        response = response.model_copy(update={"distance": distance})
    return response


@router.post("/store", response_model=IncidentStoreResponse, status_code=status.HTTP_201_CREATED)
async def store_incident(
    request: IncidentCreateRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> IncidentStoreResponse:
    """Store a detected anomaly as an incident."""
    enforce_tenant_access(auth_tenant, request.tenant_id)
    memory = IncidentMemory(db)
    incident_id = await memory.store_incident(
        tenant_id=request.tenant_id,
        greenhouse_id=request.greenhouse_id,
        embedding=request.embedding,
        anomaly_score=request.anomaly_score,
        sequence=request.sequence,
        embedding_uncertainty=request.embedding_uncertainty,
        root_cause=request.root_cause,
        tags=request.tags,
        notes=request.notes,
    )
    return IncidentStoreResponse(incident_id=incident_id, status="stored")


@router.get("/similar", response_model=List[IncidentResponse])
async def find_similar(
    embedding: List[float] = Query(..., description="Query embedding (LATENT_DIM floats)"),
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    k: int = Query(DEFAULT_NEAREST_NEIGHBORS, ge=1, le=MAX_SIMILAR_RESULTS),
    max_age_days: Optional[int] = Query(None, ge=1),
    resolved_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> List[IncidentResponse]:
    """Find the k most similar past incidents (most similar first)."""
    enforce_tenant_access(auth_tenant, tenant_id)
    memory = IncidentMemory(db)
    similar = await memory.find_similar(
        embedding=_validate_query_embedding(embedding),
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        k=k,
        max_age_days=max_age_days,
        resolved_only=resolved_only,
    )
    return [_to_incident_response(incident, distance) for incident, distance in similar]


@router.get("/suggest-fixes", response_model=SuggestionsResponse)
async def suggest_fixes(
    embedding: List[float] = Query(..., description="Query embedding (LATENT_DIM floats)"),
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    k: int = Query(DEFAULT_SUGGESTION_NEIGHBORS, ge=1, le=MAX_SIMILAR_RESULTS),
    min_confidence: float = Query(DEFAULT_MIN_ACTION_CONFIDENCE, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> SuggestionsResponse:
    """Suggest root cause and fixes based on similar resolved incidents."""
    enforce_tenant_access(auth_tenant, tenant_id)
    memory = IncidentMemory(db)
    suggestions = await memory.suggest_fixes(
        embedding=_validate_query_embedding(embedding),
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        k=k,
        min_confidence=min_confidence,
    )
    most_likely = suggestions["most_likely_cause"]
    return SuggestionsResponse(
        most_likely_cause=RootCauseSuggestion(**most_likely) if most_likely else None,
        all_causes=[RootCauseSuggestion(**cause) for cause in suggestions["all_causes"]],
        recommended_actions=[
            ActionSuggestion(**action) for action in suggestions["recommended_actions"]
        ],
        similar_incidents=[
            _to_incident_response(incident, distance)
            for incident, distance in suggestions["similar_incidents"]
        ],
        confidence=suggestions["confidence"],
        warning=suggestions["warning"],
    )


@router.post("/resolve/{incident_id}", response_model=Dict[str, Any])
async def resolve_incident(
    incident_id: str,
    request: IncidentResolveRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> Dict[str, Any]:
    """Mark an incident as resolved with root cause and actions taken."""
    enforce_tenant_access(auth_tenant, request.tenant_id)
    memory = IncidentMemory(db)
    try:
        # IncidentMemory._get_incident (called internally) already scopes the
        # lookup by (incident_id, tenant_id) and raises ValueError -> 404
        # below when the incident belongs to a different tenant.
        await memory.resolve_incident(
            incident_id=incident_id,
            tenant_id=request.tenant_id,
            root_cause=request.root_cause,
            actions_taken=request.actions_taken,
            actions_effective=request.actions_effective,
            notes=request.notes,
        )
    except IncidentAlreadyResolvedError as exc:
        logger.warning("Resolve conflict for incident %s: %s", incident_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except ValueError as exc:
        logger.warning("Resolve failed for incident %s: %s", incident_id, exc)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return {"status": "resolved", "incident_id": incident_id}


@router.get("/statistics", response_model=StatisticsResponse)
async def get_statistics(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> StatisticsResponse:
    """Tenant-scoped incident statistics."""
    enforce_tenant_access(auth_tenant, tenant_id)
    memory = IncidentMemory(db)
    statistics = await memory.get_statistics(tenant_id=tenant_id, greenhouse_id=greenhouse_id)
    return StatisticsResponse(**statistics)


@router.get("/recurring-issues", response_model=List[RecurringIssue])
async def get_recurring_issues(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    min_occurrences: int = Query(DEFAULT_MIN_RECURRENCES, ge=1),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> List[RecurringIssue]:
    """Root causes that keep re-occurring for a tenant."""
    enforce_tenant_access(auth_tenant, tenant_id)
    memory = IncidentMemory(db)
    recurring = await memory.get_recurring_issues(
        tenant_id=tenant_id, greenhouse_id=greenhouse_id, min_occurrences=min_occurrences
    )
    return [RecurringIssue(**issue) for issue in recurring]


@router.get("/open", response_model=List[IncidentResponse])
async def list_open_incidents(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> List[IncidentResponse]:
    """Unresolved incidents for operator review queue."""
    enforce_tenant_access(auth_tenant, tenant_id)
    memory = IncidentMemory(db)
    incidents = await memory.list_open_incidents(
        tenant_id=tenant_id, greenhouse_id=greenhouse_id, limit=limit
    )
    return [_to_incident_response(inc) for inc in incidents]


@router.get("/nearby", response_model=List[IncidentResponse])
async def find_nearby_incidents(
    tenant_id: str = Query(..., min_length=1),
    timestamp: datetime = Query(..., description="ISO timestamp to match"),
    greenhouse_id: Optional[str] = Query(None),
    window_min: int = Query(15, ge=1, le=120),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> List[IncidentResponse]:
    """Incidents detected within ±window_min of the given timestamp."""
    enforce_tenant_access(auth_tenant, tenant_id)
    memory = IncidentMemory(db)
    incidents = await memory.find_nearby(
        tenant_id=tenant_id,
        timestamp=timestamp,
        greenhouse_id=greenhouse_id,
        window_min=window_min,
    )
    return [_to_incident_response(inc) for inc in incidents]


@router.delete("/clear", status_code=200)
async def clear_incidents(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> Dict[str, Any]:
    """Delete all incidents and operator feedback for a tenant (and optional greenhouse)."""
    enforce_tenant_access(auth_tenant, tenant_id)

    # Step 1: collect IDs of incidents to delete (needed to cascade into child tables)
    id_stmt = sql_select(Incident.id).where(Incident.tenant_id == tenant_id)
    if greenhouse_id:
        id_stmt = id_stmt.where(Incident.greenhouse_id == greenhouse_id)
    id_result = await db.execute(id_stmt)
    incident_ids = [row[0] for row in id_result.fetchall()]

    # Step 2: delete child rows first (FK constraint order)
    if incident_ids:
        await db.execute(
            sql_delete(IncidentAction).where(
                IncidentAction.incident_id.in_(incident_ids)
            )
        )

    # Step 3: delete operator feedback — uses customer_id (= tenant_id)
    await db.execute(
        sql_delete(OperatorFeedback).where(OperatorFeedback.customer_id == tenant_id)
    )

    # Step 4: delete the incidents themselves
    inc_stmt = sql_delete(Incident).where(Incident.tenant_id == tenant_id)
    if greenhouse_id:
        inc_stmt = inc_stmt.where(Incident.greenhouse_id == greenhouse_id)
    await db.execute(inc_stmt)

    await db.commit()
    return {
        "cleared": True,
        "deleted_incidents": len(incident_ids),
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
    }


@router.get("/{incident_id}", response_model=IncidentResponse)
async def get_incident(
    incident_id: str,
    tenant_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> IncidentResponse:
    """Single incident with diagnosis snapshot."""
    enforce_tenant_access(auth_tenant, tenant_id)
    memory = IncidentMemory(db)
    try:
        incident = await memory.get_incident_by_id(
            incident_id=incident_id, tenant_id=tenant_id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return _to_incident_response(incident)
