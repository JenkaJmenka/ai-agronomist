"""System Metrics — data health for the selected greenhouse + pipeline activity.

Check whether the platform is detecting anomalies reliably and whether the
sensor data for this greenhouse is healthy in the selected period. Sensor data
health is tenant/greenhouse-scoped; pipeline counts are process-wide (all
tenants) and reset on server restart — both labelled honestly below.

Data pattern matches Monitoring: full DB bounds → pick range → Apply → window fetch.
"""

import pandas as pd
import streamlit as st

from src.dashboard.components.data_availability import render_no_sensor_data_hint
from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.components.inference_results import load_inference_results
from src.dashboard.components.timeline import (
    get_timeline_bounds,
    init_monitoring_session_state,
    render_date_range_filters,
)
from src.dashboard.constants import OPERATOR_FEEDBACK_PAGE
from src.dashboard.services.anomalies_page_data import resolve_data_bounds
from src.dashboard.services.api_client import DashboardApiClient
from src.dashboard.services.window_data import (
    load_page_window,
    lookback_hours_for_window,
)

st.set_page_config(page_title="System Metrics", layout="wide")
render_global_sidebar()
st.title("System Metrics")
st.caption(
    "Check whether the platform is detecting anomalies reliably and whether "
    "sensor data for this greenhouse is healthy in the selected period."
)

if "selected_tenant" not in st.session_state:
    st.warning("Please configure global settings in the sidebar.")
    st.stop()

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]
client = DashboardApiClient(api_key=st.session_state.get("api_key"))

_saved_inf = load_inference_results(tenant, greenhouse)
inf_results = _saved_inf["results"] if _saved_inf else None

# Full DB bounds (unioned with upload bounds when upload-only) so the picker
# spans all data — the same fix as the Thresholds page.
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

# Section B — date filter (shared component; one control for the whole page)
render_date_range_filters(data_min, data_max, key_prefix="metrics")
window_start, window_end = get_timeline_bounds()

# Section A — context bar
st.markdown(
    f"**{tenant}** / Greenhouse **{greenhouse}** · Active window: "
    f"**{window_start.strftime('%d %b %H:%M')} → {window_end.strftime('%d %b %H:%M')}**"
)

# ── Section C — Data health (selected tenant/greenhouse, selected window) ──────
st.markdown("### Data health")
try:
    df, _using_fallback = load_page_window(
        tenant, greenhouse, window_start, window_end, inf_results=inf_results
    )
except Exception as exc:
    st.warning(f"Could not load sensor data for data health: {exc}")
    df = pd.DataFrame()

if df.empty:
    st.warning(
        "No data in the selected window. "
        f"Available: {data_min.strftime('%d %b %Y')} → {data_max.strftime('%d %b %Y')}."
    )
else:
    total = len(df)
    scored = (
        int(pd.to_numeric(df["anomaly_score"], errors="coerce").notna().sum())
        if "anomaly_score" in df.columns
        else 0
    )
    coverage = scored / total if total else 0.0
    span_hours = (
        pd.to_datetime(df["timestamp"]).max() - pd.to_datetime(df["timestamp"]).min()
    ).total_seconds() / 3600

    h1, h2, h3, h4, h5 = st.columns(5)
    h1.metric("Total readings", f"{total:,}")
    h2.metric("With anomaly score", f"{scored:,}")
    h3.metric("Score coverage", f"{coverage:.0%}")
    h4.metric("LSTM windows (20 readings each)", f"{total // 20:,}")
    h5.metric("Window span", f"{span_hours:.1f} h")

    if scored == 0:
        st.info(
            "Readings exist but none are scored. Run inference via the Anomaly "
            "Detection upload, or wait for pipeline scoring."
        )

# ── Section D — Pipeline activity (process-wide) ──────────────────────────────
st.markdown("---")
st.markdown("### Pipeline activity")
st.info(
    "Pipeline counts reflect all detections on this API server (all tenants). "
    "They reset when the server restarts."
)

# Single time control: derive the lookback from the selected window (no second slider).
lookback_hours = lookback_hours_for_window(window_start, window_end)
st.caption(f"Lookback derived from the selected window: {lookback_hours} h")

try:
    metrics = client.get_monitoring_metrics(hours=lookback_hours)
except Exception as exc:
    metrics = None
    st.warning("Could not load pipeline metrics — start the FastAPI server.")
    st.caption(f"Details: {exc}")

if metrics is not None:
    total_detections = int(metrics.get("total_detections", 0) or 0)
    true_positives = int(metrics.get("true_positives", 0) or 0)
    false_positives = int(metrics.get("false_positives", 0) or 0)

    show_rates = total_detections > 0
    cols = st.columns(4 if show_rates else 2)
    cols[0].metric("Total detections", f"{total_detections:,}")
    cols[1].metric("Operator labels", f"{true_positives + false_positives:,}")
    if show_rates:
        cols[2].metric(
            "False positive rate (operator-marked / total detections)",
            f"{metrics.get('false_positive_rate', 0.0):.1%}",
        )
        cols[3].metric("Avg confidence", f"{metrics.get('avg_confidence', 0.0):.2f}")

    # Section E — operator feedback nudge
    if true_positives + false_positives == 0:
        st.info(
            "No operator labels yet. Confirm or reject anomalies on Operator "
            "Feedback to improve false-positive tracking."
        )
        st.page_link(
            OPERATOR_FEEDBACK_PAGE, label="Go to Operator Feedback →", icon="📝"
        )

    patterns = metrics.get("pattern_distribution") or {}
    conf_dist = metrics.get("confidence_distribution") or {}
    if patterns or conf_dist:
        with st.expander("Details", expanded=False):
            if patterns:
                st.markdown("**Pattern distribution**")
                st.dataframe(
                    pd.DataFrame(
                        [{"pattern": k, "count": v} for k, v in patterns.items()]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            if conf_dist:
                st.markdown("**Confidence distribution**")
                st.dataframe(
                    pd.DataFrame(
                        [{"bucket": k, "count": v} for k, v in conf_dist.items()]
                    ),
                    hide_index=True,
                    width="stretch",
                )
