"""Model registry panel for the dashboard."""

from __future__ import annotations

from typing import Any

import streamlit as st

from src.dashboard.services.api_client import DashboardApiClient


def render_model_registry(tenant_id: str, *, api_url: str | None = None) -> None:
    """List tenant models with activate / delete actions."""
    client = DashboardApiClient(base_url=api_url)

    st.subheader("Model registry")
    st.caption(
        "Cold-run uploads auto-train when no model exists. "
        "Use **Activate** to switch deployments, or **Delete** to remove versions."
    )

    try:
        versions: list[dict[str, Any]] = client.list_model_versions(tenant_id)
    except Exception as exc:
        st.warning(f"Could not load models: {exc}")
        return

    if not versions:
        st.info("No trained models for this tenant — the next cold-run upload will create one.")
        return

    active = next((v for v in versions if v.get("is_active")), None)
    if active:
        st.success(f"Active model: `{active['model_id']}` ({active.get('model_type', 'unknown')})")
    else:
        st.warning("No active deployment — cold-run will use the global baseline.")

    for version in versions:
        model_id = version.get("model_id", "?")
        is_active = version.get("is_active", False)
        metrics = version.get("metrics") or {}
        created = (version.get("created_at") or "")[:19]
        model_type = version.get("model_type", "unknown")
        badge = " **(active)**" if is_active else ""

        col_info, col_activate, col_delete = st.columns([4, 1, 1])
        with col_info:
            st.markdown(f"`{model_id}` · {model_type}{badge}")
            st.caption(f"Created {created} · metrics: {metrics or '—'}")
        with col_activate:
            if not is_active:
                if st.button("Activate", key=f"activate_{model_id}"):
                    try:
                        client.activate_model(tenant_id, model_id)
                        st.success(f"Activated `{model_id}`")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Activate failed: {exc}")
        with col_delete:
            if st.button("Delete", key=f"delete_{model_id}"):
                st.session_state[f"_confirm_delete_{model_id}"] = True
        if st.session_state.get(f"_confirm_delete_{model_id}"):
            st.warning(f"Delete `{model_id}`?")
            c_yes, c_no, _ = st.columns([1, 1, 4])
            if c_yes.button("Confirm delete", key=f"confirm_del_{model_id}"):
                try:
                    force = is_active and len(versions) == 1
                    client.delete_model(tenant_id, model_id, force=force)
                    st.session_state.pop(f"_confirm_delete_{model_id}", None)
                    st.success(f"Deleted `{model_id}`")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")
            if c_no.button("Cancel", key=f"cancel_del_{model_id}"):
                st.session_state.pop(f"_confirm_delete_{model_id}", None)
                st.rerun()

    st.markdown("---")
    if st.button("Delete all models", type="secondary", key="delete_all_models"):
        st.session_state["_confirm_delete_all_models"] = True
    if st.session_state.get("_confirm_delete_all_models"):
        st.error("This removes **all** trained models for the tenant.")
        c_yes, c_no, _ = st.columns([1, 1, 4])
        if c_yes.button("Yes, delete all", key="confirm_delete_all_models"):
            try:
                result = client.clear_models(tenant_id)
                st.session_state.pop("_confirm_delete_all_models", None)
                st.success(f"Deleted {result.get('deleted_versions', 0)} model(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Delete all failed: {exc}")
        if c_no.button("Cancel delete all", key="cancel_delete_all_models"):
            st.session_state.pop("_confirm_delete_all_models", None)
            st.rerun()
