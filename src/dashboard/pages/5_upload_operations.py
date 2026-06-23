import time
import os

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

from src.config import ANOMALY_THRESHOLD
from src.dashboard.components.charts import get_available_greenhouses

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Upload & Operations", page_icon="📤", layout="wide")

st.title("Data Upload & Model Operations")
st.markdown("Upload raw sensor data for training or find anomalies instantly using the active model.")

tab1, tab2 = st.tabs(["Upload & Train Model", "Upload & Find Anomalies"])

with tab1:
    st.header("Upload Data & Train Model")
    st.write("Ingest historical data into the platform and trigger a new model training cycle.")

    col1, col2 = st.columns(2)
    with col1:
        tenant_id_train = st.text_input("Tenant ID", "tenant_1", key="tenant_train")
        _gh_opts_train = get_available_greenhouses(tenant_id_train)
        _gh_default_train = st.session_state.get("selected_greenhouse", _gh_opts_train[0])
        _gh_idx_train = _gh_opts_train.index(_gh_default_train) if _gh_default_train in _gh_opts_train else 0
        greenhouse_id_train = st.selectbox(
            "Greenhouse ID", _gh_opts_train, index=_gh_idx_train, key="greenhouse_train"
        )

    uploaded_file_train = st.file_uploader("Choose a CSV file for training", type="csv", key="file_train")

    # ── Training status poller (runs on every rerun while a job is active) ───
    job = st.session_state.get("training_job")
    if job and job.get("status") == "running":
        _status_placeholder = st.empty()
        try:
            res_status = requests.get(
                f"{API_URL}/api/models/{job['customer_id']}/training-status", timeout=5
            )
            current = res_status.json() if res_status.ok else {"status": "running"}
        except Exception:
            current = {"status": "running"}

        if current["status"] == "running":
            _status_placeholder.info("Training in progress — checking again in 5 seconds…")
            time.sleep(5)
            st.rerun()
        elif current["status"] == "completed":
            model_id = current.get("model_id")
            if model_id:
                _status_placeholder.success(f"Training complete. New model registered: `{model_id}`")
            else:
                _status_placeholder.warning(
                    "Training finished but no model was registered "
                    "(new model did not outperform the baseline)."
                )
            st.session_state["training_job"] = None
        elif current["status"] == "failed":
            _status_placeholder.error(f"Training failed: {current.get('error', 'unknown error')}")
            st.session_state["training_job"] = None

    if st.button("Upload & Train", type="primary"):
        if not tenant_id_train or not greenhouse_id_train:
            st.warning("Tenant ID and Greenhouse ID cannot be empty.")
        elif uploaded_file_train is not None:
            with st.spinner("Uploading data…"):
                files = {"file": (uploaded_file_train.name, uploaded_file_train.getvalue(), "text/csv")}
                data = {"tenant_id": tenant_id_train, "greenhouse_id": greenhouse_id_train}
                try:
                    res_upload = requests.post(f"{API_URL}/api/upload", files=files, data=data)
                    res_upload.raise_for_status()
                    st.success(f"Uploaded {res_upload.json().get('uploaded', 0)} rows successfully.")

                    res_train = requests.post(f"{API_URL}/api/models/{tenant_id_train}/train")
                    res_train.raise_for_status()
                    st.session_state["training_job"] = {
                        "status": "running",
                        "customer_id": tenant_id_train,
                    }
                    st.rerun()
                except requests.exceptions.RequestException as e:
                    st.error(f"Operation failed: {e}")
                    if hasattr(e, "response") and e.response is not None:
                        try:
                            st.json(e.response.json())
                        except Exception:
                            st.write(e.response.text)
        else:
            st.warning("Please upload a CSV file first.")

    # ── Inject test anomalies ─────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Inject test anomalies")
    st.caption(
        "Generates synthetic readings with pre-scored anomaly spikes for the selected "
        "tenant/greenhouse. Injected rows are tagged so they can be removed independently "
        "of real sensor data."
    )

    _ANOMALY_TYPE_OPTIONS = [
        "random", "temperature_spike", "temperature_drop",
        "humidity_spike", "humidity_drop", "co2_surge",
        "light_failure", "multi_sensor", "gradual_drift",
    ]
    _TYPE_LABELS = {
        "random": "Random (mixed)",
        "temperature_spike": "Temperature spike",
        "temperature_drop": "Temperature drop",
        "humidity_spike": "Humidity spike",
        "humidity_drop": "Humidity drop",
        "co2_surge": "CO₂ surge",
        "light_failure": "Light failure",
        "multi_sensor": "Multi-sensor failure",
        "gradual_drift": "Gradual drift",
    }

    _atype_col, _count_col, _normal_col = st.columns(3)
    _anomaly_type = _atype_col.selectbox(
        "Anomaly type",
        options=_ANOMALY_TYPE_OPTIONS,
        format_func=lambda t: _TYPE_LABELS.get(t, t),
        key="ia_anomaly_type",
    )
    _n_anomalies = _count_col.number_input(
        "Anomaly count", min_value=1, max_value=50, value=8, step=1, key="ia_n_anomalies"
    )
    _n_normal = _normal_col.number_input(
        "Background readings", min_value=0, max_value=500, value=96, step=10, key="ia_n_normal"
    )

    _date_col1, _date_col2 = st.columns(2)
    _ia_date_from = _date_col1.date_input("From (optional)", value=None, key="ia_date_from")
    _ia_date_to = _date_col2.date_input("To (optional)", value=None, key="ia_date_to")
    st.caption("Leave dates empty to scatter anomalies randomly over the last 48 hours.")

    if st.button("Inject anomalies", type="primary", key="ia_inject_btn"):
        from datetime import datetime as _dt, timezone as _tz
        _window_start = None
        _window_end = None
        if _ia_date_from and _ia_date_to:
            _window_start = _dt(
                _ia_date_from.year, _ia_date_from.month, _ia_date_from.day, 0, 0, tzinfo=_tz.utc
            ).isoformat()
            _window_end = _dt(
                _ia_date_to.year, _ia_date_to.month, _ia_date_to.day, 23, 59, tzinfo=_tz.utc
            ).isoformat()
        try:
            _post_data = {
                "tenant_id": tenant_id_train,
                "greenhouse_id": greenhouse_id_train,
                "n_normal": int(_n_normal),
                "n_anomalies": int(_n_anomalies),
                "anomaly_type": _anomaly_type,
            }
            if _window_start:
                _post_data["window_start"] = _window_start
                _post_data["window_end"] = _window_end
            res_ia = requests.post(
                f"{API_URL}/api/upload/inject-test-anomalies",
                data=_post_data,
            )
            res_ia.raise_for_status()
            d = res_ia.json()
            from src.dashboard.components.charts import clear_sensor_cache  # noqa: PLC0415
            clear_sensor_cache()
            _label = _TYPE_LABELS.get(_anomaly_type, _anomaly_type)
            _ws = d.get("window_start", "")[:10]
            _we = d.get("window_end", "")[:10]
            st.success(
                f"Injected {d['injected']} readings "
                f"({d['anomalies']} × {_label}) "
                f"from {_ws} to {_we} "
                f"for `{tenant_id_train}` / `{greenhouse_id_train}`."
            )
            cols_used = d.get("columns_used", [])
            if cols_used:
                st.caption(f"Sensor columns used: {', '.join(cols_used)}")
            st.info("Open **Anomaly Detection** and **Sensor Data** to see the injected spikes.")
        except Exception as e:
            st.error(f"Injection failed: {e}")

    # ── Remove synthetic anomalies ────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Remove synthetic anomalies")
    st.caption("Removes only injected test rows — real sensor data is never affected.")

    _rm_type_col, _rm_btn_col, _ = st.columns([2, 1, 3], vertical_alignment="bottom")
    _rm_type = _rm_type_col.selectbox(
        "Type to remove",
        options=[""] + _ANOMALY_TYPE_OPTIONS,
        format_func=lambda t: "All synthetic anomalies" if t == "" else _TYPE_LABELS.get(t, t),
        key="ia_rm_type",
    )
    if _rm_btn_col.button("Remove", type="secondary", key="ia_rm_btn", use_container_width=True):
        try:
            _params = {"tenant_id": tenant_id_train, "greenhouse_id": greenhouse_id_train}
            if _rm_type:
                _params["anomaly_type"] = _rm_type
            res_rm = requests.delete(
                f"{API_URL}/api/upload/inject-test-anomalies", params=_params
            )
            res_rm.raise_for_status()
            d_rm = res_rm.json()
            from src.dashboard.components.charts import clear_sensor_cache  # noqa: PLC0415
            clear_sensor_cache()
            st.success(
                f"Removed {d_rm['deleted_sensor']} sensor readings "
                f"and {d_rm['deleted_raw']} raw readings "
                f"(type: {d_rm['anomaly_type']})."
            )
        except Exception as e:
            st.error(f"Remove failed: {e}")

    # ── Clear stored data ─────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Clear stored data")
    st.warning("This permanently deletes all sensor readings for the selected tenant and greenhouse.")
    if st.button("Clear all data", type="secondary"):
        try:
            res = requests.delete(
                f"{API_URL}/api/upload/data",
                params={"tenant_id": tenant_id_train, "greenhouse_id": greenhouse_id_train},
            )
            res.raise_for_status()
            from src.dashboard.components.charts import clear_sensor_cache  # noqa: PLC0415
            clear_sensor_cache()
            get_available_greenhouses.clear()
            st.success(
                f"All data cleared for tenant `{tenant_id_train}` / greenhouse `{greenhouse_id_train}`."
            )
        except Exception as e:
            st.error(f"Clear failed: {e}")

with tab2:
    st.header("Upload & Find Anomalies")
    st.write("Instantly evaluate a dataset for anomalies without persisting it to the training database.")

    col3, col4 = st.columns(2)
    with col3:
        tenant_id_inf = st.text_input("Tenant ID", "tenant_1", key="tenant_inf")
        _gh_opts_inf = get_available_greenhouses(tenant_id_inf)
        _gh_default_inf = st.session_state.get("selected_greenhouse", _gh_opts_inf[0])
        _gh_idx_inf = _gh_opts_inf.index(_gh_default_inf) if _gh_default_inf in _gh_opts_inf else 0
        greenhouse_id_inf = st.selectbox(
            "Greenhouse ID", _gh_opts_inf, index=_gh_idx_inf, key="greenhouse_inf"
        )

    uploaded_file_inf = st.file_uploader("Choose a CSV file for anomaly detection", type="csv", key="file_inf")

    if st.button("Find Anomalies", type="primary"):
        if not tenant_id_inf or not greenhouse_id_inf:
            st.warning("Tenant ID and Greenhouse ID cannot be empty.")
        elif uploaded_file_inf is not None:
            with st.spinner("Running inference pipeline..."):
                files = {"file": (uploaded_file_inf.name, uploaded_file_inf.getvalue(), "text/csv")}
                data = {"tenant_id": tenant_id_inf, "greenhouse_id": greenhouse_id_inf}

                try:
                    res_inf = requests.post(f"{API_URL}/api/inference/upload", files=files, data=data)
                    res_inf.raise_for_status()
                    results = res_inf.json()

                    st.success(f"Processed {results.get('sequences_processed', 0)} sequences. Detected {results.get('anomalies_detected', 0)} anomalies.")

                    # Visualization
                    scores = results.get("anomaly_scores", [])
                    if scores:
                        df_scores = pd.DataFrame({
                            "Sequence Index": range(len(scores)),
                            "Anomaly Score": scores
                        })

                        fig = px.line(df_scores, x="Sequence Index", y="Anomaly Score", title="Anomaly Scores over Time")
                        fig.add_hline(y=ANOMALY_THRESHOLD, line_dash="dash", line_color="red", annotation_text="Anomaly Threshold")

                        st.plotly_chart(fig, use_container_width=True)

                    high_anomalies = results.get("high_anomalies", [])
                    if high_anomalies:
                        st.subheader("High Anomalies Detected")
                        df_high = pd.DataFrame(high_anomalies)
                        st.dataframe(df_high)
                    else:
                        st.info("No high anomalies detected in this dataset.")

                except requests.exceptions.RequestException as e:
                    st.error(f"Inference failed: {e}")
                    if hasattr(e, 'response') and e.response is not None:
                        try:
                            st.json(e.response.json())
                        except Exception:
                            st.write(e.response.text)
        else:
            st.warning("Please upload a CSV file first.")
