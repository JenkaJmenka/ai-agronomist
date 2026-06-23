from typing import Optional

import pandas as pd
import streamlit as st

from src.dashboard.components.charts import (
    ANOMALY_RANGE_DEFAULT,
    ANOMALY_RANGE_OPTIONS,
    clear_anomaly_cache,
    plot_anomaly_timeline,
    query_anomaly_readings,
)
from src.dashboard.components.anomaly_table import render as render_anomaly_table
from src.dashboard.services.diagnosis_enricher import enrich_dataframe
from src.dashboard.services.incident_loader import fetch_incidents_in_range
from src.dashboard.services.api_client import DashboardApiClient
from src.dashboard.services.review_queue import (
    build_sequence_from_readings,
    register_anomaly_as_incident,
)

st.set_page_config(page_title="Anomaly Detection", layout="wide")
st.title("Anomaly Detection Timeline")


def _submit_feedback(
    api: DashboardApiClient,
    customer_id: str,
    incident_id: str,
    is_true_anomaly: bool,
    state_key: str,
    feedback_key: str,
) -> None:
    try:
        result = api.mark_anomaly({
            "incident_id": incident_id,
            "is_true_anomaly": is_true_anomaly,
            "customer_id": customer_id,
            "operator_id": "dashboard_user",
        })
        st.session_state[state_key][feedback_key] = {
            "is_true_anomaly": is_true_anomaly,
            "result": result,
        }
        if is_true_anomaly:
            st.success("Anomaly confirmed.")
        else:
            st.info("Marked as false positive.")

        if result.get("learning_triggered"):
            st.success(
                "Learning adjustment triggered — model is retraining with your feedback."
            )
        else:
            progress = result.get("progress_to_retrain", "—")
            st.info(f"Feedback saved. Learning progress: {progress} quality samples.")

    except Exception as exc:
        st.warning(
            f"Feedback could not be submitted (is the API server running?): {exc}"
        )


if "selected_tenant" not in st.session_state:
    st.warning("Please configure global settings in the sidebar of the main app.")
    st.stop()

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]
threshold = st.session_state.get("anomaly_threshold", 0.8)

# ── Sidebar: time range + refresh ────────────────────────────────────────────
st.sidebar.markdown("### Time range")
selected_range = st.sidebar.selectbox(
    "Show anomalies for",
    options=list(ANOMALY_RANGE_OPTIONS.keys()),
    index=list(ANOMALY_RANGE_OPTIONS.keys()).index(ANOMALY_RANGE_DEFAULT),
    key="anomaly_range",
)
hours_back = ANOMALY_RANGE_OPTIONS[selected_range]

st.sidebar.markdown("---")
st.sidebar.markdown("### Fault type filter")
_FAULT_TYPES = ["spike_fault", "calibration_drift", "stuck_fault", "bias_offset", "accuracy_decline"]
selected_fault_types = st.sidebar.multiselect(
    "Show fault types",
    options=_FAULT_TYPES + ["unknown"],
    default=[],
    format_func=lambda x: {
        "spike_fault": "Spike fault",
        "calibration_drift": "Calibration drift",
        "stuck_fault": "Stuck sensor",
        "bias_offset": "Bias offset",
        "accuracy_decline": "Accuracy decline",
        "unknown": "Unknown / unclassified",
    }.get(x, x),
    key="fault_type_filter",
    help="Leave empty to show all fault types",
)

st.sidebar.markdown("---")
if st.sidebar.button("Refresh data", use_container_width=True):
    clear_anomaly_cache()
    st.rerun()

# ── Load data (cached 2 min, scoped to the requested window) ─────────────────
with st.spinner(f"Loading {selected_range.lower()}…"):
    df = query_anomaly_readings(tenant, greenhouse, hours_back)

_loaded_at = pd.Timestamp.now().strftime("%H:%M:%S")

if df.empty or "anomaly_score" not in df.columns:
    st.info(f"No data found for the {selected_range.lower()}.")
    st.caption(f"Loaded at {_loaded_at} · refreshes every 2 min")
    st.stop()

anomalies_df = df[df["anomaly_score"] >= threshold].copy()

# ── Summary metrics ───────────────────────────────────────────────────────────
col1, col2, col3, col4 = st.columns(4)
col1.metric("Data points", f"{len(df):,}")
col2.metric(f"Anomalies (≥ {threshold})", len(anomalies_df))
anomaly_rate = len(anomalies_df) / len(df) if len(df) > 0 else 0
col3.metric("Anomaly rate", f"{anomaly_rate:.2%}")
if not df.empty:
    _span_hours = (
        pd.to_datetime(df["timestamp"]).max() - pd.to_datetime(df["timestamp"]).min()
    ).total_seconds() / 3600
    col4.metric("Data span", f"{_span_hours:.1f} h")

st.caption(f"Showing {selected_range.lower()} · loaded at {_loaded_at} · auto-refreshes every 2 min")

# ── Timeline chart ────────────────────────────────────────────────────────────
st.markdown("### Anomaly Score Timeline")
st.plotly_chart(plot_anomaly_timeline(df, threshold), use_container_width=True)

# ── Top anomaly table ─────────────────────────────────────────────────────────
st.markdown("### Top Anomalies")
if not anomalies_df.empty:
    top_anomalies = anomalies_df.sort_values("anomaly_score", ascending=False).head(20)

    ts_col = pd.to_datetime(top_anomalies["timestamp"])
    start_iso = ts_col.min().isoformat()
    end_iso = (ts_col.max() + pd.Timedelta(days=1)).isoformat()

    incidents = fetch_incidents_in_range(tenant, greenhouse, start_iso, end_iso)

    # Enrich with all incidents first, then filter rows by fault type so the
    # filter operates on the enriched DataFrame (not the raw incident dicts).
    enriched = enrich_dataframe(top_anomalies, incidents)

    if selected_fault_types:
        def _ft_match(ft: Optional[str]) -> bool:
            return ft in selected_fault_types or (
                ft is None and "unknown" in selected_fault_types
            )
        enriched = enriched[enriched["fault_type"].apply(_ft_match)]

    api = DashboardApiClient()
    feedback_state_key = "feedback_results"
    if feedback_state_key not in st.session_state:
        st.session_state[feedback_state_key] = {}

    def handle_feedback(
        incident_id: Optional[str],
        is_true_anomaly: bool,
        timestamp: str,
        state_key: str,
    ) -> None:
        effective_id = incident_id
        if effective_id is None:
            with st.spinner("Registering anomaly…"):
                try:
                    sequence = build_sequence_from_readings(df, pd.Timestamp(timestamp))
                    result = register_anomaly_as_incident(api, tenant, greenhouse, sequence)
                    effective_id = result.get("incident_id") or result.get("id")
                except Exception as exc:
                    st.warning(f"Could not register anomaly: {exc}")
                    st.rerun()
                    return
        if effective_id:
            _submit_feedback(
                api, tenant, effective_id, is_true_anomaly, feedback_state_key, state_key
            )
        st.rerun()

    render_anomaly_table(
        enriched,
        on_feedback=handle_feedback,
        feedback_state_key=feedback_state_key,
    )
else:
    st.success(
        f"No anomalies detected above the {threshold} threshold in the {selected_range.lower()}."
    )
