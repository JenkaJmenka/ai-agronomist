"""
Styled anomaly table with per-sensor highlighting, hover tooltips, and confirm/reject buttons.

Rows are rendered as Streamlit columns so native st.button widgets can appear in the
Actions column. Sensor cells retain coloured backgrounds via inline HTML spans.
The ``build_html_table`` helper is kept for unit-test coverage.
"""

from __future__ import annotations

import html
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import streamlit as st

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
_TOOLTIP_CSS = """
<style>
/* ── Sensor-cell tooltip ─────────────────────────────────────────── */
.ag-cell { position: relative; cursor: default; }
.ag-tip {
  visibility: hidden;
  opacity: 0;
  /* Appear after a short 80 ms pause; disappear instantly */
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
}
.ag-cell:hover .ag-tip {
  visibility: visible;
  opacity: 1;
  transition: opacity 0.12s ease 0.08s, visibility 0s linear 0s;
}
.ag-tip-hint { margin-bottom: 6px; }
.ag-tip-meta { color: #94a3b8; font-size: 12px; }

/* ── Action column buttons ───────────────────────────────────────── */
/* Target the nested stHorizontalBlock that row_cols[9].columns(2) creates.
   First sub-column = Confirm (green), last sub-column = Reject (red). */
[data-testid="column"] [data-testid="stHorizontalBlock"] [data-testid="column"]:first-child button {
  color: rgb(34, 197, 94) !important;
  border-color: rgba(34, 197, 94, 0.55) !important;
  background-color: rgba(34, 197, 94, 0.10) !important;
}
[data-testid="column"] [data-testid="stHorizontalBlock"] [data-testid="column"]:first-child button:hover {
  background-color: rgba(34, 197, 94, 0.20) !important;
}
[data-testid="column"] [data-testid="stHorizontalBlock"] [data-testid="column"]:last-child button {
  color: rgb(239, 68, 68) !important;
  border-color: rgba(239, 68, 68, 0.55) !important;
  background-color: rgba(239, 68, 68, 0.10) !important;
}
[data-testid="column"] [data-testid="stHorizontalBlock"] [data-testid="column"]:last-child button:hover {
  background-color: rgba(239, 68, 68, 0.20) !important;
}
</style>
"""

# Column layout for the Streamlit-columns-based render.
_COL_WIDTHS = [2.2, 0.9, 1.0, 1.1, 1.8, 0.9, 0.9, 0.9, 0.9, 1.4]
_COL_HEADERS = [
    "Timestamp", "Score", "Pattern", "Fault Type", "Sensors",
    "Temp (°C)", "Hum (%)", "CO₂ (ppm)", "Light (lux)", "Action",
]

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

# Session-state key where a pending button click is stored during render.
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
    all_headers = cols + ["Fault Type"]
    parts = [_TOOLTIP_CSS, '<div style="overflow-x: auto;">', "<table>", "<thead><tr>"]

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


def render(
    enriched_df: pd.DataFrame,
    *,
    on_feedback: Optional[Callable[..., None]] = None,
    feedback_state_key: str = "feedback_results",
) -> None:
    """Render the anomaly table as Streamlit columns with coloured cells and action buttons.

    Args:
        enriched_df: Output of ``enrich_dataframe()``.
        on_feedback: Called with ``(incident_id_or_none, is_true_anomaly, timestamp_str)``
            when a button is clicked. ``incident_id`` is None when no incident is matched
            yet; the caller is responsible for registering the anomaly first.
        feedback_state_key: Session-state key used to track already-submitted feedback.
    """
    if enriched_df.empty:
        st.info("No anomalies to display.")
        return

    if feedback_state_key not in st.session_state:
        st.session_state[feedback_state_key] = {}

    # Inject tooltip CSS once into the page so ag-cell/ag-tip works inside st.columns cells.
    st.markdown(_TOOLTIP_CSS, unsafe_allow_html=True)

    # --- Header row ---
    hcols = st.columns(_COL_WIDTHS)
    for hcol, label in zip(hcols, _COL_HEADERS):
        hcol.markdown(f"**{label}**")
    st.markdown(
        '<hr style="margin:2px 0;border-color:rgba(128,128,128,0.4)">',
        unsafe_allow_html=True,
    )

    pending_action: Optional[Dict[str, Any]] = None

    for i, (_, row) in enumerate(enriched_df.iterrows()):
        enrichment = (
            enriched_df["_enrichment"].iloc[i]
            if "_enrichment" in enriched_df.columns
            else {}
        )
        incident_id = enrichment.get("incident_id")

        row_cols = st.columns(_COL_WIDTHS)

        # Timestamp — compact "08 Jun 12:00" format
        ts = row.get("timestamp")
        try:
            ts_parsed = pd.to_datetime(ts)
            ts_str = ts_parsed.strftime("%d %b %H:%M")
            ts_iso = ts_parsed.isoformat()
        except Exception:
            ts_str = str(ts)
            ts_iso = str(ts)
        row_cols[0].write(ts_str)

        # Anomaly score
        score = row.get("anomaly_score", 0)
        row_cols[1].write(f"{float(score):.3f}")

        # Pattern type
        pattern = enrichment.get("pattern_type") or "—"
        row_cols[2].write(pattern)

        # Fault type badge
        fault_type = enrichment.get("fault_type")
        row_cols[3].markdown(_fault_badge_html(fault_type), unsafe_allow_html=True)

        # Affected sensors
        affected = enrichment.get("affected_sensors", [])
        if isinstance(affected, list):
            affected_str = ", ".join(affected) if affected else "—"
        else:
            affected_str = str(affected) if affected else "—"
        row_cols[4].write(affected_str)

        # Sensor value cells with colour highlights and hover tooltip card
        flags = enrichment.get("flags", {})
        attribution = enrichment.get("attribution", {})
        hints = enrichment.get("hints", {})
        pattern_str = enrichment.get("pattern_type", "")
        affected_list = enrichment.get("affected_sensors", [])

        for col_idx, sensor in enumerate(SENSOR_COLUMNS, start=5):
            raw = row.get(sensor)
            if raw is None:
                row_cols[col_idx].write("—")
                continue
            val = float(raw)
            flag = flags.get(sensor, False)
            attr_val = float(attribution.get(sensor, 0)) if flag else 0.0
            hint = hints.get(sensor, "") if flag else ""
            row_cols[col_idx].markdown(
                _sensor_col_html(
                    val, flag, attr_val, hint,
                    pattern=pattern_str,
                    affected=affected_list if isinstance(affected_list, list) else [],
                ),
                unsafe_allow_html=True,
            )

        # Action column — always show ✓/✗ buttons; use incident_id or ts_iso as state key
        state_key = incident_id or ts_iso
        prior = st.session_state[feedback_state_key].get(state_key)
        if prior:
            icon = "✓" if prior["is_true_anomaly"] else "✗"
            row_cols[9].markdown(f"**{icon}**")
        else:
            btn_c1, btn_c2 = row_cols[9].columns(2)
            btn_suffix = incident_id if incident_id else f"row_{i}"
            if btn_c1.button("✓", key=f"conf_{btn_suffix}", help="Confirm anomaly"):
                pending_action = {
                    "incident_id": incident_id,
                    "is_true_anomaly": True,
                    "timestamp": ts_iso,
                    "state_key": state_key,
                }
            if btn_c2.button("✗", key=f"rej_{btn_suffix}", help="Mark as false positive"):
                pending_action = {
                    "incident_id": incident_id,
                    "is_true_anomaly": False,
                    "timestamp": ts_iso,
                    "state_key": state_key,
                }

        st.markdown(
            '<hr style="margin:2px 0;border-color:rgba(128,128,128,0.15)">',
            unsafe_allow_html=True,
        )

    # Fire callback after the loop so st.rerun() (if called inside on_feedback) restarts cleanly.
    if pending_action is not None and on_feedback is not None:
        on_feedback(
            pending_action["incident_id"],
            pending_action["is_true_anomaly"],
            pending_action["timestamp"],
            pending_action["state_key"],
        )

    # Coloured legend
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
