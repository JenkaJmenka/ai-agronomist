"""Threshold Management — a dashboard-filter preview tool.

Adjust how sensitive the dashboard is when flagging anomalies and preview the
impact before saving. Saving writes a session-only override used by the other
dashboard views; it does NOT retrain the model or change API/DB thresholds.

Data pattern (matches Monitoring / Anomaly Detection): resolve full DB bounds →
user picks a range → Apply → fetch exactly that window (with upload-score merge).
"""

import pandas as pd
import streamlit as st

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
from src.dashboard.constants import ANOMALIES_PAGE
from src.dashboard.services.anomalies_page_data import resolve_data_bounds
from src.dashboard.services.anomaly_events import (
    count_anomalies_at_threshold,
    score_summary,
)
from src.dashboard.services.window_data import load_page_window, threshold_slider_range

st.set_page_config(page_title="Threshold Management", layout="wide")
render_global_sidebar()
st.title("🎛️ Threshold Management")
st.caption(
    "Adjust how sensitive the dashboard is when flagging anomalies — preview the "
    "impact before saving."
)

if "selected_tenant" not in st.session_state:
    st.warning("Please configure global settings in the sidebar.")
    st.stop()

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]

_saved_inf = load_inference_results(tenant, greenhouse)
inf_results = _saved_inf["results"] if _saved_inf else None

# Threshold resolution order (kept compatible with the Anomaly page's
# _effective_anomaly_threshold):
#   1. st.session_state["anomaly_threshold"]  — saved on this page this session
#   2. inf_results["threshold_used"]          — calibrated from the last upload
#   3. DEFAULT_ANOMALY_THRESHOLD              — config default
_override = st.session_state.get("anomaly_threshold")
saved_threshold = (
    float(_override) if _override is not None
    else calibrated_threshold_default(inf_results)
)

# Full DB bounds, unioned with upload bounds when inference lives only in the
# upload cache — so the date picker spans all available data, not a 24 h tail.
try:
    bounds = resolve_data_bounds(tenant, greenhouse, inf_results)
except Exception as exc:
    st.error(f"Could not load sensor data — is the API server running? Details: {exc}")
    st.stop()

if bounds is None:
    render_no_sensor_data_hint(tenant, greenhouse)
    st.stop()

data_min, data_max = bounds
init_monitoring_session_state(tenant, greenhouse, data_min, data_max)

# Section A — context bar
render_date_range_filters(data_min, data_max, key_prefix="thresholds")
window_start, window_end = get_timeline_bounds()

st.markdown(
    f"**{tenant}** / Greenhouse **{greenhouse}** · Active window: "
    f"**{window_start.strftime('%d %b %H:%M')} → {window_end.strftime('%d %b %H:%M')}** "
    f"· Saved threshold: **{saved_threshold:.2f}**"
)
st.caption(
    "Saved threshold applies to dashboard views (Anomalies, Climate, Feedback) in "
    "this browser session. It does not change the detection model."
)

# Section B — window fetch (re-fetch on Apply; never filter a stale 24 h frame)
try:
    df, _using_fallback = load_page_window(
        tenant, greenhouse, window_start, window_end,
        require_scores=True, inf_results=inf_results,
    )
except Exception as exc:
    st.error(f"Could not load sensor data — is the API server running? Details: {exc}")
    st.stop()

if df.empty:
    st.warning(
        "No data in the selected window. "
        f"Available: {data_min.strftime('%d %b %Y')} → {data_max.strftime('%d %b %Y')}."
    )
    st.stop()

total_points = len(df)
scores = df["anomaly_score"] if "anomaly_score" in df.columns else pd.Series(dtype=float)
stats = score_summary(scores)

if stats["scored"] == 0:
    st.warning(
        "No anomaly scores in this window — the slider needs scored readings to "
        "preview flags. Upload a CSV with inference on the Anomaly Detection page."
    )
    st.page_link(ANOMALIES_PAGE, label="Go to Anomaly Detection →", icon="🔎")
    st.stop()

# Section C — score context caption (metrics rendered under the slider so the
# preview count reflects the current slider value)
st.caption(
    f"Score range in window: **{stats['min']:.3f}** → **{stats['max']:.3f}** "
    f"(median **{stats['median']:.3f}**)"
)
_valid_scores = pd.to_numeric(scores, errors="coerce").dropna()
_p90, _p95 = float(_valid_scores.quantile(0.90)), float(_valid_scores.quantile(0.95))
st.caption(f"Distribution markers — p90 **{_p90:.3f}** · p95 **{_p95:.3f}**")

# Section D — threshold slider scaled to the real score range
slider_min, slider_max, slider_step = threshold_slider_range(scores, saved_threshold)
new_threshold = st.slider(
    "Preview threshold",
    min_value=slider_min,
    max_value=slider_max,
    value=min(max(saved_threshold, slider_min), slider_max),
    step=slider_step,
    key="threshold_preview_slider",
    help=(
        "Drag to see how many readings would be flagged. Click Save to use this "
        "value across dashboard pages."
    ),
)

# Section C — score-context metrics
anomalies_saved = count_anomalies_at_threshold(scores, saved_threshold)
anomalies_preview = count_anomalies_at_threshold(scores, new_threshold)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Points in window", f"{total_points:,}")
col2.metric("Points with scores", f"{stats['scored']:,}")
col3.metric(f"Flagged at saved ({saved_threshold:.2f})", f"{anomalies_saved:,}")
col4.metric(
    f"Flagged at preview ({new_threshold:.2f})",
    f"{anomalies_preview:,}",
    delta=anomalies_preview - anomalies_saved,
    delta_color="inverse",
)

if stats["min"] == stats["max"]:
    st.warning(
        f"All scored points have the same anomaly score ({stats['min']:.3f}). "
        "Counts change only when the threshold crosses that value."
    )
elif anomalies_preview == anomalies_saved and new_threshold != saved_threshold:
    st.info(
        "Preview count unchanged — no scores fall between the saved and preview "
        "thresholds."
    )

if stats["scored"] < total_points:
    st.caption(
        f"{total_points - stats['scored']:,} points have no score and are never flagged."
    )

# Section E — save (session-only)
st.markdown("---")
if st.button("💾 Save threshold", type="primary"):
    st.session_state["anomaly_threshold"] = new_threshold
    st.success(f"Threshold {new_threshold:.2f} saved for this session.")
    st.rerun()

# Section F — quick link
st.page_link(
    ANOMALIES_PAGE, label="View flagged points on Anomaly Detection →", icon="🔎"
)
