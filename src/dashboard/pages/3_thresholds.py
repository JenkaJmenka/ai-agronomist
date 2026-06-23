import streamlit as st
from src.dashboard.components.charts import apply_date_filter, query_sensor_readings
import pandas as pd

st.set_page_config(page_title="Threshold Management", layout="wide")
st.title("🎛️ Threshold Management")

st.markdown("""
Adjust the sensitivity of the LSTM-VAE anomaly detector. 
A lower threshold will flag more events as anomalies, while a higher threshold will only catch extreme outliers.
""")

if 'selected_tenant' not in st.session_state:
    st.warning("Please configure global settings in the sidebar of the main app.")
    st.stop()

tenant = st.session_state['selected_tenant']
greenhouse = st.session_state['selected_greenhouse']

# Load current threshold
current_threshold = st.session_state.get('anomaly_threshold', 0.8)

st.info(f"Current System Threshold: **{current_threshold:.2f}**")

new_threshold = st.slider(
    "Set New Anomaly Threshold",
    min_value=0.0,
    max_value=1.0,
    value=float(current_threshold),
    step=0.05
)

with st.spinner("Calculating impact..."):
    df = query_sensor_readings(tenant, greenhouse)

if not df.empty and 'anomaly_score' in df.columns:
    df = apply_date_filter(
        df,
        st.session_state.get("start_date"),
        st.session_state.get("end_date"),
    )
    anomalies_before = len(df[df['anomaly_score'] >= current_threshold])
    anomalies_after = len(df[df['anomaly_score'] >= new_threshold])
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Anomalies at Current Threshold", anomalies_before)
    col2.metric("Anomalies at New Threshold", anomalies_after, delta=anomalies_after - anomalies_before, delta_color="inverse")
    
    # Calculate recommended based on 95th percentile
    if len(df) > 0:
        recommended = df['anomaly_score'].quantile(0.95)
        col3.metric("Recommended (95th %ile)", f"{recommended:.2f}")

st.markdown("---")
if st.button("💾 Save New Threshold", type="primary"):
    st.session_state['anomaly_threshold'] = new_threshold
    st.success(f"Threshold successfully updated to {new_threshold:.2f} for future inference.")
    st.rerun()
