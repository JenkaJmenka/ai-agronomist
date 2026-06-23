"""Learning progress bar and retrain history panel."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import streamlit as st

from src.config import ACTIVE_LEARNING_MIN_QUALITY_LABELS


def render(
    progress: Optional[Dict[str, Any]] = None,
    statistics: Optional[Dict[str, Any]] = None,
    retraining: Optional[Dict[str, Any]] = None,
) -> None:
    """Show X/20 progress and optional post-retrain banner."""
    progress = progress or {}
    statistics = statistics or {}

    high_quality = int(
        progress.get("high_quality_count")
        or statistics.get("high_quality_feedback")
        or 0
    )
    target = ACTIVE_LEARNING_MIN_QUALITY_LABELS
    pct = min(high_quality / target, 1.0) if target else 0.0

    st.subheader("Learning progress")
    st.progress(pct, text=f"{high_quality}/{target} high-quality labels")
    st.caption(
        f"Retrainings completed: {statistics.get('retrainings_completed', 0)}"
    )

    if retraining and retraining.get("status") == "retraining_completed":
        before = retraining.get("before_metrics", {})
        after = retraining.get("after_metrics", {})
        action = retraining.get("action", "unknown")
        st.success(
            f"Retrain {action}: FP rate {before.get('fp_rate', '?')} → "
            f"{after.get('fp_rate', '?')}"
        )
