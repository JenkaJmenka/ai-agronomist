import streamlit as st
import pandas as pd

from src.dashboard.constants import DEFAULT_ANOMALY_THRESHOLD

from src.dashboard.components.diagnosis_card import render as render_diagnosis_card
from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.components.learning_progress import render as render_learning_progress
from src.dashboard.components.sequence_chart import render as render_sequence_chart
from src.dashboard.services.api_client import DashboardApiClient
from src.dashboard.services.review_queue import (
    api_is_reachable,
    list_pending_anomalies,
    register_anomaly_as_incident,
)

st.set_page_config(page_title="Operator Feedback", layout="wide", page_icon="📝")
render_global_sidebar()
st.title("Operator Feedback")

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state.get("selected_greenhouse", "gh_A")
threshold = st.session_state.get("anomaly_threshold", DEFAULT_ANOMALY_THRESHOLD)
start_date = st.session_state.get("start_date")
end_date = st.session_state.get("end_date")
api_key = st.session_state.get("api_key")
client = DashboardApiClient(api_key=api_key)

st.markdown(
    "Review open incidents, label true/false anomalies, and track learning."
)

# ── Clear all feedback ────────────────────────────────────────────────────────
with st.expander("⚠️ Clear all feedback & incidents"):
    st.warning(
        "This permanently deletes **all incidents and operator feedback** for "
        f"tenant `{tenant}` / greenhouse `{greenhouse}`."
    )
    if st.button("Clear all feedback", type="secondary", key="clear_feedback_btn"):
        try:
            from src.dashboard.services.http_utils import api_delete

            r = api_delete(
                f"{client.base_url}/api/incidents/clear",
                params={"tenant_id": tenant, "greenhouse_id": greenhouse},
            )
            d = r.json()
            st.success(
                f"Cleared {d.get('deleted_incidents', 0)} incident(s)."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Clear failed: {exc}")

with st.expander("🤖 Clear Trained Models"):
    st.warning(
        "This permanently deletes **all custom trained models** for "
        f"tenant `{tenant}`. The system will revert to the baseline model and you will need to upload clean data to retrain."
    )
    if st.button("Clear Models", type="secondary", key="clear_models_btn"):
        try:
            from src.dashboard.services.http_utils import api_delete

            r = api_delete(
                f"{client.base_url}/api/learning/models",
                params={"tenant_id": tenant},
            )
            d = r.json()
            st.success(
                f"Cleared {d.get('deleted_versions', 0)} model(s)."
            )
            import time; time.sleep(1)
            st.rerun()
        except Exception as exc:
            st.error(f"Clear models failed: {exc}")

api_ok, api_err = api_is_reachable(client)
if not api_ok:
    st.error(
        "FastAPI backend is not reachable. Start it in another terminal:\n\n"
        "`venv/bin/python -m uvicorn src.api.main:app --reload`"
    )
    st.caption(f"Details: {api_err}")
    st.stop()

try:
    open_incidents = client.list_open_incidents(tenant, greenhouse)
except Exception as exc:
    st.error(f"Could not load incidents: {exc}")
    open_incidents = []

pending = list_pending_anomalies(
    tenant,
    greenhouse,
    threshold,
    start_date,
    end_date,
    existing_incidents=open_incidents,
)

st.subheader("Review queue")

if not open_incidents and pending:
    st.warning(
        f"**{len(pending)} anomaly/anomalies** detected in sensor data but not yet "
        "registered as incidents. Incidents are created when comprehensive inference runs."
    )
    pending_df = pd.DataFrame(
        [
            {
                "timestamp": p["timestamp"].strftime("%d %b %H:%M"),
                "score": f"{p['anomaly_score']:.3f}",
                "temp (°C)": f"{p['temperature']:.1f}" if p.get("temperature") is not None else "—",
                "hum (%)": f"{p['humidity']:.1f}" if p.get("humidity") is not None else "—",
                "CO₂ (ppm)": f"{p['co2']:.0f}" if p.get("co2") is not None else "—",
                "light (lux)": f"{p['light']:.0f}" if p.get("light") is not None else "—",
            }
            for p in pending
        ]
    )
    st.dataframe(pending_df, width="stretch")

    if st.button("Register all for review", type="primary"):
        created = 0
        errors: list[str] = []
        with st.spinner("Running comprehensive inference…"):
            for item in pending:
                try:
                    register_anomaly_as_incident(
                        client, tenant, greenhouse, item["sequence"]
                    )
                    created += 1
                except Exception as exc:
                    errors.append(str(exc))
        if created:
            st.success(f"Registered {created} incident(s). Refreshing queue…")
            st.rerun()
        if errors:
            st.error("Some registrations failed:\n" + "\n".join(errors[:3]))

elif not open_incidents:
    st.info(
        "No open incidents and no anomalies above the current threshold "
        f"({threshold}). Lower the threshold in the sidebar or upload/score more data."
    )
else:
    def _fmt_dt(raw) -> str:
        if not raw:
            return "—"
        try:
            return pd.to_datetime(raw).strftime("%d %b %H:%M")
        except Exception:
            return str(raw)

    def _fmt_score(raw) -> str:
        try:
            return f"{float(raw):.3f}"
        except (TypeError, ValueError):
            return "—"

    def _humanize(label: str | None) -> str:
        if not label:
            return "—"
        return label.replace("_", " ").capitalize()

    _SENSOR_NAMES = ["temperature", "humidity", "co2", "light"]
    _SENSOR_UNITS = {"temperature": "°C", "humidity": "%", "co2": " ppm", "light": " lux"}

    def _sensor_info(inc: dict) -> tuple[str, str]:
        details = inc.get("root_cause_details") or {}
        affected: list = details.get("affected_sensors") or []
        sequence: list = inc.get("sequence") or []
        last_step: list = sequence[-1] if sequence else []
        values: dict = {
            name: last_step[idx]
            for idx, name in enumerate(_SENSOR_NAMES)
            if idx < len(last_step)
        }
        if not affected:
            affected = [
                s for s, v in (details.get("attribution") or {}).items()
                if float(v or 0) >= 0.1
            ]
        if not affected:
            return "—", "—"
        sensor_str = ", ".join(affected)
        value_parts = []
        for s in affected:
            v = values.get(s)
            if v is not None:
                value_parts.append(f"{float(v):.1f}{_SENSOR_UNITS.get(s, '')}")
        value_str = ", ".join(value_parts) if value_parts else "—"
        return sensor_str, value_str

    def _description(inc: dict) -> str:
        details = inc.get("root_cause_details") or {}
        hint_summary = details.get("hint_summary") or ""
        if hint_summary:
            return hint_summary[:120]
        reasoning = details.get("reasoning") or details.get("pattern_explanation") or ""
        if reasoning:
            return reasoning[:120]
        affected = details.get("affected_sensors") or []
        pattern = details.get("pattern_type") or inc.get("root_cause") or ""
        if affected and pattern:
            return f"{_humanize(pattern)} on {', '.join(affected)}"
        return "—"

    queue_rows = []
    for inc in open_incidents:
        sensor_type, sensor_value = _sensor_info(inc)
        queue_rows.append({
            "detected": _fmt_dt(inc.get("detected_at")),
            "score": _fmt_score(inc.get("anomaly_score")),
            "pattern": _humanize(
                (inc.get("root_cause_details") or {}).get("pattern_type")
                or inc.get("pattern_type")
            ),
            "root cause": _humanize(inc.get("root_cause")),
            "description": _description(inc),
            "sensor type": sensor_type,
            "sensor value": sensor_value,
        })

    queue_df = pd.DataFrame(queue_rows, index=range(1, len(queue_rows) + 1))
    st.dataframe(queue_df, width="stretch")

    incident_ids = [inc["id"] for inc in open_incidents if inc.get("id")]
    # index_map keeps the #-number (1-based) in sync with the table row
    index_map = {inc["id"]: str(i + 1) for i, inc in enumerate(open_incidents) if inc.get("id")}
    selected_id = st.selectbox(
        "Select incident",
        options=incident_ids or [""],
        format_func=lambda iid: index_map.get(iid, iid) if iid else "—",
    )

    if selected_id:
        try:
            incident = client.get_incident(selected_id, tenant)
        except Exception as exc:
            st.error(f"Failed to load incident: {exc}")
            incident = {}

        col_left, col_right = st.columns([2, 1])
        with col_left:
            render_sequence_chart(
                incident.get("sequence"), title=f"Incident {selected_id}"
            )
            render_diagnosis_card(incident)
        with col_right:
            try:
                stats = client.statistics(tenant)
                prog = client.learning_progress(tenant)
            except Exception:
                stats, prog = {}, {}
            render_learning_progress(progress=prog, statistics=stats)

        st.subheader("Label incident")
        with st.form("label_form"):
            is_true = st.radio(
                "Verdict", ["True anomaly", "False positive"], horizontal=True
            )
            operator_id = st.text_input("Operator ID", value="operator_1")
            pattern = st.text_input("Pattern observed (optional)")
            root_cause = st.text_input("Root cause (optional)")
            confidence = st.slider("Confidence", 0.0, 1.0, 0.85)
            submitted = st.form_submit_button("Submit label")

        if submitted:
            payload = {
                "customer_id": tenant,
                "incident_id": selected_id,
                "is_true_anomaly": is_true == "True anomaly",
                "operator_id": operator_id,
                "pattern_type_observed": pattern or None,
                "actual_root_cause": root_cause or None,
                "operator_confidence": confidence,
            }
            try:
                with st.spinner("Submitting label…"):
                    result = client.mark_anomaly(payload)
                st.success(result.get("status", "Submitted"))
                if result.get("retraining"):
                    render_learning_progress(retraining=result["retraining"])
                st.rerun()
            except Exception as exc:
                st.error(f"Submit failed: {exc}")

if pending and open_incidents:
    st.caption(
        f"{len(pending)} additional detected anomaly/anomalies not yet in the queue "
        "(already-reviewed timestamps are skipped)."
    )
