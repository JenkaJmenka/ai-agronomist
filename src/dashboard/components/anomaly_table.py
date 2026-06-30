"""
Styled anomaly table with per-sensor highlighting, hover tooltips, and confirm/reject buttons.

Rows are rendered as Streamlit columns so native st.button widgets can appear in the
Actions column. Sensor cells retain coloured backgrounds via inline HTML spans.
The Monitor action queues a jump to the Monitoring page (date + sensor pre-selection).
The ``build_html_table`` helper is kept for unit-test coverage.
"""

from __future__ import annotations

import html
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import streamlit as st

from src.dashboard.components import sticky_table as _sticky_table
from src.dashboard.components.monitoring_jump import queue_monitoring_jump
from src.dashboard.services.diagnosis_enricher import format_sensor_label

SENSOR_COLUMNS = ["temperature", "humidity", "co2", "light"]

# Semi-transparent fills so cells blend with Streamlit dark/light themes.
AMBER = "background-color: rgba(252, 211, 77, 0.28);"
ORANGE = "background-color: rgba(251, 146, 60, 0.32);"
LIGHT_RED = "background-color: rgba(239, 68, 68, 0.32);"

CELL_BASE = (
    "border: 1px solid rgba(128, 128, 128, 0.35); "
    "padding: 6px 8px; text-align: right; color: inherit;"
)
HEADER_STYLE = (
    "border: 1px solid rgba(128, 128, 128, 0.45); padding: 6px 8px; "
    "background-color: #31333d; color: #fafafa; font-weight: 600; text-align: left;"
)

# Injected once per render into the Streamlit page.
def _table_css(table_key: str) -> str:
    safe_key = html.escape(table_key.replace("_", "-"))
    return f"""
<style>
/* ── Sensor-cell tooltip ─────────────────────────────────────────── */
.ag-cell {{ position: relative; cursor: default; }}
.ag-tip {{
  visibility: hidden;
  opacity: 0;
  transition: opacity 0.12s ease 0.08s, visibility 0s linear 0.20s;
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 9999;
  background: #1e2130;
  color: #e8eaf0;
  border: 1px solid rgba(128, 128, 128, 0.55);
  border-radius: 8px;
  padding: 10px 14px;
  min-width: 240px;
  max-width: 380px;
  font-size: 13px;
  line-height: 1.5;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  white-space: normal;
  pointer-events: none;
  text-align: left;
}}
.ag-cell:hover .ag-tip {{
  visibility: visible;
  opacity: 1;
  transition: opacity 0.12s ease 0.08s, visibility 0s linear 0s;
}}
.ag-tip-hint {{ margin-bottom: 6px; }}
.ag-tip-meta {{ color: #94a3b8; font-size: 12px; }}

/* ── Row action buttons (accept · reject · monitor) ─────────────── */
div[data-testid="column"]:has(.ag-actions-{safe_key}) [data-testid="stHorizontalBlock"] [data-testid="column"]:nth-child(1) button {{
  color: rgb(34, 197, 94) !important;
  border-color: rgba(34, 197, 94, 0.55) !important;
  background-color: rgba(34, 197, 94, 0.10) !important;
  min-width: 2rem !important;
  padding: 0.1rem 0.35rem !important;
}}
div[data-testid="column"]:has(.ag-actions-{safe_key}) [data-testid="stHorizontalBlock"] [data-testid="column"]:nth-child(2) button {{
  color: rgb(239, 68, 68) !important;
  border-color: rgba(239, 68, 68, 0.55) !important;
  background-color: rgba(239, 68, 68, 0.10) !important;
  min-width: 2rem !important;
  padding: 0.1rem 0.35rem !important;
}}
div[data-testid="column"]:has(.ag-actions-{safe_key}) [data-testid="stHorizontalBlock"] [data-testid="column"]:nth-child(3) button,
div[data-testid="column"]:has(.ag-actions-{safe_key}) [data-testid="stHorizontalBlock"] [data-testid="column"]:only-child button {{
  color: rgb(96, 165, 250) !important;
  border-color: rgba(96, 165, 250, 0.55) !important;
  background-color: rgba(96, 165, 250, 0.10) !important;
  min-width: 2rem !important;
  padding: 0.1rem 0.35rem !important;
}}
</style>
"""

_ACTIONS_COL_INDEX = 8

# Column layout shared by all anomaly tables.
_COL_WIDTHS = [1.4, 1.0, 2.5, 1.2, 0.72, 0.72, 0.78, 0.78, 1.15]
_COL_HEADERS = [
    "Timestamp",
    "Fault Type",
    "Reasoning",
    "Sensors",
    "Temp (°C)",
    "Hum (%)",
    "CO₂ (ppm)",
    "Light (lux)",
    "Actions",
]
_TOP_TABLE_KEY = "top_anomalies"

# Badge colors per fault type (CSS rgba values).
_FAULT_BADGE_COLORS: dict[str, str] = {
    "spike_fault": "rgba(239,68,68,0.20)",
    "calibration_drift": "rgba(251,146,60,0.22)",
    "stuck_fault": "rgba(234,179,8,0.22)",
    "bias_offset": "rgba(252,211,77,0.22)",
    "accuracy_decline": "rgba(148,163,184,0.22)",
}
_FAULT_BADGE_BORDER: dict[str, str] = {
    "spike_fault": "rgba(239,68,68,0.55)",
    "calibration_drift": "rgba(251,146,60,0.55)",
    "stuck_fault": "rgba(234,179,8,0.55)",
    "bias_offset": "rgba(252,211,77,0.55)",
    "accuracy_decline": "rgba(148,163,184,0.55)",
}
_FAULT_LABELS: dict[str, str] = {
    "spike_fault": "Spike",
    "calibration_drift": "Drift",
    "stuck_fault": "Stuck",
    "bias_offset": "Bias",
    "accuracy_decline": "Noise",
    "unknown": "Unknown",
    "polling_dropout": "Dropout",
}


def _fault_badge_html(fault_type: Optional[str]) -> str:
    """Return an inline HTML badge for the given fault_type."""
    if not fault_type:
        return '<span style="color:rgba(148,163,184,0.5);font-size:0.8em;">—</span>'
    bg = _FAULT_BADGE_COLORS.get(fault_type, "rgba(148,163,184,0.15)")
    border = _FAULT_BADGE_BORDER.get(fault_type, "rgba(148,163,184,0.40)")
    label = _FAULT_LABELS.get(fault_type, fault_type.replace("_", " ").title())
    return (
        f'<span style="background:{bg};border:1px solid {border};'
        f'border-radius:4px;padding:2px 7px;font-size:0.78em;'
        f'white-space:nowrap;">{html.escape(label)}</span>'
    )

# Session-state key where a pending button click is stored during render (legacy).
PENDING_FEEDBACK_KEY = "pending_anomaly_feedback"


# ---------------------------------------------------------------------------
# Style helpers (also used by build_html_table / tests)
# ---------------------------------------------------------------------------

def _cell_style(col: str, enrichment: Dict[str, Any]) -> str:
    flags = enrichment.get("flags", {})
    attribution = enrichment.get("attribution", {})

    if not flags.get(col, False):
        return CELL_BASE

    attr_val = float(attribution.get(col, 0.0)) if attribution else 0.0
    if attr_val >= 0.25:
        color = ORANGE
    elif attr_val > 0:
        color = AMBER
    else:
        color = LIGHT_RED

    return f"{CELL_BASE} {color}"


def _sensor_cell(col: str, value: str, enrichment: Dict[str, Any]) -> str:
    """Return a <td> with optional tooltip for flagged sensor cells (HTML table path)."""
    flags = enrichment.get("flags", {})
    style = _cell_style(col, enrichment)
    esc_val = html.escape(value)

    if not flags.get(col, False):
        return f'<td style="{style}">{esc_val}</td>'

    hint = enrichment.get("hints", {}).get(col, "")
    pattern = enrichment.get("pattern_type", "")
    affected = enrichment.get("affected_sensors", [])

    hint_html = f'<div class="ag-tip-hint">{html.escape(hint)}</div>' if hint else ""
    meta_parts: List[str] = []
    if pattern:
        meta_parts.append(f"Pattern: <strong>{html.escape(str(pattern))}</strong>")
    if affected:
        sensors_str = html.escape(
            ", ".join(affected) if isinstance(affected, list) else str(affected)
        )
        meta_parts.append(f"Sensors: <strong>{sensors_str}</strong>")
    meta_html = (
        '<div class="ag-tip-meta">' + " &nbsp;·&nbsp; ".join(meta_parts) + "</div>"
        if meta_parts
        else ""
    )

    tip_html = f'<span class="ag-tip">{hint_html}{meta_html}</span>'
    return f'<td class="ag-cell" style="{style}">{esc_val}{tip_html}</td>'


# ---------------------------------------------------------------------------
# HTML table (kept for unit tests)
# ---------------------------------------------------------------------------

def build_html_table(
    display: pd.DataFrame, enrichments: List[Dict[str, Any]]
) -> str:
    """Build an HTML table with per-cell background colors and hover tooltips."""
    cols = list(display.columns)
    all_headers = cols + ["Fault Type", "Reasoning"]
    parts = [_table_css("html-table"), '<div style="overflow-x: auto;">', "<table>", "<thead><tr>"]

    for col in all_headers:
        parts.append(f'<th style="{HEADER_STYLE}">{html.escape(str(col))}</th>')
    parts.append("</tr></thead><tbody>")

    for pos, (_, row) in enumerate(display.iterrows()):
        enrichment = enrichments[pos] if pos < len(enrichments) else {}
        parts.append("<tr>")
        for col in cols:
            if col in SENSOR_COLUMNS:
                parts.append(_sensor_cell(col, str(row[col]), enrichment))
            else:
                style = f"{CELL_BASE} text-align: left;"
                parts.append(f'<td style="{style}">{html.escape(str(row[col]))}</td>')
        # Fault type badge column
        cell_style = f"{CELL_BASE} text-align: left;"
        parts.append(
            f'<td style="{cell_style}">'
            f'{_fault_badge_html(enrichment.get("fault_type"))}'
            f"</td>"
        )
        parts.append(
            f'<td style="{cell_style}">{html.escape(str(enrichment.get("reasoning", "")))}</td>'
        )
        parts.append("</tr>")

    parts.append("</tbody></table></div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Streamlit-columns renderer (main interactive path)
# ---------------------------------------------------------------------------

def _sensor_col_html(
    val: float,
    flag: bool,
    attr_val: float,
    hint: str,
    pattern: str = "",
    affected: Optional[List[str]] = None,
) -> str:
    """Inline HTML for a coloured sensor value with a styled hover tooltip card."""
    val_str = f"{val:.1f}"
    if not flag:
        return (
            f'<div style="text-align:right;font-size:0.9em;padding:3px 6px;">'
            f"{val_str}</div>"
        )
    if attr_val >= 0.25:
        bg = "rgba(251,146,60,0.32)"
    elif attr_val > 0:
        bg = "rgba(252,211,77,0.28)"
    else:
        bg = "rgba(239,68,68,0.32)"

    hint_html = f'<div class="ag-tip-hint">{html.escape(hint)}</div>' if hint else ""
    meta_parts: List[str] = []
    if pattern:
        meta_parts.append(f"Pattern: <strong>{html.escape(pattern)}</strong>")
    if affected:
        sensors_str = html.escape(
            ", ".join(affected) if isinstance(affected, list) else str(affected)
        )
        meta_parts.append(f"Sensors: <strong>{sensors_str}</strong>")
    meta_html = (
        '<div class="ag-tip-meta">' + " &nbsp;·&nbsp; ".join(meta_parts) + "</div>"
        if meta_parts
        else ""
    )
    tip_html = f'<span class="ag-tip">{hint_html}{meta_html}</span>'
    return (
        f'<div class="ag-cell" style="background:{bg};'
        f'padding:3px 6px;border-radius:3px;font-size:0.9em;text-align:right;">'
        f"{val_str}{tip_html}</div>"
    )


def _affected_sensors_label(row: Any, enrichment: Dict[str, Any]) -> str:
    affected = enrichment.get("affected_sensors", [])
    if not affected and hasattr(row, "get") and row.get("affected_sensors"):
        raw_affected = row.get("affected_sensors")
        affected = (
            raw_affected.split(", ")
            if isinstance(raw_affected, str)
            else raw_affected
        )
    if not isinstance(affected, list) or not affected:
        return "—"
    return ", ".join(format_sensor_label(str(sensor)) for sensor in affected)


def _monitor_jump(
    payload: dict[str, Any],
    tenant: str,
    greenhouse: str,
    *,
    source_df: pd.DataFrame | None,
    available_columns: list[str] | None,
) -> None:
    try:
        queue_monitoring_jump(
            payload["monitor_row"],
            tenant,
            greenhouse,
            available_columns=available_columns,
            source_df=source_df,
        )
        st.switch_page("pages/1_Active_Monitoring/1_Monitoring.py")
    except ValueError as exc:
        st.error(str(exc))


def _render_row_action_buttons(
    action_cell: Any,
    *,
    table_key: str,
    row_index: int,
    payload: dict[str, Any],
    incident_id: Optional[str],
    ts_iso: str,
    state_key: str,
    feedback_state_key: str,
    on_feedback: Optional[Callable[..., None]],
    pending_action: list[Optional[Dict[str, Any]]],
    tenant: str,
    greenhouse: str,
    source_df: pd.DataFrame | None,
    available_columns: list[str] | None,
) -> None:
    safe_key = table_key.replace("_", "-")
    action_cell.markdown(
        f'<span class="ag-actions-{safe_key}" style="display:none"></span>',
        unsafe_allow_html=True,
    )

    buttons: list[dict[str, Any]] = []

    if on_feedback is not None:

        def _accept() -> None:
            pending_action[0] = {
                "incident_id": incident_id,
                "is_true_anomaly": True,
                "timestamp": ts_iso,
                "state_key": state_key,
            }

        def _reject() -> None:
            pending_action[0] = {
                "incident_id": incident_id,
                "is_true_anomaly": False,
                "timestamp": ts_iso,
                "state_key": state_key,
            }

        buttons.extend(
            [
                {"label": "✓", "suffix": "accept", "help": "Accept anomaly", "on_click": _accept},
                {"label": "✗", "suffix": "reject", "help": "Reject anomaly", "on_click": _reject},
            ]
        )

    def _monitor() -> None:
        _monitor_jump(
            payload,
            tenant,
            greenhouse,
            source_df=source_df,
            available_columns=available_columns,
        )

    buttons.append(
        {
            "label": "📈",
            "suffix": "monitor",
            "help": "Open Monitoring for this anomaly's date and sensors",
            "on_click": _monitor,
        }
    )

    _sticky_table.render_compact_buttons(action_cell, table_key, row_index, buttons)


def _row_score(row: Any) -> float:
    for key in ("anomaly_score", "score"):
        if hasattr(row, "get"):
            val = row.get(key)
            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
    return 0.0


def _monitor_row_from_payload(row: Any, ts_iso: str, enrichment: Dict[str, Any]) -> dict[str, Any]:
    cold = row.get("_cold_run") if hasattr(row, "get") else None
    if isinstance(cold, dict):
        jump = {k: v for k, v in cold.items() if not str(k).startswith("_")}
        jump.setdefault("timestamp", ts_iso)
        return jump

    affected = enrichment.get("affected_sensors", [])
    if not affected and hasattr(row, "get") and row.get("affected_sensors"):
        raw_affected = row.get("affected_sensors")
        affected = (
            raw_affected.split(", ")
            if isinstance(raw_affected, str)
            else raw_affected
        )
    monitor_col = affected[0] if isinstance(affected, list) and affected else None
    return {
        "timestamp": ts_iso,
        "detector": row.get("detector", "lstm") if hasattr(row, "get") else "lstm",
        "score": _row_score(row),
        "column": monitor_col,
    }


def render(
    enriched_df: pd.DataFrame,
    *,
    on_feedback: Optional[Callable[..., None]] = None,
    feedback_state_key: str = "feedback_results",
    table_title: str = "Top Anomalies",
    table_key: str = _TOP_TABLE_KEY,
    tenant_id: str = "",
    greenhouse_id: str = "",
    source_df: pd.DataFrame | None = None,
    available_columns: list[str] | None = None,
    empty_message: Optional[str] = None,
    max_height: int = 420,
    show_legend: bool = True,
) -> None:
    """Render anomalies in a sticky scrollable table with Streamlit actions."""
    if enriched_df.empty:
        if empty_message is not None:
            def _empty_row(_index: int, _row: Any, _cells: list[Any]) -> None:
                return

            _sticky_table.render_streamlit_sticky_table(
                table_title,
                _COL_HEADERS,
                _COL_WIDTHS,
                table_key,
                [],
                _empty_row,
                empty_message=empty_message,
                max_height=max_height,
            )
        else:
            st.info("No anomalies to display.")
        return

    if feedback_state_key not in st.session_state:
        st.session_state[feedback_state_key] = {}

    st.markdown(_table_css(table_key), unsafe_allow_html=True)

    tenant = tenant_id or st.session_state.get("selected_tenant", "")
    greenhouse = greenhouse_id or st.session_state.get("selected_greenhouse", "")

    monitor_columns = available_columns
    if monitor_columns is None and source_df is not None and not source_df.empty:
        from src.dashboard.components.sensor_columns import get_all_sensor_columns

        monitor_columns = get_all_sensor_columns(source_df)

    row_payloads: list[dict[str, Any]] = []
    for i, (_, row) in enumerate(enriched_df.iterrows()):
        enrichment = row.get("_enrichment")
        if not isinstance(enrichment, dict):
            enrichment = (
                enriched_df["_enrichment"].iloc[i]
                if "_enrichment" in enriched_df.columns
                and isinstance(enriched_df["_enrichment"].iloc[i], dict)
                else {}
            )
        ts = row.get("timestamp")
        try:
            ts_parsed = pd.to_datetime(ts)
            ts_iso = ts_parsed.isoformat()
        except Exception:
            ts_iso = str(ts)

        row_payloads.append({
            "row": row,
            "enrichment": enrichment,
            "incident_id": enrichment.get("incident_id"),
            "timestamp": ts_iso,
            "state_key": enrichment.get("incident_id") or ts_iso,
            "monitor_row": _monitor_row_from_payload(row, ts_iso, enrichment),
        })

    pending_action_box: list[Optional[Dict[str, Any]]] = [None]

    def _render_row(row_index: int, payload: dict[str, Any], cells: list[Any]) -> None:
        row = payload["row"]
        enrichment = payload["enrichment"]
        incident_id = payload["incident_id"]
        ts_iso = payload["timestamp"]
        state_key = payload["state_key"]

        ts = row.get("timestamp")
        try:
            ts_str = pd.to_datetime(ts).strftime("%d %b %H:%M")
        except Exception:
            ts_str = str(ts)

        cells[0].markdown(ts_str)
        cells[1].markdown(
            _fault_badge_html(enrichment.get("fault_type") or row.get("fault_type")),
            unsafe_allow_html=True,
        )
        cells[2].markdown(str(enrichment.get("reasoning") or row.get("reasoning") or "—"))
        cells[3].markdown(_affected_sensors_label(row, enrichment))

        flags = enrichment.get("flags", {})
        attribution = enrichment.get("attribution", {})
        hints = enrichment.get("hints", {})
        pattern_str = enrichment.get("pattern_type", "")
        affected_list = enrichment.get("affected_sensors", [])

        for col_idx, sensor in enumerate(SENSOR_COLUMNS, start=4):
            raw = row.get(sensor)
            if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                cells[col_idx].markdown("—")
                continue
            val = float(raw)
            flag = flags.get(sensor, False)
            attr_val = float(attribution.get(sensor, 0)) if flag else 0.0
            hint = hints.get(sensor, "") if flag else ""
            cells[col_idx].markdown(
                _sensor_col_html(
                    val, flag, attr_val, hint,
                    pattern=pattern_str,
                    affected=affected_list if isinstance(affected_list, list) else [],
                ),
                unsafe_allow_html=True,
            )

        _render_row_action_buttons(
            cells[_ACTIONS_COL_INDEX],
            table_key=table_key,
            row_index=row_index,
            payload=payload,
            incident_id=incident_id,
            ts_iso=ts_iso,
            state_key=state_key,
            feedback_state_key=feedback_state_key,
            on_feedback=on_feedback,
            pending_action=pending_action_box,
            tenant=tenant,
            greenhouse=greenhouse,
            source_df=source_df,
            available_columns=monitor_columns,
        )

    _sticky_table.render_streamlit_sticky_table(
        table_title,
        _COL_HEADERS,
        _COL_WIDTHS,
        table_key,
        row_payloads,
        _render_row,
        row_records=row_payloads,
        max_height=max_height,
    )

    pending_action = pending_action_box[0]
    if pending_action is not None and on_feedback is not None:
        on_feedback(
            pending_action["incident_id"],
            pending_action["is_true_anomaly"],
            pending_action["timestamp"],
            pending_action["state_key"],
        )

    if not show_legend:
        return

    st.markdown(
        '<div style="display:flex;gap:16px;font-size:12px;margin-top:8px;flex-wrap:wrap;">'
        '<span><span style="display:inline-block;width:12px;height:12px;vertical-align:middle;'
        'margin-right:4px;border-radius:2px;background:rgba(239,68,68,0.7);"></span>'
        "Range anomaly</span>"
        '<span><span style="display:inline-block;width:12px;height:12px;vertical-align:middle;'
        'margin-right:4px;border-radius:2px;background:rgba(251,146,60,0.7);"></span>'
        "Strong attribution (≥0.25)</span>"
        '<span><span style="display:inline-block;width:12px;height:12px;vertical-align:middle;'
        'margin-right:4px;border-radius:2px;background:rgba(252,211,77,0.7);"></span>'
        "Contributing sensor</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    multi_sensor_rows = sum(
        1
        for e in (
            enriched_df["_enrichment"]
            if "_enrichment" in enriched_df.columns
            else []
        )
        if len(e.get("affected_sensors", [])) >= 2
    )
    if multi_sensor_rows:
        st.caption(f"{multi_sensor_rows} row(s) with multi-sensor attribution.")
