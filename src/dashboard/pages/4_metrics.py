import streamlit as st
from src.dashboard.components.charts import apply_date_filter, query_sensor_readings
from src.dashboard.services.api_client import DashboardApiClient
import pandas as pd

st.set_page_config(page_title="System Metrics", layout="wide")
st.title("System Metrics & Model Performance")

if "selected_tenant" not in st.session_state:
    st.warning("Please configure global settings in the sidebar of the main app.")
    st.stop()

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]
api_key = st.session_state.get("api_key")
client = DashboardApiClient(api_key=api_key)

st.markdown("### Pipeline Metrics")
hours = st.slider("Lookback (hours)", min_value=1, max_value=8760, value=24, step=24)
try:
    pipeline_metrics = client.get_monitoring_metrics(hours=hours)
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Total detections", pipeline_metrics.get("total_detections", 0))
    p2.metric(
        "False positive rate",
        f"{pipeline_metrics.get('false_positive_rate', 0.0):.1%}",
    )
    p3.metric("Avg confidence", f"{pipeline_metrics.get('avg_confidence', 0.0):.2f}")
    p4.metric(
        "Operator labels",
        pipeline_metrics.get("true_positives", 0)
        + pipeline_metrics.get("false_positives", 0),
    )

    col_tp, col_fp = st.columns(2)
    col_tp.metric("True positives", pipeline_metrics.get("true_positives", 0))
    col_fp.metric("False positives", pipeline_metrics.get("false_positives", 0))

    patterns = pipeline_metrics.get("pattern_distribution") or {}
    if patterns:
        st.markdown("**Pattern distribution**")
        st.dataframe(
            pd.DataFrame(
                [{"pattern": k, "count": v} for k, v in patterns.items()]
            ),
            use_container_width=True,
        )

    conf_dist = pipeline_metrics.get("confidence_distribution") or {}
    if conf_dist:
        st.markdown("**Confidence distribution**")
        st.dataframe(
            pd.DataFrame(
                [{"bucket": k, "count": v} for k, v in conf_dist.items()]
            ),
            use_container_width=True,
        )
except Exception as exc:
    st.warning(
        "Could not load pipeline metrics from the API. "
        "Start the FastAPI server and ensure it is reachable."
    )
    st.caption(f"Details: {exc}")
    st.caption("See docs/deployment/ops-runbook.md for startup steps.")

st.markdown("---")
st.markdown("### Sensor Coverage")

with st.spinner("Compiling sensor coverage..."):
    df = query_sensor_readings(tenant, greenhouse)

if df.empty:
    st.info("No sensor data available to calculate coverage metrics.")
else:
    df = apply_date_filter(
        df,
        st.session_state.get("start_date"),
        st.session_state.get("end_date"),
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Data Points", f"{len(df):,}")
    if not df.empty:
        date_range_str = (
            f"{df['timestamp'].min().strftime('%Y-%m-%d')} to "
            f"{df['timestamp'].max().strftime('%Y-%m-%d')}"
        )
    else:
        date_range_str = "—"
    col2.metric("Date Range Covered", date_range_str)
    col3.metric("Greenhouses Monitored", "1 (Selected)")
    col4.metric("Sequences Processed", f"{len(df) // 20:,}")

st.markdown("---")
st.markdown("### Model Information")
model_col1, model_col2 = st.columns(2)
with model_col1:
    st.info("**Model Architecture:** LSTM-VAE + ensemble detectors")
    st.info("**Input Shape:** 4 sensors × 20 timesteps")
with model_col2:
    st.info("**Metrics source:** GET /api/monitoring/metrics")
    st.info("**Config:** docs/deployment/monitoring-dashboard-config.yaml")

st.markdown("---")
st.markdown("### Export Data")
if not df.empty:
    csv = df.to_csv(index=False)
    st.download_button(
        label="Download CSV Extract",
        data=csv,
        file_name=f"export_{tenant}_{greenhouse}.csv",
        mime="text/csv",
    )
