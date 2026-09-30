"""Stuck-fault rows in the dashboard: routing, legacy payloads, hours labels.

Step-5 verification of the five display patches in step 2, plus the two
legacy-payload cases. ``JobPortion.result``/``Job.result`` blobs written
before ``frozen_hours_at_detection`` existed still hold stuck rows in the old
shape (numeric ``z_score`` carrying the duration, no new field) and there is
no backfill migration, so both shapes reach every reader here.

Both shapes now converge on one rendering. Step 6 consolidated the fallback
decision into ``diagnosis_enricher.resolve_stuck_duration_hours`` and every
display site delegates to it, so a legacy row renders exactly as a
current-shape one does. The pairs below assert that convergence from both
sides: one test per site pins the absolute rendering, its sibling pins that
the two shapes are indistinguishable — which is the property that would break
first if one site grew its own private copy of the rule again.

These four tests were ``xfail(strict=True)`` plus positive pins of the defect
until step 6 (``cb6fdf4``) fixed it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.dashboard.components.inference_results import (
    _statistical_anomalies_from_results,
    stat_rows_to_jump_rows,
)
from src.dashboard.components.monitoring_jump import (
    anomalies_to_display_frame,
    anomaly_display_columns,
    format_anomaly_cell,
)
from src.dashboard.services.diagnosis_enricher import (
    _build_reasoning,
    cold_run_row_to_series,
)
from src.models.episode_grouper import group_alerts_into_episodes
from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.config import STUCK_WALLCLOCK_MIN_HOURS

SECONDS_PER_HOUR = 3600.0
ONSET_TS = "2026-12-18T13:00:00+00:00"
FROZEN_CHANNEL = "humidity"
GATE_HOURS = float(STUCK_WALLCLOCK_MIN_HOURS)

REASONING_KWARGS = dict(
    incident=None, details={}, hints={}, pattern_type=None, affected_sensors=[]
)


def _current_stuck_row() -> dict:
    """The shape ``upload_portions._stuck_dict_to_high_detail`` writes today."""
    return {
        "timestamp": ONSET_TS,
        "detected_ts": "2026-12-18T19:00:00+00:00",
        "detector": "stuck_fault",
        "score": 1.0,
        "column": FROZEN_CHANNEL,
        "severity": "urgent",
        "method": "wallclock_stuck",
        "value": 100.0,
        "z_score": None,
        "frozen_hours_at_detection": GATE_HOURS,
    }


def _legacy_stuck_row() -> dict:
    """The pre-branch shape: the duration lives in ``z_score``, no new field."""
    row = _current_stuck_row()
    del row["frozen_hours_at_detection"]
    row["z_score"] = GATE_HOURS
    return row


def _legacy_statistical_row() -> dict:
    """A statistical row also has a numeric ``z_score`` — and always did.

    This is why every reader must discriminate on ``detector``, never on
    whether ``z_score`` is present: that test cannot tell this row apart from
    a legacy stuck row.
    """
    return {
        "timestamp": "2026-12-21T13:00:00+00:00",
        "detector": "statistical",
        "score": 0.8,
        "column": "measured_pipe_1_temp",
        "severity": "critical",
        "method": "rolling_z_score",
        "value": 51.0,
        "z_score": GATE_HOURS,
    }


# ---------------------------------------------------------------------------
# 13/14 — episode_grouper reads a duration from either shape, by detector
# ---------------------------------------------------------------------------

class TestEpisodeDurationDiscriminatesByDetector:
    """Spec 13 first half, and spec 14.

    ``_stuck_fault_duration`` is only reached under ``detector ==
    "stuck_fault"``, so reading a legacy ``z_score`` as hours is safe there
    and nowhere else. Both cases are driven through the public
    ``group_alerts_into_episodes`` with ``completed_faults=None`` so the
    fallback is the only available source of a duration.
    """

    @staticmethod
    def _sole_episode(row: dict):
        alert = UploadHighAnomalyDetail(**row)
        episodes = group_alerts_into_episodes([alert], completed_faults=None)
        assert len(episodes) == 1, f"Expected 1 episode, got {len(episodes)}"
        return episodes[0]

    def test_legacy_stuck_row_yields_the_gate_duration(self) -> None:
        episode = self._sole_episode(_legacy_stuck_row())
        assert episode.duration_seconds == GATE_HOURS * SECONDS_PER_HOUR
        assert episode.is_ongoing is True

    def test_current_stuck_row_yields_the_same_duration(self) -> None:
        """Both shapes must land on one number — that is the whole point."""
        assert (
            self._sole_episode(_current_stuck_row()).duration_seconds
            == self._sole_episode(_legacy_stuck_row()).duration_seconds
        )

    def test_a_statistical_z_score_is_never_read_as_a_duration(self) -> None:
        """Spec 14: the same numeric ``z_score``, but a statistical detector.

        Its episode duration comes from the span of its alert rows (one row →
        0.0 s), not from ``z_score``. If the fallback branched on ``z_score``
        being present instead of on the detector, this would report 6 h.
        """
        episode = self._sole_episode(_legacy_statistical_row())
        assert episode.detector == "statistical"
        assert episode.duration_seconds != GATE_HOURS * SECONDS_PER_HOUR


# ---------------------------------------------------------------------------
# 13 second half — the hours label in the jump table
# ---------------------------------------------------------------------------

class TestJumpTableRendersHoursNotASigma:
    """Spec 13 second half, and the step-2 display patch in monitoring_jump."""

    def test_current_stuck_row_shows_hours_and_no_z_score(self) -> None:
        cells = anomalies_to_display_frame([_current_stuck_row()]).to_dict("records")[0]
        assert cells["frozen_hours_at_detection"] == f"{GATE_HOURS:.1f} h"
        assert cells["z_score"] == "—"

    def test_the_frozen_column_is_offered_when_the_field_is_present(self) -> None:
        assert "frozen_hours_at_detection" in anomaly_display_columns(
            [_current_stuck_row()]
        )

    def test_the_frozen_column_is_absent_for_a_statistical_row(self) -> None:
        """The column is data-driven, so it must not appear where it is empty."""
        assert "frozen_hours_at_detection" not in anomaly_display_columns(
            [_legacy_statistical_row()]
        )

    @pytest.mark.parametrize(
        "value,expected",
        [
            (GATE_HOURS, "6.0 h"),
            (163.83, "163.8 h"),
            (0.0, "0.0 h"),
            (None, "—"),
            ("not a number", "—"),
        ],
    )
    def test_cell_formatting_always_carries_an_hours_unit(self, value, expected) -> None:
        """A bare number here would read as a z-score in a z-score-shaped table."""
        assert format_anomaly_cell("frozen_hours_at_detection", value) == expected

    def test_a_z_score_cell_is_still_formatted_without_a_unit(self) -> None:
        """Non-vacuity: the hours suffix must be specific to the new column."""
        assert format_anomaly_cell("z_score", GATE_HOURS) == "6.0"

    def test_legacy_stuck_row_shows_hours_and_no_z_score(self) -> None:
        """Both cells, not just one: the duration must move, not be duplicated.

        Asserting only that the frozen cell reads hours would pass a fix that
        surfaced the duration while leaving it under the Z-score header too —
        the same number under two different units, which is worse than the
        defect this replaced. Asserting the frozen cell also covers the
        column set implicitly: it cannot hold a value the frame has no column
        for.
        """
        cells = anomalies_to_display_frame([_legacy_stuck_row()]).to_dict("records")[0]
        assert cells["frozen_hours_at_detection"] == f"{GATE_HOURS:.1f} h"
        assert cells["z_score"] == "—"

    def test_legacy_and_current_shapes_render_one_identical_row(self) -> None:
        """Convergence: the stored shape must not be visible in the table.

        The complement to the pin above. That one would still pass if the two
        shapes rendered differently but both plausibly; this fails unless the
        legacy row is genuinely indistinguishable from a current-shape one,
        which is what breaks first if a display site reintroduces its own
        copy of the fallback rule.
        """
        legacy = anomalies_to_display_frame([_legacy_stuck_row()]).to_dict("records")[0]
        current = anomalies_to_display_frame([_current_stuck_row()]).to_dict("records")[0]
        assert legacy == current


# ---------------------------------------------------------------------------
# diagnosis_enricher — the stuck branch of _build_reasoning
# ---------------------------------------------------------------------------

class TestDiagnosisReasoningForStuckRows:
    """The step-2 patch at ``diagnosis_enricher._build_reasoning``'s stuck branch.

    Driven through ``cold_run_row_to_series``, the production converter, so
    the passthrough of the new field is covered at the same time — a series
    that dropped ``frozen_hours_at_detection`` would silently take the
    z-score branch.
    """

    @staticmethod
    def _reasoning(row: dict) -> str:
        return _build_reasoning(**REASONING_KWARGS, row=cold_run_row_to_series(row))

    def test_current_stuck_row_reads_as_frozen_hours(self) -> None:
        reasoning = self._reasoning(_current_stuck_row())
        assert "Frozen 6.0h" in reasoning
        assert "z-score" not in reasoning
        assert "σ" not in reasoning

    def test_the_series_carries_the_new_field_through(self) -> None:
        series = cold_run_row_to_series(_current_stuck_row())
        assert series["frozen_hours_at_detection"] == GATE_HOURS

    def test_a_statistical_row_still_reads_as_a_z_score(self) -> None:
        reasoning = self._reasoning(_legacy_statistical_row())
        assert "z-score 6.0σ" in reasoning
        assert "Frozen" not in reasoning

    def test_legacy_stuck_row_reads_as_frozen_hours(self) -> None:
        """No sigma on a duration — the unit is the whole point of this pin."""
        reasoning = self._reasoning(_legacy_stuck_row())
        assert "Frozen 6.0h" in reasoning
        assert "z-score" not in reasoning
        assert "σ" not in reasoning

    def test_legacy_and_current_shapes_read_identically(self) -> None:
        """Convergence: prose a user reads must not depend on the stored shape."""
        assert self._reasoning(_legacy_stuck_row()) == self._reasoning(
            _current_stuck_row()
        )

    def test_a_row_with_neither_field_produces_no_magnitude_clause(self) -> None:
        row = _current_stuck_row()
        del row["frozen_hours_at_detection"]
        row["z_score"] = None
        reasoning = self._reasoning(row)
        assert "Frozen" not in reasoning
        assert "z-score" not in reasoning
