"""Shared helpers when DB-backed pages have no sensor readings."""

from __future__ import annotations

import streamlit as st

from src.dashboard.services.upload_results_store import read_upload_cache


def render_no_sensor_data_hint(tenant_id: str, greenhouse_id: str) -> None:
    """Explain why pages are empty and how to fix it (cold-run vs persisted data)."""
    cached = read_upload_cache(tenant_id, greenhouse_id)
    if not cached:
        st.info(
            "No sensor data found for this tenant and greenhouse. "
            "Upload a CSV on **Anomaly Detection** (enable **Load data into platform**) "
            "or use **Upload & Operations → Upload & Train Model**."
        )
        return

    results = cached.get("results") or {}
    filename = cached.get("filename") or "last upload"
    detected = int(results.get("anomalies_detected", 0) or 0)

    if results.get("data_persisted"):
        st.warning(
            f"Cold-run upload **{filename}** was persisted ({detected} anomalies detected) "
            "but no readings are visible for the current tenant/greenhouse. "
            "Confirm the sidebar selection matches the upload target, then use "
            "**Upload & Operations → Refresh data** or re-upload the CSV."
        )
        return

    st.info(
        f"Cold-run upload **{filename}** found {detected} anomalies but readings were "
        "**not saved to the database**. Monitoring, Climate, Thresholds, and Metrics "
        "only read persisted data. Re-upload on **Anomaly Detection** with "
        "**Load data into platform** enabled, or use **Upload & Operations → Upload & Train Model**."
    )
