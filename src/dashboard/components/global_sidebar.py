"""Global sidebar settings shared across every dashboard page."""

from __future__ import annotations

import streamlit as st

from src.dashboard.components.charts import get_available_greenhouses

_TENANT_OPTIONS = ["tenant_1", "tenant_2", "tenant_3"]
_PENDING_TENANT_KEY = "_pending_selected_tenant"
_PENDING_GREENHOUSE_KEY = "_pending_selected_greenhouse"


def queue_sidebar_selection(
    *,
    tenant: str | None = None,
    greenhouse: str | None = None,
) -> None:
    """Stage sidebar values to apply on the next rerun before widgets mount."""
    if tenant is not None:
        st.session_state[_PENDING_TENANT_KEY] = tenant
    if greenhouse is not None:
        st.session_state[_PENDING_GREENHOUSE_KEY] = greenhouse


def _apply_pending_sidebar_selection() -> None:
    pending_tenant = st.session_state.pop(_PENDING_TENANT_KEY, None)
    pending_greenhouse = st.session_state.pop(_PENDING_GREENHOUSE_KEY, None)
    if pending_tenant is not None:
        st.session_state["selected_tenant"] = pending_tenant
    if pending_greenhouse is not None:
        st.session_state["selected_greenhouse"] = pending_greenhouse


def render_global_sidebar() -> None:
    """Render tenant and greenhouse selectors on every page."""
    _apply_pending_sidebar_selection()
    if "selected_tenant" not in st.session_state:
        st.session_state["selected_tenant"] = _TENANT_OPTIONS[0]

    st.sidebar.header("Global Settings")
    st.sidebar.selectbox(
        "Select Tenant",
        _TENANT_OPTIONS,
        key="selected_tenant",
    )

    tenant = st.session_state["selected_tenant"]
    _gh_options = get_available_greenhouses(tenant)
    if "selected_greenhouse" not in st.session_state:
        st.session_state["selected_greenhouse"] = _gh_options[0]
    if st.session_state["selected_greenhouse"] not in _gh_options:
        st.session_state["selected_greenhouse"] = _gh_options[0]

    st.sidebar.selectbox(
        "Select Greenhouse",
        _gh_options,
        key="selected_greenhouse",
    )

    st.sidebar.markdown("---")
    api_key = st.sidebar.text_input(
        "API Key (optional)",
        type="password",
        help="Required when API auth is enabled (X-API-Key header)",
        key="global_api_key",
    )
    st.session_state["api_key"] = api_key or None
