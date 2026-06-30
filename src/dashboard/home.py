import streamlit as st

from src.dashboard.components.global_sidebar import render_global_sidebar

render_global_sidebar()

st.title("🌱 AI Agronomist - Greenhouse Anomaly Detection")

st.markdown("""
Welcome to the MVP dashboard! This interface connects directly to our FastAPI backend and SQLite database to visualize sensor readings and monitor the LSTM-VAE anomaly detection model in real time.

Use the sidebar to select your tenant and greenhouse, then navigate through the pages.
""")

st.info("👈 Select a page from the sidebar to begin.")
