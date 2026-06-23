import streamlit as st
import sys
import os
from datetime import datetime, timedelta

# Ensure src is in Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.dashboard.components.charts import (
    clear_sensor_cache,
    get_available_greenhouses,
    get_data_date_bounds,
)

st.set_page_config(page_title="AI Agronomist", layout="wide", page_icon="🌱")

st.title("🌱 AI Agronomist - Greenhouse Anomaly Detection")

st.markdown("""
Welcome to the MVP dashboard! This interface connects directly to our FastAPI backend and SQLite database to visualize sensor readings and monitor the LSTM-VAE anomaly detection model in real time.

Please use the sidebar to select your tenant and greenhouse, and navigate through the pages.
""")

# Sidebar Global Settings
st.sidebar.header("Global Settings")
tenant = st.sidebar.selectbox("Select Tenant", ["tenant_1", "tenant_2", "tenant_3"], index=0)
_gh_options = get_available_greenhouses(tenant)
_gh_default = st.session_state.get("selected_greenhouse", _gh_options[0])
_gh_idx = _gh_options.index(_gh_default) if _gh_default in _gh_options else 0
greenhouse = st.sidebar.selectbox("Select Greenhouse", _gh_options, index=_gh_idx)

# Default date range to actual data bounds when available (seeded data ages out of
# a rolling "last 7 days" window otherwise).
data_min, data_max = get_data_date_bounds(tenant, greenhouse)
if data_min and data_max:
    default_range = (data_min, data_max)
else:
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    default_range = (start_date.date(), end_date.date())

date_range = st.sidebar.date_input(
    "Date Range",
    value=default_range,
    key=f"date_range_{tenant}_{greenhouse}",
)

# Save to session state
st.session_state['selected_tenant'] = tenant
st.session_state['selected_greenhouse'] = greenhouse
if len(date_range) == 2:
    st.session_state['start_date'] = date_range[0]
    st.session_state['end_date'] = date_range[1]

if data_min and data_max:
    st.sidebar.caption(f"Data available: {data_min} → {data_max}")

st.sidebar.markdown("---")
api_key = st.sidebar.text_input(
    "API Key (optional)",
    type="password",
    help="Required when API auth is enabled (X-API-Key header)",
)
st.session_state["api_key"] = api_key or None

st.sidebar.markdown("---")
st.sidebar.page_link("pages/6_operator_feedback.py", label="Operator Feedback", icon="📝")

if st.sidebar.button("Refresh Data"):
    clear_sensor_cache()
    st.rerun()

st.info("👈 Select a page from the sidebar to begin.")
