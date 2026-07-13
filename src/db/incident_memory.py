"""
Incident Memory System: store and semantically search past anomalies.

Capabilities:
- Store detected anomalies (with latent embeddings) as incidents
- Find similar past incidents via cosine distance (pgvector on PostgreSQL,
  numpy fallback on SQLite — see src/db/vector_search.py)
- Suggest root causes and fixes learned from resolved history
- Track resolutions, operator feedback, statistics and recurring issues
  (aggregations live in src/db/incident_analytics.py)

All methods are async (SQLAlchemy 2.x AsyncSession) and tenant-scoped.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.incident_analytics import (
    compute_recurring_issues,
    compute_statistics,
    rank_actions,
    rank_root_causes,
)
from src.db.models import Incident, IncidentAction
from src.db.vector_search import IncidentDistancePair, find_similar_incidents

logger = logging.getLogger(__name__)

# Default number of neighbours used when mining fixes from history.
DEFAULT_SUGGESTION_NEIGHBORS: int = 10

# Minimum observed effectiveness rate for an action to be recommended.
DEFAULT_MIN_ACTION_CONFIDENCE: float = 0.5

# Same root cause seen at least this many times counts as "recurring".
DEFAULT_MIN_RECURRENCES: int = 3

SECONDS_PER_MINUTE: int = 60
UNKNOWN_ROOT_CAUSE: str = "unknown"
CONFIRMED_ANOMALY_TAG: str = "confirmed_anomaly"
FALSE_POSITIVE_TAG: str = "false_positive"


class IncidentAlreadyResolvedError(Exception):
    """Raised when resolve_incident is called on an already-resolved incident."""


def _ensure_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC (we only store UTC)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _to_float_list(values: Sequence[float]) -> List[float]:
    if isinstance(values, np.ndarray):
        return [float(component) for component in values.tolist()]
    return [float(component) for component in values]


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats (NaN/±Inf) with None, recursing into containers.

    PostgreSQL's JSON type cannot represent NaN/Infinity — asyncpg raises
    InvalidTextRepresentationError ('Token "NaN" is invalid') on insert. SQLite
    silently stores them as text, so this must guard at the persistence boundary
    regardless of which detector produced the value.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class IncidentMemory:
    """Semantic search over past incidents, backed by an AsyncSession."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def store_incident(
        self,
        *,
        tenant_id: str,
        embedding: Sequence[float],
        anomaly_score: float,
        sequence: Any,
        embedding_uncertainty: float,
        greenhouse_id: str = "default",
        root_cause: Optional[str] = None,
        root_cause_details: Optional[Dict[str, Any]] = None,
        fault_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        notes: Optional[str] = None,
    ) -> str:
        """Store a detected anomaly as an incident; returns the incident UUID."""
        sequence_payload = sequence.tolist() if isinstance(sequence, np.ndarray) else sequence
        incident = Incident(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            embedding=_to_float_list(embedding),
            anomaly_score=float(anomaly_score),
            sequence=sequence_payload,
            sequence_length=len(sequence_payload),
            embedding_uncertainty=float(embedding_uncertainty),
            root_cause=root_cause,
            root_cause_details=_json_safe(root_cause_details),
            fault_type=fault_type,
            detected_at=datetime.now(timezone.utc),
            tags=list(tags) if tags else [],
            notes=notes,
        )
        self._session.add(incident)
        await self._session.commit()
        logger.info("Stored incident %s for tenant %s", incident.id, tenant_id)
        return str(incident.id)

    async def find_similar(
        self,
        *,
        embedding: Sequence[float],
        tenant_id: str,
        greenhouse_id: Optional[str] = None,
        k: int = 5,
        max_age_days: Optional[int] = None,
        resolved_only: bool = False,
    ) -> List[IncidentDistancePair]:
        """k most similar past incidents as (incident, cosine_distance) pairs."""
        return await find_similar_incidents(
            self._session,
            embedding=_to_float_list(embedding),
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            k=k,
            max_age_days=max_age_days,
            resolved_only=resolved_only,
        )

    async def find_similar_by_root_cause(
        self,
        *,
        embedding: Sequence[float],
        tenant_id: str,
        greenhouse_id: Optional[str] = None,
        k: int = 5,
    ) -> Dict[str, List[Incident]]:
        """Find similar incidents and group them by root cause label."""
        similar = await self.find_similar(
            embedding=embedding, tenant_id=tenant_id, greenhouse_id=greenhouse_id, k=k
        )
        grouped: Dict[str, List[Incident]] = {}
        for incident, _distance in similar:
            cause = incident.root_cause or UNKNOWN_ROOT_CAUSE
            grouped.setdefault(cause, []).append(incident)
        return grouped

    async def suggest_fixes(
        self,
        *,
        embedding: Sequence[float],
        tenant_id: str,
        greenhouse_id: Optional[str] = None,
        k: int = DEFAULT_SUGGESTION_NEIGHBORS,
        min_confidence: float = DEFAULT_MIN_ACTION_CONFIDENCE,
    ) -> Dict[str, Any]:
        """Suggest root cause and actions based on similar RESOLVED incidents."""
        similar = await self.find_similar(
            embedding=embedding,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            k=k,
            resolved_only=True,
        )
        if not similar:
            return {
                "most_likely_cause": None,
                "all_causes": [],
                "recommended_actions": [],
                "similar_incidents": [],
                "confidence": 0,
                "warning": "No similar resolved incidents found",
            }

        incidents = [incident for incident, _distance in similar]
        ranked_causes = rank_root_causes(incidents)
        return {
            "most_likely_cause": ranked_causes[0] if ranked_causes else None,
            "all_causes": ranked_causes,
            "recommended_actions": rank_actions(incidents, min_confidence),
            "similar_incidents": similar,
            "confidence": len(similar),
            "warning": None,
        }

    async def resolve_incident(
        self,
        *,
        incident_id: str,
        tenant_id: str,
        root_cause: str,
        actions_taken: List[str],
        actions_effective: bool,
        notes: Optional[str] = None,
    ) -> None:
        """Mark an incident resolved; records actions and time-to-resolve."""
        incident = await self._get_incident(incident_id, tenant_id)
        if incident.resolved:
            raise IncidentAlreadyResolvedError(
                f"Incident {incident_id} is already resolved"
            )
        resolved_at = datetime.now(timezone.utc)
        incident.root_cause = root_cause
        incident.actions_taken = list(actions_taken)
        incident.actions_effective = actions_effective
        incident.resolved = True
        incident.resolved_at = resolved_at
        if notes is not None:
            incident.notes = notes
        if incident.detected_at is not None:
            elapsed = resolved_at - _ensure_utc(incident.detected_at)
            incident.time_to_resolve = int(elapsed.total_seconds() / SECONDS_PER_MINUTE)
        for action in actions_taken:
            self._session.add(
                IncidentAction(
                    incident_id=incident.id,
                    action=action,
                    action_time=resolved_at,
                    effectiveness=actions_effective,
                )
            )
        await self._session.commit()
        logger.info("Resolved incident %s with cause=%s", incident_id, root_cause)

    async def mark_anomaly(
        self, *, incident_id: str, tenant_id: str, is_anomaly: bool
    ) -> None:
        """Operator feedback: confirm or reject an incident as a real anomaly."""
        incident = await self._get_incident(incident_id, tenant_id)
        feedback_tag = CONFIRMED_ANOMALY_TAG if is_anomaly else FALSE_POSITIVE_TAG
        current_tags = list(incident.tags or [])
        if feedback_tag not in current_tags:
            # Reassign (not mutate) so the JSON column change is tracked.
            incident.tags = current_tags + [feedback_tag]
        await self._session.commit()
        logger.info("Marked incident %s as anomaly=%s", incident_id, is_anomaly)

    async def get_statistics(
        self, *, tenant_id: str, greenhouse_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Tenant-scoped incident statistics."""
        return await compute_statistics(
            self._session, tenant_id=tenant_id, greenhouse_id=greenhouse_id
        )

    async def get_recurring_issues(
        self,
        *,
        tenant_id: str,
        greenhouse_id: Optional[str] = None,
        min_occurrences: int = DEFAULT_MIN_RECURRENCES,
    ) -> List[Dict[str, Any]]:
        """Root causes seen at least min_occurrences times for this tenant."""
        return await compute_recurring_issues(
            self._session,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            min_occurrences=min_occurrences,
        )

    async def get_incident_by_id(
        self, *, incident_id: str, tenant_id: str
    ) -> Incident:
        """Fetch a tenant-owned incident."""
        return await self._get_incident(incident_id, tenant_id)

    async def list_open_incidents(
        self,
        *,
        tenant_id: str,
        greenhouse_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Incident]:
        """Return unresolved incidents ordered by detected_at desc."""
        stmt = (
            select(Incident)
            .where(Incident.tenant_id == tenant_id, Incident.resolved.is_(False))
            .order_by(Incident.detected_at.desc())
            .limit(limit)
        )
        if greenhouse_id:
            stmt = stmt.where(Incident.greenhouse_id == greenhouse_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_nearby(
        self,
        *,
        tenant_id: str,
        timestamp: datetime,
        greenhouse_id: Optional[str] = None,
        window_min: int = 15,
    ) -> List[Incident]:
        """Incidents detected within ±window_min of timestamp."""
        from datetime import timedelta

        start = timestamp - timedelta(minutes=window_min)
        end = timestamp + timedelta(minutes=window_min)
        stmt = (
            select(Incident)
            .where(
                Incident.tenant_id == tenant_id,
                Incident.detected_at >= start,
                Incident.detected_at <= end,
            )
            .order_by(Incident.detected_at.desc())
        )
        if greenhouse_id:
            stmt = stmt.where(Incident.greenhouse_id == greenhouse_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def _get_incident(self, incident_id: str, tenant_id: str) -> Incident:
        """Fetch a tenant-owned incident or raise ValueError (maps to 404)."""
        incident = await self._session.scalar(
            select(Incident).where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
        )
        if incident is None:
            raise ValueError(f"Incident {incident_id} not found for tenant")
        return incident
