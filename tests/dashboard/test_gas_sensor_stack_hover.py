"""Task 2c item 2: plot_gas_sensor_stack's anomaly-marker hover text.

charts.py:657 used to always render "Z-score: 0.00" for a stuck_fault
anomaly point (it has no z_score -- see UploadHighAnomalyDetail), because
``ap.get('z_score')`` fell back to ``0`` whenever the key was present but
null. This pins the fix: a stuck row's hover shows its
frozen_hours_at_detection under an hours label and never "Z-score: ...",
for both the current shape (new field) and the legacy shape (duration
stored in z_score, discriminated by detector == "stuck_fault").

``anomaly_points`` is fed from ``3_Gas_Monitoring.py``'s ``_anomaly_points``,
which is always ``[]`` today (its only append site is a bare ``pass``), so
this calls ``plot_gas_sensor_stack`` directly rather than through the page --
the only way to exercise this line at all.
"""

from __future__ import annotations

import pandas as pd

from src.dashboard.components.charts import plot_gas_sensor_stack

COLUMN = "gas_co2"
ONSET_TS = pd.Timestamp("2026-12-18T13:00:00+00:00")
GATE_HOURS = 6.0
STAT_Z_SCORE = 8.5


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(ONSET_TS, periods=10, freq="5min"),
            COLUMN: [400.0 + index for index in range(10)],
        }
    )


def _current_stuck_point() -> dict:
    return {
        "timestamp": ONSET_TS,
        "detector": "stuck_fault",
        "column": COLUMN,
        "value": 400.0,
        "severity": "critical",
        "z_score": None,
        "frozen_hours_at_detection": GATE_HOURS,
    }


def _legacy_stuck_point() -> dict:
    point = _current_stuck_point()
    del point["frozen_hours_at_detection"]
    point["z_score"] = GATE_HOURS
    return point


def _statistical_point() -> dict:
    return {
        "timestamp": ONSET_TS,
        "detector": "statistical",
        "column": COLUMN,
        "value": 400.0,
        "severity": "warning",
        "z_score": STAT_Z_SCORE,
    }


def _marker_hover(anomaly_points: list[dict]) -> str:
    fig = plot_gas_sensor_stack(
        _frame(), [COLUMN], {COLUMN: "CO2"}, {COLUMN: "ppm"}, anomaly_points
    )
    markers = [trace for trace in fig.data if trace.mode == "markers"]
    assert markers, "no anomaly marker rendered"
    return markers[0].hovertemplate


class TestStuckRowsHoverFrozenHoursNeverZScore:
    def test_current_shape_shows_frozen_hours(self) -> None:
        hover = _marker_hover([_current_stuck_point()])
        assert "Frozen (h): 6.00" in hover
        assert "Z-score:" not in hover

    def test_legacy_shape_shows_frozen_hours(self) -> None:
        hover = _marker_hover([_legacy_stuck_point()])
        assert "Frozen (h): 6.00" in hover
        assert "Z-score:" not in hover

    def test_legacy_and_current_shapes_hover_identically(self) -> None:
        assert _marker_hover([_legacy_stuck_point()]) == _marker_hover(
            [_current_stuck_point()]
        )


class TestStatisticalRowsKeepTheirZScoreHover:
    """Non-vacuity: the hours label is stuck-specific, not a blanket change."""

    def test_a_statistical_row_still_shows_z_score(self) -> None:
        hover = _marker_hover([_statistical_point()])
        assert f"Z-score: {STAT_Z_SCORE:.2f}" in hover
        assert "Frozen" not in hover
