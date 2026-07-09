"""AppTest coverage for the redesigned System Metrics page.

Element lists are snapshotted inside the patch context immediately after
``at.run()``; reading AppTest accessors after the patches unwind can otherwise
return inconsistent results for this page.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

_PAGE = "src/dashboard/views/4_metrics.py"
_BOUNDS = (pd.Timestamp("2026-12-01"), pd.Timestamp("2026-12-31"))


@dataclass
class _Snap:
    has_exception: bool = False
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    metric_labels: list[str] = field(default_factory=list)
    markdowns: list[str] = field(default_factory=list)
    download_buttons: int = 0

    @classmethod
    def of(cls, at: AppTest) -> "_Snap":
        return cls(
            has_exception=bool(at.exception),
            warnings=[w.value for w in at.warning],
            infos=[i.value for i in at.info],
            metric_labels=[m.label for m in at.metric],
            markdowns=[m.value for m in at.markdown],
            download_buttons=len(at.get("download_button")),
        )


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-12-20", periods=40, freq="h"),
            "anomaly_score": [0.1] * 40,
        }
    )


def _run(*, metrics_return=None, metrics_raises=False, df: pd.DataFrame | None = None) -> _Snap:
    at = AppTest.from_file(_PAGE, default_timeout=30)
    at.session_state["selected_tenant"] = "tenant-a"
    at.session_state["selected_greenhouse"] = "gh-1"
    df = _df() if df is None else df

    target = "src.dashboard.services.api_client.DashboardApiClient.get_monitoring_metrics"
    metrics_patch = (
        patch(target, side_effect=RuntimeError("API down"))
        if metrics_raises
        else patch(target, return_value=metrics_return or {})
    )

    with ExitStack() as stack:
        # st.page_link needs a registered navigation, which AppTest.from_file has
        # no concept of; stub it (it works in the live multipage app).
        stack.enter_context(patch("streamlit.page_link", lambda *a, **k: None))
        stack.enter_context(patch(
            "src.dashboard.components.global_sidebar.render_global_sidebar",
            lambda *a, **k: None,
        ))
        stack.enter_context(patch(
            "src.dashboard.components.inference_results.load_inference_results",
            return_value=None,
        ))
        stack.enter_context(patch(
            "src.dashboard.services.anomalies_page_data.resolve_data_bounds",
            return_value=_BOUNDS,
        ))
        stack.enter_context(patch(
            "src.dashboard.services.window_data.load_page_window",
            return_value=(df, False),
        ))
        stack.enter_context(metrics_patch)
        at.run()
        return _Snap.of(at)


def test_data_health_renders_when_pipeline_api_down():
    snap = _run(metrics_raises=True)
    assert not snap.has_exception
    assert "Total readings" in snap.metric_labels
    assert "Score coverage" in snap.metric_labels
    assert any("start the fastapi server" in w.lower() for w in snap.warnings)


def test_pipeline_metrics_shown_with_detections():
    snap = _run(metrics_return={
        "total_detections": 10, "true_positives": 2, "false_positives": 1,
        "false_positive_rate": 0.1, "avg_confidence": 0.8,
    })
    assert not snap.has_exception
    assert "Total detections" in snap.metric_labels
    assert any("False positive rate" in lbl for lbl in snap.metric_labels)


def test_feedback_nudge_when_no_operator_labels():
    snap = _run(metrics_return={
        "total_detections": 5, "true_positives": 0, "false_positives": 0,
    })
    assert not snap.has_exception
    assert any("No operator labels yet" in i for i in snap.infos)


def test_no_static_model_info_or_hardcoded_greenhouse_count():
    snap = _run(metrics_return={"total_detections": 0})
    blob = " ".join(snap.markdowns + snap.infos + snap.metric_labels)
    assert "Model Architecture" not in blob
    assert "Greenhouses Monitored" not in blob
    assert snap.download_buttons == 0


def test_pipeline_scope_labeled_global():
    snap = _run(metrics_return={
        "total_detections": 3, "true_positives": 1, "false_positives": 0,
    })
    assert any("all tenants" in i.lower() for i in snap.infos)
