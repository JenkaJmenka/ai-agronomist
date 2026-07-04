import os
from typing import Callable, Optional

from src.dashboard.constants import DEFAULT_ANOMALY_THRESHOLD
from src.dashboard.services.api_client import DEFAULT_API_URL

import pandas as pd
import streamlit as st

from src.dashboard.components.sensor_columns import get_all_sensor_columns
from src.dashboard.components.anomaly_table import render as render_anomaly_table
from src.dashboard.components.charts import (
    apply_datetime_filter,
    clear_sensor_cache,
    get_available_greenhouses,
)
from src.dashboard.services.anomalies_page_data import (
    bounds_from_upload_results,
    enrich_anomalies_table,
    filter_anomalies,
    load_readings_window,
    load_timestamp_bounds,
    load_upload_score_frame,
)
from src.dashboard.components.global_sidebar import (
    queue_sidebar_selection,
    render_global_sidebar,
)
from src.dashboard.components.inference_results import (
    clear_inference_results,
    get_recent_inference_results,
    load_inference_results,
    render_anomaly_score_charts,
    render_cold_run_anomaly_tables,
    render_inference_upload_summary,
    render_upload_status_bar,
    save_inference_results,
)
from src.dashboard.components.timeline import (
    init_monitoring_session_state,
    render_date_range_filters,
)
from src.dashboard.services.api_client import DashboardApiClient
from src.dashboard.services.http_utils import api_post
from src.dashboard.services.inference_cache import compute_file_hash, read_layer1, write_layer1
from src.dashboard.services.upload_validator import validate_csv_upload
from src.dashboard.services.incident_loader import fetch_incidents_in_range
from src.dashboard.constants import CACHE_TTL_SHORT
from src.dashboard.services.review_queue import (
    build_sequence_from_readings,
    register_anomaly_as_incident,
)

API_URL = os.environ.get("API_URL", DEFAULT_API_URL)


@st.cache_data(ttl=CACHE_TTL_SHORT)
def _cached_active_model_version(tenant_id: str) -> str:
    """Short-TTL cache (C5) so renders don't pay an API hop per upload click."""
    return DashboardApiClient().get_active_model_version(tenant_id)


st.set_page_config(page_title="Anomaly Detection", layout="wide")
render_global_sidebar()
st.title("Anomaly Detection")

_FAULT_TYPES = [
    "spike_fault",
    "calibration_drift",
    "stuck_fault",
    "bias_offset",
    "accuracy_decline",
]
_FAULT_LABELS = {
    "spike_fault": "Spike fault",
    "calibration_drift": "Calibration drift",
    "stuck_fault": "Stuck sensor",
    "bias_offset": "Bias offset",
    "accuracy_decline": "Accuracy decline",
    "unknown": "Unknown / unclassified",
}


def _submit_feedback(
    api: DashboardApiClient,
    customer_id: str,
    incident_id: str,
    is_true_anomaly: bool,
    state_key: str,
    feedback_state_key: str,
) -> None:
    try:
        with st.spinner("Submitting feedback…"):
            result = api.mark_anomaly({
                "incident_id": incident_id,
                "is_true_anomaly": is_true_anomaly,
                "customer_id": customer_id,
                "operator_id": "dashboard_user",
            })
        st.session_state[feedback_state_key][state_key] = {
            "is_true_anomaly": is_true_anomaly,
            "result": result,
        }
        if is_true_anomaly:
            st.success("Anomaly confirmed.")
        else:
            st.info("Marked as false positive.")

        if result.get("learning_triggered"):
            st.success(
                "Learning adjustment triggered — model is retraining with your feedback."
            )
        else:
            progress = result.get("progress_to_retrain", "—")
            st.info(f"Feedback saved. Learning progress: {progress} quality samples.")

    except Exception as exc:
        st.warning(
            f"Feedback could not be submitted (is the API server running?): {exc}"
        )


def _render_upload_section(
    tenant: str,
    greenhouse: str,
    saved_inf: dict | None,
) -> None:
    with st.expander("Upload & find anomalies", expanded=False):
        if saved_inf:
            st.markdown("**Last upload results**")
            render_inference_upload_summary(
                saved_inf["results"],
                saved_inf["tenant_id"],
                saved_inf["greenhouse_id"],
            )
            st.divider()

        load_data_into_platform = st.toggle(
            "Load data into platform",
            value=True,
            help=(
                "When enabled, saves all sensor readings to the database and attaches "
                "detected anomaly scores."
            ),
            key="inf_load_data",
        )

        col1, col2 = st.columns(2)
        with col1:
            tenant_id_inf = st.text_input(
                "Tenant ID", value=tenant, key="anomaly_upload_tenant"
            )
        with col2:
            gh_opts = get_available_greenhouses(tenant_id_inf)
            gh_default = greenhouse if greenhouse in gh_opts else gh_opts[0]
            gh_idx = gh_opts.index(gh_default) if gh_default in gh_opts else 0
            greenhouse_id_inf = st.selectbox(
                "Greenhouse ID", gh_opts, index=gh_idx, key="anomaly_upload_greenhouse"
            )

        uploaded_file = st.file_uploader(
            "Choose a CSV file for anomaly detection",
            type="csv",
            key="anomaly_upload_file",
        )

        if st.button("Upload & find anomalies", type="primary", key="anomaly_upload_btn"):
            if not tenant_id_inf or not greenhouse_id_inf:
                st.warning("Tenant ID and Greenhouse ID cannot be empty.")
            elif uploaded_file is None:
                st.warning("Please upload a CSV file first.")
            else:
                file_bytes = uploaded_file.getvalue()
                validation = validate_csv_upload(file_bytes, uploaded_file.name)
                if not validation.is_valid:
                    for _err in validation.errors:
                        st.error(_err)
                    return
                for _warn in validation.warnings:
                    st.warning(_warn)
                file_hash = compute_file_hash(file_bytes)
                _client = DashboardApiClient(api_key=st.session_state.get("api_key"))
                model_version = _cached_active_model_version(tenant_id_inf)
                # Cached inference results say nothing about whether the rows
                # still exist in the DB — when the user asks to load data into
                # the platform, always go to the API so persistence runs.
                cached = (
                    None
                    if load_data_into_platform
                    else read_layer1(file_hash, model_version)
                )
                _stats = st.session_state.setdefault(
                    "_cache_stats", {"l1_hits": 0, "l1_misses": 0}
                )
                if cached is not None:
                    # Layer 1 HIT — skip API call
                    results = cached["results"]
                    _stats["l1_hits"] += 1
                    st.toast("Loaded from cache", icon="⚡")
                    save_inference_results(
                        results,
                        tenant_id_inf,
                        greenhouse_id_inf,
                        filename=uploaded_file.name,
                    )
                    clear_sensor_cache()
                    get_available_greenhouses.clear()
                    queue_sidebar_selection(
                        tenant=tenant_id_inf,
                        greenhouse=greenhouse_id_inf,
                    )
                    st.rerun()
                else:
                    # Layer 1 MISS — call API, then populate cache
                    _stats["l1_misses"] += 1
                    with st.spinner(
                        "Running inference pipeline"
                        + (" and loading data…" if load_data_into_platform else "…")
                    ):
                        files = {
                            "file": (
                                uploaded_file.name,
                                file_bytes,
                                "text/csv",
                            )
                        }
                        data = {
                            "tenant_id": tenant_id_inf,
                            "greenhouse_id": greenhouse_id_inf,
                            "persist_data": str(load_data_into_platform).lower(),
                        }
                        try:
                            res_inf = api_post(
                                f"{API_URL}/api/inference/upload",
                                files=files,
                                data=data,
                                timeout=300,
                            )
                            results = res_inf.json()
                            write_layer1(file_hash, model_version, {"results": results})
                            save_inference_results(
                                results,
                                tenant_id_inf,
                                greenhouse_id_inf,
                                filename=uploaded_file.name,
                            )
                            clear_sensor_cache()
                            get_available_greenhouses.clear()
                            queue_sidebar_selection(
                                tenant=tenant_id_inf,
                                greenhouse=greenhouse_id_inf,
                            )
                            st.rerun()
                        except Exception as exc:
                            exc_str = str(exc)
                            if "connection" in exc_str.lower() or "refused" in exc_str.lower() or "timeout" in exc_str.lower():
                                st.error(
                                    "Could not reach the API server. "
                                    "Check that the API service is running and that API_URL is set correctly."
                                )
                            elif "422" in exc_str or "400" in exc_str:
                                detail = exc_str.split(":", 1)[-1].strip()[:300]
                                st.error(f"The file was rejected by the server: {detail}")
                            elif "500" in exc_str or "503" in exc_str:
                                st.error(
                                    "The inference server encountered an internal error. "
                                    "Check the API server logs for details."
                                )
                            else:
                                st.error(f"Inference failed: {exc}")

        if st.session_state.get("debug_cache"):
            with st.expander("Cache stats", expanded=False):
                st.json(st.session_state.get("_cache_stats", {}))


def _make_feedback_handler(
    *,
    df: pd.DataFrame,
    tenant: str,
    greenhouse: str,
    api: DashboardApiClient,
    feedback_state_key: str,
) -> Callable[[Optional[str], bool, str, str], None]:
    def handle_feedback(
        incident_id: Optional[str],
        is_true_anomaly: bool,
        timestamp: str,
        state_key: str,
    ) -> None:
        effective_id = incident_id
        if effective_id is None:
            with st.spinner("Registering anomaly…"):
                try:
                    sequence = build_sequence_from_readings(df, pd.Timestamp(timestamp))
                    result = register_anomaly_as_incident(api, tenant, greenhouse, sequence)
                    effective_id = result.get("incident_id") or result.get("id")
                except Exception as exc:
                    st.warning(f"Could not register anomaly: {exc}")
                    return
        if effective_id:
            _submit_feedback(
                api, tenant, effective_id, is_true_anomaly, state_key, feedback_state_key
            )
        st.rerun()

    return handle_feedback


def _incidents_for_cold_run(
    results: dict | None,
    tenant: str,
    greenhouse: str,
) -> list:
    if not results:
        return []
    timestamps: list[str] = []
    for row in results.get("high_anomalies") or []:
        ts = row.get("timestamp")
        if ts:
            timestamps.append(str(ts))
    stat_summary = results.get("statistical_summary") or {}
    for row in stat_summary.get("anomalies") or []:
        ts = row.get("timestamp")
        if ts:
            timestamps.append(str(ts))
    if not timestamps:
        return []
    ts_col = pd.to_datetime(pd.Series(timestamps))
    return fetch_incidents_in_range(
        tenant,
        greenhouse,
        ts_col.min().isoformat(),
        (ts_col.max() + pd.Timedelta(days=1)).isoformat(),
    )


def _render_top_anomalies_section(
    *,
    anomalies_df: pd.DataFrame,
    df: pd.DataFrame,
    source_df: pd.DataFrame,
    tenant: str,
    greenhouse: str,
    threshold: float,
    selected_fault_types: list[str],
    handle_feedback: Callable[[Optional[str], bool, str, str], None],
    feedback_state_key: str,
) -> None:
    st.markdown("### Top Anomalies")

    if anomalies_df.empty:
        st.info(
            f"No anomalies detected above the {threshold} threshold in the selected range."
        )
        return

    top_anomalies = anomalies_df.sort_values("anomaly_score", ascending=False).head(20)
    ts_col = pd.to_datetime(top_anomalies["timestamp"])
    start_iso = ts_col.min().isoformat()
    end_iso = (ts_col.max() + pd.Timedelta(days=1)).isoformat()

    try:
        enriched = enrich_anomalies_table(
            top_anomalies,
            tenant,
            greenhouse,
            start_iso,
            end_iso,
        )
    except Exception as exc:
        st.warning(f"Could not enrich anomaly table: {exc}")
        enriched = top_anomalies

    if selected_fault_types:
        def _ft_match(ft: Optional[str]) -> bool:
            return ft in selected_fault_types or (
                ft is None and "unknown" in selected_fault_types
            )

        enriched = enriched[enriched["fault_type"].apply(_ft_match)]

    render_anomaly_table(
        enriched,
        on_feedback=handle_feedback,
        feedback_state_key=feedback_state_key,
        tenant_id=tenant,
        greenhouse_id=greenhouse,
        table_title="",
        source_df=source_df,
        available_columns=get_all_sensor_columns(source_df) if not source_df.empty else None,
    )


if "selected_tenant" not in st.session_state:
    st.warning("Please configure global settings in the sidebar.")
    st.stop()

tenant = st.session_state["selected_tenant"]
greenhouse = st.session_state["selected_greenhouse"]
threshold = st.session_state.get("anomaly_threshold", DEFAULT_ANOMALY_THRESHOLD)
api = DashboardApiClient(api_key=st.session_state.get("api_key"))

saved_inf = load_inference_results(tenant, greenhouse)
if saved_inf is None:
    recent = get_recent_inference_results()
    if (
        recent
        and recent.get("tenant_id") == tenant
        and recent.get("greenhouse_id") == greenhouse
    ):
        saved_inf = recent
inf_results = saved_inf["results"] if saved_inf else None
inf_tenant = saved_inf["tenant_id"] if saved_inf else tenant
inf_greenhouse = saved_inf["greenhouse_id"] if saved_inf else greenhouse

_render_upload_section(tenant, greenhouse, saved_inf)

if render_upload_status_bar(saved_inf):
    clear_inference_results()
    st.rerun()


def _resolve_data_bounds(
    tenant: str,
    greenhouse: str,
    inf_results: dict | None,
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    min_iso, max_iso = load_timestamp_bounds(tenant, greenhouse)
    if min_iso and max_iso:
        return pd.Timestamp(min_iso), pd.Timestamp(max_iso)
    if inf_results:
        return bounds_from_upload_results(inf_results)
    return None


def _load_window_dataframe(
    *,
    tenant: str,
    greenhouse: str,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    inf_results: dict | None,
) -> tuple[pd.DataFrame, bool]:
    start_iso = pd.Timestamp(window_start).isoformat()
    end_iso = pd.Timestamp(window_end).isoformat()
    df_window = load_readings_window(tenant, greenhouse, start_iso, end_iso)
    if not df_window.empty:
        return df_window, False

    if inf_results:
        upload_frame = load_upload_score_frame(
            tenant,
            greenhouse,
            int(inf_results.get("sequences_processed", 0) or 0),
            len(inf_results.get("anomaly_scores") or []),
            inf_results,
        )
        if not upload_frame.empty:
            filtered = apply_datetime_filter(upload_frame, window_start, window_end)
            if not filtered.empty:
                return filtered, True
    return pd.DataFrame(), False


@st.fragment
def _render_anomaly_dashboard(
    *,
    tenant: str,
    greenhouse: str,
    threshold: float,
    api: DashboardApiClient,
    saved_inf: dict | None,
    inf_results: dict | None,
    inf_tenant: str,
    inf_greenhouse: str,
) -> None:
    bounds = _resolve_data_bounds(tenant, greenhouse, inf_results)
    using_upload_fallback = False
    score_threshold = threshold

    has_sensor_data = bounds is not None
    window_start: pd.Timestamp | None = None
    window_end: pd.Timestamp | None = None
    df = pd.DataFrame()
    df_window = pd.DataFrame()
    anomalies_df = pd.DataFrame()
    range_label = "—"
    loaded_at = pd.Timestamp.now().strftime("%H:%M:%S")
    selected_fault_types: list[str] = []

    if has_sensor_data and bounds is not None:
        data_min, data_max = bounds
        init_monitoring_session_state(tenant, greenhouse, data_min, data_max)

        window_start, window_end = render_date_range_filters(
            data_min,
            data_max,
            key_prefix="anomaly",
            collapsible=True,
            fault_type_options=_FAULT_TYPES + ["unknown"],
            fault_type_labels=_FAULT_LABELS,
            fault_type_key="fault_type_filter",
        )
        selected_fault_types = st.session_state.get("fault_type_filter", [])

        try:
            df_window, using_upload_fallback = _load_window_dataframe(
                tenant=tenant,
                greenhouse=greenhouse,
                window_start=window_start,
                window_end=window_end,
                inf_results=inf_results,
            )
        except Exception as exc:
            st.error(f"Could not load sensor readings: {exc}")
            df_window = pd.DataFrame()
            using_upload_fallback = False

        if using_upload_fallback:
            score_threshold = float(inf_results.get("threshold_used", threshold)) if inf_results else threshold
            if inf_results and inf_results.get("data_persisted"):
                st.warning(
                    "Database readings are unavailable — showing filters from your last upload. "
                    "If this persists, refresh the page or re-upload the CSV."
                )
            else:
                st.info(
                    "Showing filters from your last CSV upload. "
                    "Enable **Load data into platform** on upload to persist readings in the database."
                )

        if df_window.empty and has_sensor_data:
            st.info(
                "No sensor readings in the selected date window. "
                "Try expanding the date range or choose a different preset."
            )

        df = df_window
        range_label = (
            f"{window_start.strftime('%d %b %Y %H:%M')} → "
            f"{window_end.strftime('%d %b %Y %H:%M')}"
        )
        anomalies_df = filter_anomalies(df, score_threshold)
    else:
        with st.expander("Filters", expanded=True):
            st.info(
                "No sensor data for this greenhouse — upload a CSV or choose another tenant/greenhouse."
            )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Data points", f"{len(df):,}" if not df.empty else "0")
    col2.metric(f"Anomalies (≥ {score_threshold:.2f})", len(anomalies_df))
    anomaly_rate = len(anomalies_df) / len(df) if len(df) > 0 else 0
    col3.metric("Anomaly rate", f"{anomaly_rate:.2%}")
    if not df.empty:
        span_hours = (
            pd.to_datetime(df["timestamp"]).max() - pd.to_datetime(df["timestamp"]).min()
        ).total_seconds() / 3600
        col4.metric("Window span", f"{span_hours:.1f} h")
    else:
        col4.metric("Window span", "—")

    st.caption(
        f"Range: {range_label} · loaded at {loaded_at} · window-cached 2 min"
    )

    feedback_state_key = "feedback_results"
    if feedback_state_key not in st.session_state:
        st.session_state[feedback_state_key] = {}
    handle_feedback = _make_feedback_handler(
        df=df,
        tenant=tenant,
        greenhouse=greenhouse,
        api=api,
        feedback_state_key=feedback_state_key,
    )

    st.markdown("### Anomaly Score Timeline")
    x_range = (
        (window_start, window_end)
        if window_start is not None and window_end is not None
        else None
    )
    render_anomaly_score_charts(
        df,
        score_threshold,
        saved_inf,
        x_range=x_range,
    )

    _render_top_anomalies_section(
        anomalies_df=anomalies_df,
        df=df,
        source_df=df,
        tenant=tenant,
        greenhouse=greenhouse,
        threshold=score_threshold,
        selected_fault_types=selected_fault_types,
        handle_feedback=handle_feedback,
        feedback_state_key=feedback_state_key,
    )

    render_cold_run_anomaly_tables(
        inf_results,
        inf_tenant,
        inf_greenhouse,
        incidents=_incidents_for_cold_run(inf_results, inf_tenant, inf_greenhouse),
        on_feedback=handle_feedback,
        feedback_state_key=feedback_state_key,
        source_df=df if has_sensor_data and not df.empty else None,
        available_columns=get_all_sensor_columns(df) if has_sensor_data and not df.empty else None,
        window_start=window_start,
        window_end=window_end,
    )


_render_anomaly_dashboard(
    tenant=tenant,
    greenhouse=greenhouse,
    threshold=threshold,
    api=api,
    saved_inf=saved_inf,
    inf_results=inf_results,
    inf_tenant=inf_tenant,
    inf_greenhouse=inf_greenhouse,
)
