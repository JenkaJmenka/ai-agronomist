"""Fetch incidents from DB for dashboard enrichment."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import streamlit as st
from sqlalchemy import select

from src.dashboard.constants import CACHE_TTL_SHORT

from src.db.models import Incident
from src.db.session import AsyncSessionLocal


def _run_async(coro):
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@st.cache_data(ttl=CACHE_TTL_SHORT)
def fetch_incidents_in_range(
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> List[Dict[str, Any]]:
    """Return incidents detected within [start, end]."""

    async def fetch() -> List[Dict[str, Any]]:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        async with AsyncSessionLocal() as session:
            stmt = (
                select(Incident)
                .where(
                    Incident.tenant_id == tenant_id,
                    Incident.greenhouse_id == greenhouse_id,
                    Incident.detected_at >= start,
                    Incident.detected_at <= end,
                )
                .order_by(Incident.detected_at.desc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "id": str(row.id),
                    "detected_at": row.detected_at,
                    "root_cause": row.root_cause,
                    "root_cause_details": row.root_cause_details,
                    "anomaly_score": row.anomaly_score,
                    "sequence": row.sequence,
                    "fault_type": row.fault_type,
                }
                for row in rows
            ]

    return _run_async(fetch())
