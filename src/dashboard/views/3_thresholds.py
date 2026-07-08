import streamlit as st
from src.dashboard.components.charts import apply_datetime_filter, query_sensor_readings
from src.dashboard.components.data_availability import render_no_sensor_data_hint
from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.components.inference_results import (
    calibrated_threshold_default,
    load_inference_results,
)
from src.dashboard.components.timeline import (
    get_timeline_bounds,
    init_monitoring_session_state,
    render_date_range_filters,
)
from src.dashboard.services.anomaly_events import (
    count_anomalies_at_threshold,
    score_summary,
)
import pandas as pd

st.set_page_config(page_title="Threshold Management", layout="wide")
render_global_sidebar()
st.title("🎛️ Threshold Management")

st.markdown("""
Adjust the sensitivity of the LSTM-VAE anomaly detector. 
A lower threshold will flag more events as anomalies, while a higher threshold will only catch extreme outliers.
""")

if 'selected_tenant' not in st.session_state:
    st.warning("Please configure global settings in the sidebar.")
    st.stop()

tenant = st.session_state['selected_tenant']
greenhouse = st.session_state['selected_greenhouse']
# Default to the model's calibrated cutoff (scores top out ~0.2, so the static
# 0.8 default would flag nothing); an explicit saved threshold still wins.
_saved_inf = load_inference_results(tenant, greenhouse)
_inf_results = _saved_inf["results"] if _saved_inf else None
_override = st.session_state.get('anomaly_threshold')
saved_threshold = (
    float(_override) if _override is not None
    else calibrated_threshold_default(_inf_results)
)

with st.spinner("Loading sensor data…"):
    try:
        df = query_sensor_readings(tenant, greenhouse)
    except Exception as exc:
        st.error(
            f"Could not load sensor data — is the API server running? "
            f"Details: {exc}"
        )
        st.stop()

if df.empty:
    render_no_sensor_data_hint(tenant, greenhouse)
    st.stop()

_ts = pd.to_datetime(df["timestamp"])
init_monitoring_session_state(tenant, greenhouse, _ts.min(), _ts.max())
render_date_range_filters(_ts.min(), _ts.max(), key_prefix="thresholds")
window_start, window_end = get_timeline_bounds()
df = apply_datetime_filter(df, window_start, window_end)

st.info(f"Saved system threshold: **{saved_threshold:.2f}**")

if df.empty or 'anomaly_score' not in df.columns:
    st.info("No scored data in the selected date window.")
    st.stop()

scores = df["anomaly_score"]
stats = score_summary(scores)

new_threshold = st.slider(
    "Preview threshold",
    min_value=0.0,
    max_value=1.0,
    value=saved_threshold,
    step=0.01,
    key="threshold_preview_slider",
    help="Drag to preview how many points would be flagged before saving. "
    "LSTM-VAE scores are small (typically 0–0.2), so keep the threshold low.",
)

anomalies_saved = count_anomalies_at_threshold(scores, saved_threshold)
anomalies_preview = count_anomalies_at_threshold(scores, new_threshold)
total_points = len(df)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Data points in window", f"{total_points:,}")
col2.metric("Points with scores", f"{stats['scored']:,}")
col3.metric(
    f"Flagged at saved ({saved_threshold:.2f})",
    f"{anomalies_saved:,}",
)
col4.metric(
    f"Flagged at preview ({new_threshold:.2f})",
    f"{anomalies_preview:,}",
    delta=anomalies_preview - anomalies_saved,
    delta_color="inverse",
)

if stats["scored"] > 0:
    st.caption(
        f"Score range in window: **{stats['min']:.3f}** → **{stats['max']:.3f}** "
        f"(median **{stats['median']:.3f}**)"
    )
    if stats["min"] == stats["max"]:
        st.warning(
            f"All scored points have the same anomaly score ({stats['min']:.3f}). "
            "Counts will only change when the threshold crosses that value."
        )
    elif anomalies_preview == anomalies_saved and new_threshold != saved_threshold:
        st.info(
            "Preview count unchanged — no scores fall between the saved and preview thresholds."
        )

if stats["scored"] == 0:
    st.warning("No anomaly scores in this window. Upload data with inference or run scoring first.")
elif stats["scored"] < total_points:
    st.caption(
        f"{total_points - stats['scored']:,} points have no score and are never flagged."
    )

if len(df) > 0 and stats["scored"] > 0:
    recommended = pd.to_numeric(scores, errors="coerce").quantile(0.95)
    st.metric("Recommended (95th percentile of scores)", f"{recommended:.2f}")

st.markdown("---")
if st.button("💾 Save preview threshold", type="primary"):
    st.session_state['anomaly_threshold'] = new_threshold
    st.success(f"Threshold saved as {new_threshold:.2f} for dashboard filtering.")
    st.rerun()
