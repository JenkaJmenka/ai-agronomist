"""Scoped CSS for the anomaly Table view (kept out of the component so the
component file stays within the 300-line limit).

Injected once per render; styles the green group accents, pills, and the
compact Material-icon action buttons (colour keyed by position:
confirm / reject / monitor).
"""

from __future__ import annotations

# One green accent shared with the standalone comparison design.
_ACCENT = "rgba(47,125,79,0.90)"
_ACCENT_SOFT = "rgba(47,125,79,0.14)"


def view_css() -> str:
    return f"""
<style>
/* Group accent bar on each collapsible section header */
div[data-testid="stExpander"]:has(.atv-group) summary {{
  border-left: 3px solid {_ACCENT};
}}
.atv-pill {{
  border-radius: 5px; padding: 2px 8px; font-size: 0.76em;
  white-space: nowrap; display: inline-block;
}}
.atv-count {{
  background: {_ACCENT_SOFT}; border: 1px solid {_ACCENT};
  color: inherit; border-radius: 20px; padding: 1px 9px;
  font-size: 0.72em; font-variant-numeric: tabular-nums;
}}
.atv-ts {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.86em; }}
.atv-hdr {{
  font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em;
  color: rgba(148,163,184,0.95); font-weight: 600;
}}
/* Compact, quiet action buttons — colour keyed by position (confirm/reject/monitor) */
div[data-testid="column"]:has(.atv-actions) [data-testid="stButton"] button {{
  min-width: 2.1rem !important; padding: 0.15rem 0.4rem !important;
  border-radius: 6px !important; background: transparent !important;
}}
div[data-testid="column"]:has(.atv-actions) [data-testid="stHorizontalBlock"]
  [data-testid="column"]:nth-child(1) [data-testid="stButton"] button {{
  color: rgb(47,125,79) !important; border-color: rgba(47,125,79,0.45) !important;
}}
div[data-testid="column"]:has(.atv-actions) [data-testid="stHorizontalBlock"]
  [data-testid="column"]:nth-child(1) [data-testid="stButton"] button:hover {{
  background: rgba(47,125,79,0.12) !important; border-color: rgba(47,125,79,0.85) !important;
}}
div[data-testid="column"]:has(.atv-actions) [data-testid="stHorizontalBlock"]
  [data-testid="column"]:nth-child(2) [data-testid="stButton"] button {{
  color: rgb(181,67,44) !important; border-color: rgba(181,67,44,0.40) !important;
}}
div[data-testid="column"]:has(.atv-actions) [data-testid="stHorizontalBlock"]
  [data-testid="column"]:nth-child(2) [data-testid="stButton"] button:hover {{
  background: rgba(181,67,44,0.12) !important; border-color: rgba(181,67,44,0.85) !important;
}}
div[data-testid="column"]:has(.atv-actions) [data-testid="stHorizontalBlock"]
  [data-testid="column"]:nth-child(3) [data-testid="stButton"] button {{
  color: rgb(100,116,139) !important; border-color: rgba(100,116,139,0.40) !important;
}}
div[data-testid="column"]:has(.atv-actions) [data-testid="stHorizontalBlock"]
  [data-testid="column"]:nth-child(3) [data-testid="stButton"] button:hover {{
  background: rgba(100,116,139,0.14) !important; border-color: rgba(100,116,139,0.85) !important;
}}
</style>
"""
