import os
import sys

import streamlit as st

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

st.set_page_config(page_title="AI Agronomist", layout="wide", page_icon="🌱")

home = st.Page("home.py", title="Home", icon="🌱", default=True)

active_monitoring = [
    st.Page(
        "views/1_Active_Monitoring/1_Monitoring.py",
        title="Monitoring",
        icon="📊",
    ),
    st.Page(
        "views/1_Active_Monitoring/2_Climate_Data.py",
        title="Climate Data",
        icon="🌡️",
    ),
    st.Page(
        "views/1_Active_Monitoring/3_Gas_Monitoring.py",
        title="Gas Monitoring",
        icon="🧪",
    ),
]

detection_pages = [
    st.Page("views/2_anomalies.py", title="Anomaly Detection"),
    st.Page("views/3_thresholds.py", title="Thresholds"),
    st.Page("views/4_metrics.py", title="System Metrics"),
]

operations_pages = [
    st.Page(
        "views/5_upload_operations.py",
        title="Upload & Operations",
        icon="📤",
    ),
    st.Page(
        "views/6_job_monitoring.py",
        title="Job Monitoring",
        icon="⏱️",
    ),
]

feedback_pages = [
    st.Page(
        "views/99_Operator_Feedback.py",
        title="Operator Feedback",
        icon="📝",
    ),
]

pg = st.navigation(
    {
        "": [home],
        "Active Monitoring": active_monitoring,
        "Detection": detection_pages,
        "Operations": operations_pages,
        "Feedback": feedback_pages,
    }
)

pg.run()
