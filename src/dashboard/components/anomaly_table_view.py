"""Grouped "Table view" of anomalies for the Anomaly Detection page.

A cohesive, green-accented alternative to the Top Anomalies list: anomalies are
grouped (by date, anomaly type, or affected sensor) into collapsible sections,
each record showing a fault-type pill, a severity chip, and a compact Actions
column built from Google Material icons (confirm · reject · monitor).

Actions operate on the specific record and reuse the page's existing feedback
handler and Monitoring jump, so behaviour matches the Graphical view.
"""

from __future__ import annotations

import html
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import streamlit as st

from src.dashboard.components.anomaly_table import (
    _affected_sensors_label,
    _fault_badge_html,
    _monitor_row_from_payload,
    _row_score,
    enriched_to_export_df,
)
from src.dashboard.components.anomaly_table_view_style import view_css
from src.dashboard.components.monitoring_jump import queue_monitoring_jump
from src.dashboard.components.sensor_columns import get_all_sensor_columns
from src.dashboard.components.table_export import render_export_button
from src.dashboard.constants import MONITORING_PAGE

# Severity chip thresholds are relative to the anomalies visible in the window
# (scores are squashed to [0,1] and a real day tops out ~0.2, so absolute cuts
# would collapse everything into one bucket).
_SEV_HIGH = "rgba(181,67,44,0.20)"
_SEV_HIGH_BORDER = "rgba(181,67,44,0.55)"
_SEV_MID = "rgba(154,106,18,0.20)"
_SEV_MID_BORDER = "rgba(154,106,18,0.55)"
_SEV_LOW = "rgba(120,138,128,0.18)"
_SEV_LOW_BORDER = "rgba(120,138,128,0.45)"

_MAX_RECORDS = 300
_GROUP_OPTIONS = ["Date", "Anomaly type", "Affected sensor"]
_COL_WIDTHS = [1.35, 1.05, 0.95, 2.3, 1.5]
_COL_HEADERS = ["Timestamp", "Type", "Severity", "Affected sensors", "Actions"]


def _severity_chip(score: float, hi_cut: float, mid_cut: float) -> str:
    if score >= hi_cut:
        bg, border, label = _SEV_HIGH, _SEV_HIGH_BORDER, "High"
    elif score >= mid_cut:
        bg, border, label = _SEV_MID, _SEV_MID_BORDER, "Med"
    else:
        bg, border, label = _SEV_LOW, _SEV_LOW_BORDER, "Low"
    return (
        f'<span class="atv-pill" style="background:{bg};border:1px solid {border};">'
        f'{label} · {score:.3f}</span>'
    )


def _enrichment_of(row: Any) -> Dict[str, Any]:
    enrichment = row.get("_enrichment") if hasattr(row, "get") else None
    return enrichment if isinstance(enrichment, dict) else {}


def _fault_of(row: Any, enrichment: Dict[str, Any]) -> str:
    return enrichment.get("fault_type") or (row.get("fault_type") if hasattr(row, "get") else None) or "unknown"


def _group_key(row: Any, enrichment: Dict[str, Any], mode: str) -> str:
    if mode == "Anomaly type":
        from src.dashboard.components.anomaly_table import _FAULT_LABELS

        fault = _fault_of(row, enrichment)
        return _FAULT_LABELS.get(fault, fault.replace("_", " ").title())
    if mode == "Affected sensor":
        label = _affected_sensors_label(row, enrichment)
        if label == "—":
            return "Unattributed"
        parts = [p.strip() for p in label.split(",") if p.strip()]
        return parts[0] if len(parts) == 1 else f"{parts[0]} (+{len(parts) - 1})"
    # Date (default)
    ts = row.get("timestamp") if hasattr(row, "get") else None
    try:
        return pd.to_datetime(ts).strftime("%d %b %Y")
    except Exception:
        return "Unknown date"


def _render_record_row(
    cells: list[Any],
    *,
    row: Any,
    enrichment: Dict[str, Any],
    row_uid: str,
    hi_cut: float,
    mid_cut: float,
    on_feedback: Optional[Callable[..., None]],
    pending: list[Optional[Dict[str, Any]]],
    tenant: str,
    greenhouse: str,
    monitor_columns: Optional[list[str]],
    source_df: Optional[pd.DataFrame],
) -> None:
    ts = row.get("timestamp")
    try:
        ts_iso = pd.to_datetime(ts).isoformat()
        ts_label = pd.to_datetime(ts).strftime("%d %b %H:%M")
    except Exception:
        ts_iso = str(ts)
        ts_label = str(ts)

    cells[0].markdown(f'<span class="atv-ts">{html.escape(ts_label)}</span>', unsafe_allow_html=True)
    cells[1].markdown(_fault_badge_html(_fault_of(row, enrichment)), unsafe_allow_html=True)
    cells[2].markdown(_severity_chip(_row_score(row), hi_cut, mid_cut), unsafe_allow_html=True)
    cells[3].markdown(_affected_sensors_label(row, enrichment))

    action_cell = cells[4]
    action_cell.markdown('<span class="atv-actions" style="display:none"></span>', unsafe_allow_html=True)
    incident_id = enrichment.get("incident_id")
    state_key = incident_id or ts_iso

    specs: list[dict[str, Any]] = []
    if on_feedback is not None:
        specs.append({
            "icon": ":material/check:", "help": "Confirm anomaly",
            "action": lambda: pending.__setitem__(0, {
                "incident_id": incident_id, "is_true_anomaly": True,
                "timestamp": ts_iso, "state_key": state_key,
            }),
        })
        specs.append({
            "icon": ":material/close:", "help": "Reject (false positive)",
            "action": lambda: pending.__setitem__(0, {
                "incident_id": incident_id, "is_true_anomaly": False,
                "timestamp": ts_iso, "state_key": state_key,
            }),
        })

    def _monitor() -> None:
        try:
            monitor_row = _monitor_row_from_payload(row, ts_iso, enrichment)
            queue_monitoring_jump(
                monitor_row, tenant, greenhouse,
                available_columns=monitor_columns, source_df=source_df,
            )
            st.switch_page(MONITORING_PAGE)
        except ValueError as exc:
            st.error(str(exc))

    specs.append({"icon": ":material/monitoring:", "help": "Open Monitoring for this record", "action": _monitor})

    btn_cols = action_cell.columns(len(specs), gap="small")
    for i, (col, spec) in enumerate(zip(btn_cols, specs)):
        if col.button("", icon=spec["icon"], key=f"atv_{row_uid}_{i}", help=spec["help"], type="secondary"):
            spec["action"]()


def render(
    enriched_df: pd.DataFrame,
    *,
    on_feedback: Optional[Callable[..., None]] = None,
    feedback_state_key: str = "feedback_results",
    tenant_id: str = "",
    greenhouse_id: str = "",
    available_columns: Optional[list[str]] = None,
    source_df: Optional[pd.DataFrame] = None,
    max_records: int = _MAX_RECORDS,
) -> None:
    """Render grouped, collapsible anomaly records with Material-icon actions."""
    if enriched_df is None or enriched_df.empty:
        st.info("No anomalies in the selected filters. Adjust the date range or fault-type filter above.")
        return

    if feedback_state_key not in st.session_state:
        st.session_state[feedback_state_key] = {}

    st.markdown(view_css(), unsafe_allow_html=True)

    tenant = tenant_id or st.session_state.get("selected_tenant", "")
    greenhouse = greenhouse_id or st.session_state.get("selected_greenhouse", "")
    monitor_columns = available_columns
    if monitor_columns is None and source_df is not None and not source_df.empty:
        monitor_columns = get_all_sensor_columns(source_df)

    # Bound the render to the most severe records so a wide window stays responsive.
    ranked = enriched_df.copy()
    ranked["_score"] = ranked.apply(_row_score, axis=1)
    total = len(ranked)
    ranked = ranked.sort_values("_score", ascending=False).head(max_records)

    scores = ranked["_score"]
    hi_cut = float(scores.quantile(0.75)) if len(scores) > 3 else float(scores.max() or 0)
    mid_cut = float(scores.quantile(0.40)) if len(scores) > 3 else 0.0

    controls = st.columns([2, 3])
    with controls[0]:
        group_mode = st.segmented_control(
            "Group by", _GROUP_OPTIONS, default="Date", key="anomaly_table_group_by",
        ) or "Date"
    with controls[1]:
        st.caption(
            f"{min(total, max_records)} of {total} anomal{'y' if total == 1 else 'ies'} shown"
            + (f" · top {max_records} by score" if total > max_records else "")
        )

    # Build groups preserving a stable, meaningful order.
    groups: Dict[str, List[Any]] = {}
    for _, row in ranked.iterrows():
        key = _group_key(row, _enrichment_of(row), group_mode)
        groups.setdefault(key, []).append(row)

    if group_mode == "Date":
        ordered_keys = sorted(groups, key=lambda k: pd.to_datetime(groups[k][0].get("timestamp")), reverse=True)
    else:
        ordered_keys = sorted(groups, key=lambda k: len(groups[k]), reverse=True)

    pending: list[Optional[Dict[str, Any]]] = [None]

    for gi, key in enumerate(ordered_keys):
        rows = groups[key]
        peak = max(_row_score(r) for r in rows)
        header = f"{key}  ·  {len(rows)} record{'s' if len(rows) != 1 else ''}  ·  peak {peak:.3f}"
        with st.expander(header, expanded=(gi == 0)):
            st.markdown('<span class="atv-group" style="display:none"></span>', unsafe_allow_html=True)
            head = st.columns(_COL_WIDTHS)
            for idx, label in enumerate(_COL_HEADERS):
                head[idx].markdown(f'<span class="atv-hdr">{html.escape(label)}</span>', unsafe_allow_html=True)
            for ri, row in enumerate(rows):
                cells = st.columns(_COL_WIDTHS)
                _render_record_row(
                    cells,
                    row=row,
                    enrichment=_enrichment_of(row),
                    row_uid=f"{gi}_{ri}",
                    hi_cut=hi_cut,
                    mid_cut=mid_cut,
                    on_feedback=on_feedback,
                    pending=pending,
                    tenant=tenant,
                    greenhouse=greenhouse,
                    monitor_columns=monitor_columns,
                    source_df=source_df,
                )

    export_df = enriched_to_export_df(ranked.drop(columns=["_score"], errors="ignore"))
    if not export_df.empty:
        render_export_button(
            export_df,
            file_name="anomalies_table_view.csv",
            label="Export CSV",
            key="export_anomaly_table_view",
        )

    action = pending[0]
    if action is not None and on_feedback is not None:
        on_feedback(action["incident_id"], action["is_true_anomaly"], action["timestamp"], action["state_key"])
