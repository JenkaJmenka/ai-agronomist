import datetime
from typing import Any, Dict

import pandas as pd
import streamlit as st

from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.services.api_client import DashboardApiClient

st.set_page_config(
    page_title="Job Monitoring",
    page_icon="⏱️",
    layout="wide",
    initial_sidebar_state="expanded",
)

_client = DashboardApiClient()


def _format_datetime(iso_str: str | None) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = pd.to_datetime(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(iso_str)


def _render_job_card(job: Dict[str, Any]) -> None:
    job_id = job.get("id", "Unknown")
    job_type = job.get("job_type", "Unknown")
    status = job.get("status", "Unknown")
    created_at = _format_datetime(job.get("created_at"))
    finished_at = _format_datetime(job.get("finished_at"))
    
    total = job.get("total_portions", 0) or 0
    completed = job.get("completed_portions", 0) or 0
    failed = job.get("failed_portions", 0) or 0
    
    # Calculate progress
    if total > 0:
        progress_pct = (completed + failed) / total * 100.0
    elif status == "completed":
        progress_pct = 100.0
    else:
        progress_pct = 0.0

    st.markdown(f"### Job: `{job_id}` ({job_type})")
    
    col1, col2, col3, col4, col5 = st.columns([2, 2, 2, 2, 2])
    with col1:
        st.metric("Status", status.upper())
    with col2:
        st.metric("Progress", f"{progress_pct:.1f}%")
    with col3:
        st.metric("Created", created_at)
    with col4:
        st.metric("Finished", finished_at if finished_at != "N/A" else "--")
    with col5:
        if status in ("pending", "preparing", "running", "finalizing"):
            st.write("") # spacing
            if st.button("Terminate", key=f"term_{job_id}", type="primary"):
                try:
                    _client.cancel_job(job_id)
                    st.toast("Job terminated successfully.", icon="🛑")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to terminate job: {e}")

    # If the job is active, show a progress bar
    if status == "pending":
        st.info("Waiting in queue...")
    elif status in ("preparing", "running", "finalizing"):
        if total == 0:
            st.info("Training cold-start model (Pass 1 & Pass 2)... This may take a few minutes for large files.")
        else:
            st.progress(min(int(progress_pct), 100), text=f"Processing {completed}/{total} portions...")
        
    if failed > 0:
        st.error(f"{failed} portions failed during execution.")
    if job.get("error"):
        st.error(f"Error: {job.get('error')}")
        
    st.markdown("---")


@st.fragment(run_every=datetime.timedelta(seconds=5))
def _render_active_jobs_poller(tenant_id: str, greenhouse_id: str) -> None:
    st.subheader("Active & Recent Jobs")
    jobs = _client.list_jobs(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        limit=20,
    )
    
    if not jobs:
        st.info("No recent jobs found for this tenant and greenhouse.")
        return
        
    for job in jobs:
        _render_job_card(job)


def main() -> None:
    st.title("Job Monitoring ⏱️")
    st.markdown(
        "Monitor the progress of background tasks such as bulk CSV uploads and model training. "
        "Active jobs refresh automatically."
    )
    
    render_global_sidebar()
    tenant = st.session_state.get("selected_tenant")
    greenhouse = st.session_state.get("selected_greenhouse")
    
    if not tenant or not greenhouse:
        st.warning("Please select a Tenant and Greenhouse from the sidebar.")
        return
        
    _render_active_jobs_poller(tenant, greenhouse)


if __name__ == "__main__":
    main()
