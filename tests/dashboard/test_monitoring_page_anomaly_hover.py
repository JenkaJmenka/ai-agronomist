"""The Monitoring page's anomaly hover label: hours for a freeze, sigma for a z.

Step-5 verification. ``_add_anomaly_markers`` in
``src/dashboard/views/1_Active_Monitoring/1_Monitoring.py`` is one of the five
step-2 display patches and had no test touching it — it is a private function
in a Streamlit page module whose name starts with a digit, and importing that
module normally executes the page.

So the module's DEFINITIONS are loaded by exec'ing the source up to the first
top-level ``st.set_page_config(`` call. The split point is asserted to exist,
so a restructure of the page fails this file loudly instead of silently
skipping the behaviour it covers. Everything exercised below is the real
production function body, unmodified.

Both stored shapes now hover identically. Step 6 consolidated the fallback
into ``diagnosis_enricher.resolve_stuck_duration_hours``, which this page
imports, so a legacy row (duration in ``z_score``, no new field) hovers the
same hours as a current-shape one. The legacy pair below was
``xfail(strict=True)`` plus a positive pin of the defect until step 6
(``cb6fdf4``).
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import pytest

PAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "dashboard" / "views" / "1_Active_Monitoring" / "1_Monitoring.py"
)
PAGE_SETUP_MARKER = "\nst.set_page_config("

FROZEN_CHANNEL = "humidity"
ONSET_TS = "2026-12-18T13:00:00+00:00"
GATE_HOURS = 6.0
STAT_Z_SCORE = 8.5
WINDOW_ROW_COUNT = 30
CADENCE_MINUTES = 5


@pytest.fixture(scope="module")
def monitoring_page() -> types.ModuleType:
    """The page's definitions, without its top-level Streamlit page body."""
    source = PAGE_PATH.read_text()
    assert PAGE_SETUP_MARKER in source, (
        f"{PAGE_PATH.name} no longer calls st.set_page_config at top level; "
        "the definition/page-body split point this fixture relies on has moved"
    )
    definitions = source.split(PAGE_SETUP_MARKER)[0]
    module = types.ModuleType("monitoring_page_definitions")
    module.__file__ = str(PAGE_PATH)
    exec(compile(definitions, str(PAGE_PATH), "exec"), module.__dict__)
    assert hasattr(module, "_add_anomaly_markers"), (
        "_add_anomaly_markers is no longer defined before the page body"
    )
    return module


@pytest.fixture
def window_frame() -> pd.DataFrame:
    """A frame spanning the onset, with a spread of values so y-normalisation works."""
    start = datetime(2026, 12, 18, 12, 0, tzinfo=timezone.utc)
    return pd.DataFrame(
        {
            "timestamp": [
                start + timedelta(minutes=CADENCE_MINUTES * index)
                for index in range(WINDOW_ROW_COUNT)
            ],
            FROZEN_CHANNEL: [95.0 + 0.2 * index for index in range(WINDOW_ROW_COUNT)],
        }
    )


def _marker_labels(module, frame: pd.DataFrame, highlight: dict) -> list[str]:
    """Every marker trace name the page would render for one highlight."""
    figure = module._add_anomaly_markers(
        go.Figure(), frame, [FROZEN_CHANNEL], [highlight]
    )
    return [trace.name for trace in figure.data if trace.name]


def _current_stuck_highlight() -> dict:
    return {
        "timestamp": ONSET_TS,
        "detector": "stuck_fault",
        "score": 1.0,
        "column": FROZEN_CHANNEL,
        "severity": "urgent",
        "z_score": None,
        "frozen_hours_at_detection": GATE_HOURS,
        "value": 100.0,
    }


def _legacy_stuck_highlight() -> dict:
    highlight = _current_stuck_highlight()
    del highlight["frozen_hours_at_detection"]
    highlight["z_score"] = GATE_HOURS
    return highlight


def _statistical_highlight() -> dict:
    return {
        "timestamp": ONSET_TS,
        "detector": "statistical",
        "score": 0.8,
        "column": FROZEN_CHANNEL,
        "severity": "critical",
        "z_score": STAT_Z_SCORE,
        "value": 99.0,
    }


class TestStuckHoverShowsHours:
    def test_a_current_stuck_row_hovers_frozen_hours(
        self, monitoring_page, window_frame
    ) -> None:
        labels = _marker_labels(monitoring_page, window_frame, _current_stuck_highlight())
        assert labels, "no marker rendered"
        assert all("frozen 6.0h" in label for label in labels)

    def test_a_current_stuck_row_never_hovers_a_z_score(
        self, monitoring_page, window_frame
    ) -> None:
        labels = _marker_labels(monitoring_page, window_frame, _current_stuck_highlight())
        assert all("z=" not in label for label in labels)

    def test_a_legacy_stuck_row_hovers_frozen_hours(
        self, monitoring_page, window_frame
    ) -> None:
        """Hours with a unit, and no z-score anywhere on the marker."""
        labels = _marker_labels(monitoring_page, window_frame, _legacy_stuck_highlight())
        assert labels, "no marker rendered"
        assert all(f"frozen {GATE_HOURS:.1f}h" in label for label in labels)
        assert all("z=" not in label for label in labels)

    def test_legacy_and_current_shapes_hover_identically(
        self, monitoring_page, window_frame
    ) -> None:
        """Convergence: the stored shape must not be visible in the hover.

        The complement to the pin above — that one would still pass if the two
        shapes hovered different but individually plausible text. This is the
        assertion that fails first if this page reintroduces its own copy of
        the fallback rule instead of delegating.
        """
        legacy = _marker_labels(
            monitoring_page, window_frame, _legacy_stuck_highlight()
        )
        current = _marker_labels(
            monitoring_page, window_frame, _current_stuck_highlight()
        )
        assert legacy == current


class TestOtherDetectorsKeepTheirOwnHover:
    """Non-vacuity for the branch above: the hours label is stuck-specific."""

    def test_a_statistical_row_still_hovers_its_z_score(
        self, monitoring_page, window_frame
    ) -> None:
        labels = _marker_labels(monitoring_page, window_frame, _statistical_highlight())
        assert labels
        assert all(f"z={STAT_Z_SCORE:.1f}" in label for label in labels)
        assert all("frozen" not in label for label in labels)

    def test_a_row_with_neither_field_hovers_no_magnitude(
        self, monitoring_page, window_frame
    ) -> None:
        highlight = {
            "timestamp": ONSET_TS,
            "detector": "lstm",
            "score": 0.7,
            "column": None,
            "severity": None,
            "z_score": None,
        }
        labels = _marker_labels(monitoring_page, window_frame, highlight)
        assert labels
        assert all("z=" not in label and "frozen" not in label for label in labels)

    def test_a_stuck_row_with_a_null_frozen_value_does_not_raise(
        self, monitoring_page, window_frame
    ) -> None:
        """``frozen_hours_at_detection`` explicitly null, as a stored blob holds it.

        The branch guard is ``is not None``, so this must fall through to the
        z-score branch (also null here) and render no magnitude rather than
        formatting ``None``.
        """
        highlight = dict(_current_stuck_highlight(), frozen_hours_at_detection=None)
        labels = _marker_labels(monitoring_page, window_frame, highlight)
        assert labels
        assert all("frozen" not in label for label in labels)


    def test_a_stuck_row_with_a_non_numeric_frozen_value_does_not_raise(
        self, monitoring_page, window_frame
    ) -> None:
        """Task 2c item 1: a non-numeric stored value must not reach the
        ``f"{v:.1f}"`` hover format. Before the fix, this raised ValueError
        at render time instead of degrading to no magnitude clause.
        """
        highlight = dict(
            _current_stuck_highlight(), frozen_hours_at_detection="unknown"
        )
        labels = _marker_labels(monitoring_page, window_frame, highlight)
        assert labels
        assert all("frozen" not in label for label in labels)
