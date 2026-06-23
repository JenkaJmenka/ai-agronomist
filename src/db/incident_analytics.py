"""
Analytics and ranking helpers for the incident memory system.

Pure aggregation logic split out of src/db/incident_memory.py:
- root cause / action ranking over similar incidents (suggest_fixes)
- tenant-scoped statistics and recurring-issue reports

All queries are tenant-scoped via build_incident_filters.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Incident
from src.db.vector_search import build_incident_filters

# Number of root causes reported in statistics.
TOP_ROOT_CAUSES_LIMIT: int = 10


def rank_root_causes(incidents: List[Incident]) -> List[Dict[str, Any]]:
    """Rank root causes by frequency; confidence = frequency among neighbours."""
    cause_counts: Dict[str, int] = {}
    for incident in incidents:
        if incident.root_cause:
            cause_counts[incident.root_cause] = cause_counts.get(incident.root_cause, 0) + 1
    ranked = sorted(cause_counts.items(), key=lambda pair: pair[1], reverse=True)
    return [
        {"root_cause": cause, "confidence": count / len(incidents), "count": count}
        for cause, count in ranked
    ]


def rank_actions(incidents: List[Incident], min_confidence: float) -> List[Dict[str, Any]]:
    """Rank actions by observed effectiveness rate across similar incidents."""
    action_stats: Dict[str, List[int]] = {}
    for incident in incidents:
        for action in incident.actions_taken or []:
            totals = action_stats.setdefault(action, [0, 0])
            totals[0] += 1
            if incident.actions_effective:
                totals[1] += 1
    recommended = [
        {"action": action, "effectiveness": effective / total, "count": total}
        for action, (total, effective) in action_stats.items()
        if total > 0 and (effective / total) >= min_confidence
    ]
    recommended.sort(key=lambda entry: entry["effectiveness"], reverse=True)
    return recommended


async def compute_statistics(
    session: AsyncSession, *, tenant_id: str, greenhouse_id: Optional[str] = None
) -> Dict[str, Any]:
    """Tenant-scoped incident statistics (totals, resolution rate, top causes)."""
    conditions = build_incident_filters(tenant_id=tenant_id, greenhouse_id=greenhouse_id)
    total = await session.scalar(
        select(func.count()).select_from(Incident).where(*conditions)
    )
    resolved_count = await session.scalar(
        select(func.count())
        .select_from(Incident)
        .where(*conditions, Incident.resolved.is_(True))
    )
    avg_time = await session.scalar(
        select(func.avg(Incident.time_to_resolve))
        .where(*conditions, Incident.resolved.is_(True))
    )
    count_column = func.count(Incident.id).label("cause_count")
    top_causes_rows = (
        await session.execute(
            select(Incident.root_cause, count_column)
            .where(*conditions, Incident.root_cause.isnot(None))
            .group_by(Incident.root_cause)
            .order_by(desc(count_column))
            .limit(TOP_ROOT_CAUSES_LIMIT)
        )
    ).all()
    total_incidents = int(total or 0)
    resolved_incidents = int(resolved_count or 0)
    return {
        "total_incidents": total_incidents,
        "resolved_incidents": resolved_incidents,
        "resolved_rate": resolved_incidents / total_incidents if total_incidents else 0.0,
        "avg_time_to_resolve_minutes": float(avg_time) if avg_time is not None else None,
        "top_root_causes": [
            {"root_cause": cause, "count": int(count)} for cause, count in top_causes_rows
        ],
    }


async def compute_recurring_issues(
    session: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: Optional[str] = None,
    min_occurrences: int,
) -> List[Dict[str, Any]]:
    """Root causes seen at least min_occurrences times for this tenant."""
    conditions = build_incident_filters(tenant_id=tenant_id, greenhouse_id=greenhouse_id)
    count_column = func.count(Incident.id).label("occurrence_count")
    rows = (
        await session.execute(
            select(
                Incident.root_cause,
                count_column,
                func.max(Incident.detected_at).label("last_occurrence"),
                func.avg(Incident.time_to_resolve).label("avg_resolve_time"),
            )
            .where(*conditions, Incident.root_cause.isnot(None))
            .group_by(Incident.root_cause)
            .having(count_column >= min_occurrences)
            .order_by(desc(count_column))
        )
    ).all()
    return [
        {
            "root_cause": cause,
            "occurrences": int(count),
            "last_occurrence": last_occurrence,
            "avg_time_to_resolve_minutes": float(avg_time) if avg_time is not None else None,
        }
        for cause, count, last_occurrence, avg_time in rows
    ]
