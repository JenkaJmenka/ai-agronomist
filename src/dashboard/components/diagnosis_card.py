"""Diagnosis summary card for operator review."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import streamlit as st


def render(
    incident: Dict[str, Any],
    inference: Optional[Dict[str, Any]] = None,
) -> None:
    """Show pattern, attribution, root cause, and recommended actions."""
    details = incident.get("root_cause_details") or {}
    data = inference or {}

    pattern = details.get("pattern_type") or data.get("pattern_type", "unknown")
    explanation = details.get("pattern_explanation") or data.get("reasoning", "")

    st.subheader("Diagnosis")
    st.write(f"**Pattern:** {pattern}")
    if explanation:
        st.caption(explanation)

    attribution = details.get("attribution") or data.get("attribution") or {}
    if attribution:
        st.markdown("**Sensor attribution**")
        for sensor, score in sorted(
            attribution.items(), key=lambda x: x[1], reverse=True
        ):
            st.progress(min(float(score), 1.0), text=f"{sensor}: {score:.2f}")

    root_cause = incident.get("root_cause") or data.get("root_cause")
    if root_cause:
        st.write(f"**Root cause:** {root_cause}")

    actions: List[str] = data.get("immediate_actions") or []
    if actions:
        st.markdown("**Immediate actions**")
        for action in actions:
            st.write(f"- {action}")

    detector_scores = details.get("detector_scores") or data.get("detector_scores")
    if detector_scores:
        st.markdown("**Detector breakdown**")
        cols = st.columns(len(detector_scores))
        for col, (name, score) in zip(cols, detector_scores.items()):
            col.metric(name, "—" if score is None else f"{float(score):.3f}")
