"""Fetch incidents via the API for dashboard enrichment."""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.dashboard.constants import CACHE_MAX_ENTRIES_LIST, CACHE_TTL_SHORT
from src.dashboard.services.api_client import DashboardApiClient

_client = DashboardApiClient()


@st.cache_data(ttl=CACHE_TTL_SHORT, max_entries=CACHE_MAX_ENTRIES_LIST)
def fetch_incidents_in_range(
    tenant_id: str,
    greenhouse_id: str,
    start_iso: str,
    end_iso: str,
) -> List[Dict[str, Any]]:
    """Return incidents detected within [start, end]."""
    rows = _client.get_incidents(
        tenant_id, greenhouse_id, start_iso=start_iso, end_iso=end_iso
    )
    return [
        {
            "id": row["id"],
            "detected_at": row["detected_at"],
            "root_cause": row["root_cause"],
            "root_cause_details": row["root_cause_details"],
            "anomaly_score": row["anomaly_score"],
            "fault_type": row["fault_type"],
        }
        for row in rows
    ]
