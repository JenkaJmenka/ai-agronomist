import os
from datetime import timedelta

import streamlit as st

from src.dashboard.components.charts import get_available_greenhouses
from src.dashboard.components.global_sidebar import render_global_sidebar
from src.dashboard.services.api_client import DEFAULT_API_URL
from src.dashboard.services.http_utils import api_delete, api_get, api_post
from src.dashboard.services.upload_validator import validate_csv_upload

API_URL = os.environ.get("API_URL", DEFAULT_API_URL)

_TRAINING_POLL_SECONDS = 5
_MAX_POLL_ERRORS = 5
_MAX_POLL_ATTEMPTS = 120  # 120 × 5 s ≈ 10 min ceiling


@st.fragment(run_every=timedelta(seconds=_TRAINING_POLL_SECONDS))
def _render_training_status_poller() -> None:
    """Non-blocking training job poll (replaces sleep + full-page rerun)."""
    job = st.session_state.get("training_job")
    if not job or job.get("status") != "running":
        return

    _poll_errors = job.get("poll_errors", 0)
    _poll_attempts = job.get("poll_attempts", 0)

    if _poll_attempts >= _MAX_POLL_ATTEMPTS:
        st.warning(
            "Training job status is unknown — it has been polling for over 10 minutes. "
            "Check the API server logs to see if training completed."
        )
        st.session_state["training_job"] = None
        return

    _current: dict | None = None
    try:
        res_status = api_get(
            f"{API_URL}/api/models/{job['customer_id']}/training-status",
            timeout=5,
        )
        _current = res_status.json() if res_status.ok else {"status": "running"}
        _poll_errors = 0
    except Exception as _poll_exc:
        _poll_errors += 1
        if _poll_errors >= _MAX_POLL_ERRORS:
            st.warning(
                f"Training status API is not responding ({_poll_errors} consecutive failures). "
                f"Last error: {_poll_exc}"
            )
            st.session_state["training_job"] = None
            return
        _current = {"status": "running"}

    job["poll_errors"] = _poll_errors
    job["poll_attempts"] = _poll_attempts + 1
    st.session_state["training_job"] = job

    if _current["status"] == "running":
        st.info("Training in progress…")
    elif _current["status"] == "completed":
        model_id = _current.get("model_id")
        if model_id:
            st.success(f"Training complete. New model registered: `{model_id}`")
        else:
            st.warning(
                "Training finished but no model was registered "
                "(new model did not outperform the baseline)."
            )
        st.session_state["training_job"] = None
    elif _current["status"] == "failed":
        st.error(f"Training failed: {_current.get('error', 'unknown error')}")
        st.session_state["training_job"] = None


st.set_page_config(page_title="Upload & Operations", page_icon="📤", layout="wide")
render_global_sidebar()

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

    _render_training_status_poller()

    if st.button("Upload & Train", type="primary"):
        if not tenant_id_train or not greenhouse_id_train:
            st.warning("Tenant ID and Greenhouse ID cannot be empty.")
        elif uploaded_file_train is not None:
            _file_bytes_train = uploaded_file_train.getvalue()
            _validation = validate_csv_upload(_file_bytes_train, uploaded_file_train.name)
            if not _validation.is_valid:
                for _err in _validation.errors:
                    st.error(_err)
            else:
                for _warn in _validation.warnings:
                    st.warning(_warn)
                with st.spinner("Uploading data…"):
                    files = {"file": (uploaded_file_train.name, _file_bytes_train, "text/csv")}
                    data = {"tenant_id": tenant_id_train, "greenhouse_id": greenhouse_id_train}
                    try:
                        res_upload = api_post(
                            f"{API_URL}/api/upload",
                            files=files,
                            data=data,
                        )
                        st.success(f"Uploaded {res_upload.json().get('uploaded', 0)} rows successfully.")

                        res_train = api_post(f"{API_URL}/api/models/{tenant_id_train}/train")
                        st.session_state["training_job"] = {
                            "status": "running",
                            "customer_id": tenant_id_train,
                        }
                        st.rerun()
                    except Exception as e:
                        exc_str = str(e)
                        if "connection" in exc_str.lower() or "refused" in exc_str.lower():
                            st.error(
                                "Could not reach the API server. "
                                "Check that the API service is running and that API_URL is set correctly."
                            )
                        else:
                            st.error(f"Upload or training failed: {exc_str[:300]}")
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
            res_ia = api_post(
                f"{API_URL}/api/upload/inject-test-anomalies",
                data=_post_data,
            )
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
            res_rm = api_delete(
                f"{API_URL}/api/upload/inject-test-anomalies", params=_params
            )
            d_rm = res_rm.json()
            from src.dashboard.components.charts import clear_sensor_cache  # noqa: PLC0415
            from src.dashboard.components.inference_results import (  # noqa: PLC0415
                clear_inference_results_for,
            )

            clear_sensor_cache()
            clear_inference_results_for(tenant_id_train, greenhouse_id_train)
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

    _confirm_key = f"_confirm_clear_{tenant_id_train}_{greenhouse_id_train}"
    if not st.session_state.get(_confirm_key):
        if st.button("Clear all data", type="secondary", key="clear_data_btn_1"):
            st.session_state[_confirm_key] = True
            st.rerun()
    else:
        st.error(
            f"Are you sure? This will permanently delete ALL data for "
            f"`{tenant_id_train}` / `{greenhouse_id_train}`. This cannot be undone."
        )
        _col_yes, _col_no, _ = st.columns([1, 1, 4])
        if _col_yes.button("Yes, delete everything", type="primary", key="clear_data_confirm"):
            try:
                api_delete(
                    f"{API_URL}/api/upload/data",
                    params={"tenant_id": tenant_id_train, "greenhouse_id": greenhouse_id_train},
                )
                from src.dashboard.components.charts import clear_sensor_cache  # noqa: PLC0415
                from src.dashboard.components.inference_results import (  # noqa: PLC0415
                    clear_inference_results_for,
                )
                clear_sensor_cache()
                clear_inference_results_for(tenant_id_train, greenhouse_id_train)
                get_available_greenhouses.clear()
                st.success(
                    f"All data cleared for tenant `{tenant_id_train}` / greenhouse `{greenhouse_id_train}`."
                )
            except Exception as e:
                st.error(f"Clear failed: {e}")
            finally:
                st.session_state.pop(_confirm_key, None)
        if _col_no.button("Cancel", type="secondary", key="clear_data_cancel"):
            st.session_state.pop(_confirm_key, None)
            st.rerun()

with tab2:
    st.header("Upload & Find Anomalies")
    st.info(
        "CSV anomaly detection has moved to the **Anomaly Detection** page. "
        "Upload there to analyze data, view combined charts, and browse all anomaly tables."
    )
    if st.button("Go to Anomaly Detection", type="primary", key="goto_anomalies"):
        st.switch_page("pages/2_anomalies.py")
