"""AppTest coverage for the redesigned Threshold Management page."""

from contextlib import ExitStack
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

_PAGE = "src/dashboard/views/3_thresholds.py"
_BOUNDS = (pd.Timestamp("2026-12-01"), pd.Timestamp("2026-12-31"))


def _scored_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-12-20", periods=6, freq="h"),
            "anomaly_score": [0.05, 0.08, 0.10, 0.12, 0.15, 0.18],
        }
    )


def _run(*, df: pd.DataFrame, inf_results: dict | None, click_contains: str | None = None):
    at = AppTest.from_file(_PAGE, default_timeout=30)
    at.session_state["selected_tenant"] = "tenant-a"
    at.session_state["selected_greenhouse"] = "gh-1"
    saved_inf = {"results": inf_results} if inf_results is not None else None
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
            return_value=saved_inf,
        ))
        stack.enter_context(patch(
            "src.dashboard.services.anomalies_page_data.resolve_data_bounds",
            return_value=_BOUNDS,
        ))
        stack.enter_context(patch(
            "src.dashboard.services.window_data.load_page_window",
            return_value=(df, False),
        ))
        at.run()
        # Any re-run (e.g. after a button click) must stay inside the patch scope,
        # otherwise the rerun hits the real data layer / API.
        if click_contains is not None:
            targets = [b for b in at.button if click_contains in b.label]
            assert targets, f"button containing {click_contains!r} not found"
            targets[0].click().run()
    return at


def test_slider_defaults_to_calibrated_threshold_not_static_08():
    at = _run(df=_scored_df(), inf_results={"threshold_used": 0.12})
    assert not at.exception
    assert len(at.slider) == 1
    # Calibrated default lands inside the real score scale, not the 0.8 default.
    assert at.slider[0].value == 0.12


def test_no_chart_no_download_on_thresholds_page():
    at = _run(df=_scored_df(), inf_results={"threshold_used": 0.12})
    assert len(at.get("arrow_chart")) == 0
    assert len(at.get("plotly_chart")) == 0
    assert len(at.get("download_button")) == 0


def test_save_writes_session_threshold():
    at = _run(df=_scored_df(), inf_results={"threshold_used": 0.12}, click_contains="Save")
    assert at.session_state["anomaly_threshold"] == 0.12


def test_empty_window_warns_with_available_range():
    at = _run(df=pd.DataFrame(columns=["timestamp", "anomaly_score"]),
              inf_results={"threshold_used": 0.12})
    assert not at.exception
    assert any("No data in the selected window" in w.value for w in at.warning)
    assert len(at.slider) == 0  # stopped before the slider


def test_unscored_window_points_to_anomaly_upload():
    df = pd.DataFrame({"timestamp": pd.date_range("2026-12-20", periods=3, freq="h")})
    at = _run(df=df, inf_results=None)
    assert not at.exception
    assert any("No anomaly scores" in w.value for w in at.warning)
