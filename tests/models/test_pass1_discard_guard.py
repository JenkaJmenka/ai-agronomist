"""The Pass-1 discard gate: thresholds, denominators, and the raise (BACKLOG-14).

Before this module nothing in ``tests/`` referenced ``evaluate_pass1_discard``,
``Pass1DiscardStats``, ``ExcessivePass1DiscardError`` or ``DISCARD_STATUS_FAIL``
— only the ``ok`` branch was ever asserted, and that incidentally, via corpora
that happen to pass. Two independent reviews rated the gap blocking, and the
reason is that this module is the one component whose whole job is to *stop* a
bad upload: an untested stop is indistinguishable from no stop.

Three properties are pinned here.

**Inclusivity at both boundaries.** ``_status_for`` uses ``>=``, so 15.00%
warns and 14.99% does not; 25.00% fails and 24.99% does not. An off-by-one
either rejects an upload that should have trained or trains on one that should
have been rejected, and neither is visible from the ``ok`` path.

**The frame-vs-data denominator split**, which is the module's reason to exist.
``bootstrapped_clean_csvtest_20260712.csv`` showed a **44.6% cull of real
readings surfacing as 3.84% of a 91.4%-NaN frame** — the frame denominator
hides exactly the case the gate is for, so status is decided on the data
denominator and never on the frame one.
:class:`TestDenominatorSplit` reconstructs those proportions and asserts the
two denominators reach *opposite verdicts* on the same flags.

**The raise.** ``ColdStartBootstrap.run`` turns ``status == "fail"`` into
``ExcessivePass1DiscardError``, and that error's text becomes an HTTP 400 body,
so it is also asserted to be tenant-safe. The 400 mapping on both routes is in
``tests/api/test_pass1_discard_route_mapping.py``.

``evaluate_pass1_discard`` is pure and documented never to raise, so most of
this is arithmetic on constructed frames rather than pipeline runs.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import (
    BOOTSTRAP_CULL_FAIL_FRACTION,
    BOOTSTRAP_CULL_WARN_FRACTION,
)
from src.models import pass1_discard_guard
from src.models.bootstrap import ColdStartBootstrap, ExcessivePass1DiscardError
from src.models.pass1_discard_guard import (
    DISCARD_STATUS_FAIL,
    DISCARD_STATUS_OK,
    DISCARD_STATUS_WARN,
    Pass1DiscardStats,
    evaluate_pass1_discard,
)
from src.models.pass1_warning import (
    PASS1_WARNING_CODES,
    WARNING_CODE_DISCARD_EXCESSIVE,
    WARNING_CODE_DISCARD_HIGH,
)

SENSOR_COLUMNS = ["temperature", "humidity", "co2", "light"]

# A round denominator so a discard count reads directly as a percentage to two
# decimal places: 1499/10000 is exactly 0.1499, which is what lets the boundary
# cases below be stated exactly rather than approached.
BOUNDARY_ROWS = 10_000

# Reconstruction of bootstrapped_clean_csvtest_20260712.csv's proportions.
# 1000 rows carry data, 446 of them are discarded (44.6%), and the frame is
# 91.4% empty, so 446/11628 reads as 3.84% against the frame denominator.
INCIDENT_DATA_ROWS = 1_000
INCIDENT_DATA_DISCARDED = 446
INCIDENT_FRAME_ROWS = 11_628

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
KNIFE_EDGE_FIXTURE = _FIXTURE_DIR / "pass1_light_knife_edge_high_modal.csv"

# Strings that must never appear in a message bound for a tenant or an HTTP
# body. The remediation naming these moved to the log in the review fixes.
INTERNAL_IDENTIFIERS = (
    "BOOTSTRAP_",
    "MAD",
    "IsolationForest",
    "mad_flags",
    "Pass1DiscardStats",
    "_SENSOR_COLS",
    "data_discard_fraction",
)


def _frame_with_data_rows(frame_rows: int, data_rows: int) -> pd.DataFrame:
    """A frame of *frame_rows* where only the first *data_rows* carry readings.

    The remainder are all-NaN across every sensor column, which is what
    ``evaluate_pass1_discard`` uses to separate the two denominators: a row
    counts as carrying data when *any* sensor column is finite.
    """
    values = np.full((frame_rows, len(SENSOR_COLUMNS)), np.nan, dtype=float)
    values[:data_rows, :] = 1.0
    return pd.DataFrame(values, columns=SENSOR_COLUMNS)


def _flags_discarding(frame_rows: int, discarded: int) -> pd.Series:
    """Flags marking the first *discarded* rows of *frame_rows* as discarded."""
    mask = np.zeros(frame_rows, dtype=bool)
    mask[:discarded] = True
    return pd.Series(mask)


def _evaluate(*, frame_rows: int, data_rows: int, discarded: int) -> Pass1DiscardStats:
    return evaluate_pass1_discard(
        _frame_with_data_rows(frame_rows, data_rows),
        _flags_discarding(frame_rows, discarded),
        SENSOR_COLUMNS,
    )


class TestThresholdInclusivity:
    """``>=`` at both boundaries — the thresholds include their own value."""

    def test_default_limits_are_the_documented_ones(self) -> None:
        """Guards the arithmetic below: these tests assume 15% and 25%."""
        assert BOOTSTRAP_CULL_WARN_FRACTION == 0.15
        assert BOOTSTRAP_CULL_FAIL_FRACTION == 0.25

    def test_just_below_the_warn_limit_is_ok(self) -> None:
        stats = _evaluate(frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=1_499)
        assert stats.data_discard_fraction == 0.1499
        assert stats.status == DISCARD_STATUS_OK

    def test_exactly_at_the_warn_limit_warns(self) -> None:
        stats = _evaluate(frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=1_500)
        assert stats.data_discard_fraction == BOOTSTRAP_CULL_WARN_FRACTION
        assert stats.status == DISCARD_STATUS_WARN

    def test_just_below_the_fail_limit_only_warns(self) -> None:
        stats = _evaluate(frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=2_499)
        assert stats.data_discard_fraction == 0.2499
        assert stats.status == DISCARD_STATUS_WARN

    def test_exactly_at_the_fail_limit_fails(self) -> None:
        stats = _evaluate(frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=2_500)
        assert stats.data_discard_fraction == BOOTSTRAP_CULL_FAIL_FRACTION
        assert stats.status == DISCARD_STATUS_FAIL

    def test_ok_status_carries_no_warning(self) -> None:
        stats = _evaluate(frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=1_499)
        assert stats.warnings == ()

    def test_warn_status_carries_exactly_one_warning(self) -> None:
        stats = _evaluate(frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=1_500)
        assert [warning.code for warning in stats.warnings] == [
            WARNING_CODE_DISCARD_HIGH
        ]

    def test_fail_status_carries_the_excessive_code(self) -> None:
        stats = _evaluate(frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=2_500)
        assert [warning.code for warning in stats.warnings] == [
            WARNING_CODE_DISCARD_EXCESSIVE
        ]

    def test_is_failing_agrees_with_status(self) -> None:
        failing = _evaluate(
            frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=2_500
        )
        warning = _evaluate(
            frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=1_500
        )
        assert failing.is_failing is True
        assert warning.is_failing is False


class TestDenominatorSplit:
    """Status is decided on rows carrying data, never on rows in the frame.

    This is the property the module was written for. Asserting it needs a case
    where the two denominators *disagree about the verdict* — a case where they
    merely differ numerically would pass even if the code read the wrong one.
    """

    @pytest.fixture
    def incident_stats(self) -> Pass1DiscardStats:
        return _evaluate(
            frame_rows=INCIDENT_FRAME_ROWS,
            data_rows=INCIDENT_DATA_ROWS,
            discarded=INCIDENT_DATA_DISCARDED,
        )

    def test_data_denominator_reproduces_the_44_6_percent_cull(
        self, incident_stats: Pass1DiscardStats
    ) -> None:
        assert incident_stats.data_rows == INCIDENT_DATA_ROWS
        assert incident_stats.data_discarded == INCIDENT_DATA_DISCARDED
        assert incident_stats.data_discard_fraction == pytest.approx(0.446)

    def test_frame_denominator_reproduces_the_3_84_percent_illusion(
        self, incident_stats: Pass1DiscardStats
    ) -> None:
        """The same flags against the frame read as 3.84% — the hidden number."""
        assert incident_stats.frame_rows == INCIDENT_FRAME_ROWS
        assert incident_stats.frame_discard_fraction == pytest.approx(0.0384, abs=5e-5)

    def test_the_two_denominators_reach_opposite_verdicts(
        self, incident_stats: Pass1DiscardStats
    ) -> None:
        """Data says fail; frame would have said ok. The gate must pick data."""
        assert incident_stats.data_discard_fraction >= BOOTSTRAP_CULL_FAIL_FRACTION
        assert incident_stats.frame_discard_fraction < BOOTSTRAP_CULL_WARN_FRACTION
        assert incident_stats.status == DISCARD_STATUS_FAIL

    def test_summary_carries_both_numerators_and_both_denominators(
        self, incident_stats: Pass1DiscardStats
    ) -> None:
        """So an operator reading one line cannot confuse the two again."""
        summary = incident_stats.summary()
        for number in (
            INCIDENT_DATA_DISCARDED,
            INCIDENT_DATA_ROWS,
            INCIDENT_FRAME_ROWS,
        ):
            assert str(number) in summary

    def test_warning_quotes_the_data_denominator_not_the_frame(
        self, incident_stats: Pass1DiscardStats
    ) -> None:
        """A tenant must be told 44.6%, not the 3.84% that hid the problem."""
        message = incident_stats.warnings[0].message
        assert "44.6%" in message
        assert "3.8%" not in message


class TestDegenerateInputs:
    """``evaluate_pass1_discard`` is documented never to raise."""

    def test_empty_frame_is_ok_and_not_a_division_by_zero(self) -> None:
        stats = evaluate_pass1_discard(
            pd.DataFrame(columns=SENSOR_COLUMNS),
            pd.Series([], dtype=bool),
            SENSOR_COLUMNS,
        )
        assert stats.frame_rows == 0
        assert stats.data_discard_fraction == 0.0
        assert stats.status == DISCARD_STATUS_OK

    def test_all_nan_frame_has_no_data_rows_and_stays_ok(self) -> None:
        """Nothing was discarded *of the rows that mattered*, because there were none."""
        stats = _evaluate(frame_rows=500, data_rows=0, discarded=500)
        assert stats.data_rows == 0
        assert stats.data_discard_fraction == 0.0
        assert stats.status == DISCARD_STATUS_OK
        assert stats.frame_discard_fraction == 1.0

    def test_absent_sensor_columns_mean_no_rows_carry_data(self) -> None:
        frame = pd.DataFrame({"unrelated": [1.0, 2.0, 3.0]})
        stats = evaluate_pass1_discard(
            frame, pd.Series([True, True, True]), SENSOR_COLUMNS
        )
        assert stats.data_rows == 0
        assert stats.status == DISCARD_STATUS_OK

    def test_total_discard_of_real_rows_fails(self) -> None:
        stats = _evaluate(frame_rows=500, data_rows=500, discarded=500)
        assert stats.data_discard_fraction == 1.0
        assert stats.status == DISCARD_STATUS_FAIL


class TestRunRaisesOnFail:
    """``ColdStartBootstrap.run`` stops rather than returning a failing result.

    Driven by lowering the fail limit against a real fixture rather than by
    mocking the guard, so the raise is reached through the real Pass-1 path:
    load, physics, MAD scrub, IsolationForest, persist, then the gate.
    """

    @pytest.fixture
    def failing_limit(self, monkeypatch: pytest.MonkeyPatch) -> float:
        """Lower the fail limit under the fixture's real ~4.0% discard.

        Patched on ``pass1_discard_guard`` rather than on ``src.config``
        because ``_status_for`` resolves the name from this module's globals at
        call time; patching the config module would not be seen.
        """
        limit = 0.02
        monkeypatch.setattr(
            pass1_discard_guard, "BOOTSTRAP_CULL_FAIL_FRACTION", limit
        )
        return limit

    def test_run_raises_excessive_discard(
        self, failing_limit: float, tmp_path: Path
    ) -> None:
        bootstrap = ColdStartBootstrap(
            clean_path=str(tmp_path / "clean.csv"),
            anomaly_path=str(tmp_path / "anomaly.csv"),
        )
        with pytest.raises(ExcessivePass1DiscardError):
            bootstrap.run(str(KNIFE_EDGE_FIXTURE))

    def test_the_error_is_a_value_error_so_routes_map_it_to_400(self) -> None:
        """No new route handler was needed; this is why.

        ``src/api/routes/bootstrap.py`` catches ``InsufficientDataError``,
        which *is* ``ValueError``, so a ``ValueError`` subclass is already
        mapped to HTTP 400 at both call sites.
        """
        assert issubclass(ExcessivePass1DiscardError, ValueError)

    def test_the_error_message_is_tenant_safe(
        self, failing_limit: float, tmp_path: Path
    ) -> None:
        """``str(exc)`` becomes the HTTP 400 body, so it must leak nothing.

        The operator remediation that used to live here — naming the
        environment variables and printing the mad/median pair — moved to the
        log in the review fixes. This asserts it did not come back.
        """
        bootstrap = ColdStartBootstrap(
            clean_path=str(tmp_path / "clean.csv"),
            anomaly_path=str(tmp_path / "anomaly.csv"),
        )
        with pytest.raises(ExcessivePass1DiscardError) as exc_info:
            bootstrap.run(str(KNIFE_EDGE_FIXTURE))

        detail = str(exc_info.value)
        assert detail, "the 400 body must not be empty"
        for identifier in INTERNAL_IDENTIFIERS:
            assert identifier not in detail, f"{identifier!r} leaked to the tenant"

    def test_the_csvs_are_persisted_before_the_raise(
        self, failing_limit: float, tmp_path: Path
    ) -> None:
        """Diagnostics survive the stop — that ordering is deliberate."""
        clean_path = tmp_path / "clean.csv"
        anomaly_path = tmp_path / "anomaly.csv"
        bootstrap = ColdStartBootstrap(
            clean_path=str(clean_path), anomaly_path=str(anomaly_path)
        )
        with pytest.raises(ExcessivePass1DiscardError):
            bootstrap.run(str(KNIFE_EDGE_FIXTURE))

        assert clean_path.is_file()
        assert anomaly_path.is_file()

    def test_a_passing_limit_returns_a_result_instead(self, tmp_path: Path) -> None:
        """Negative control: the raise is caused by the limit, not the fixture.

        Without this the three tests above would also pass if ``run`` had
        simply started raising unconditionally.
        """
        bootstrap = ColdStartBootstrap(
            clean_path=str(tmp_path / "clean.csv"),
            anomaly_path=str(tmp_path / "anomaly.csv"),
        )
        result = bootstrap.run(str(KNIFE_EDGE_FIXTURE))
        assert result.discard_stats.status == DISCARD_STATUS_OK


class TestWarnStatusStillReturns:
    """``status == "warn"`` is a successful run that carries a warning."""

    @pytest.fixture
    def warning_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Warn under the fixture's ~4.0% discard without reaching fail."""
        monkeypatch.setattr(
            pass1_discard_guard, "BOOTSTRAP_CULL_WARN_FRACTION", 0.02
        )
        monkeypatch.setattr(
            pass1_discard_guard, "BOOTSTRAP_CULL_FAIL_FRACTION", 0.90
        )

    def test_warn_returns_a_result_carrying_the_warning(
        self, warning_limit: None, tmp_path: Path
    ) -> None:
        """Phase 2's contract: a non-empty ``warnings`` is not a failure."""
        bootstrap = ColdStartBootstrap(
            clean_path=str(tmp_path / "clean.csv"),
            anomaly_path=str(tmp_path / "anomaly.csv"),
        )
        result = bootstrap.run(str(KNIFE_EDGE_FIXTURE))

        assert result.discard_stats.status == DISCARD_STATUS_WARN
        assert [warning.code for warning in result.warnings] == [
            WARNING_CODE_DISCARD_HIGH
        ]
        assert len(result.clean_df) > 0


class TestStatsInvariants:
    """Properties that hold for any input, checked across the boundary cases."""

    @pytest.mark.parametrize("discarded", [0, 1_499, 1_500, 2_499, 2_500, BOUNDARY_ROWS])
    def test_fractions_are_fractions_never_percentages(self, discarded: int) -> None:
        """Every fraction on this type is in [0, 1]; the module says so twice."""
        stats = _evaluate(
            frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=discarded
        )
        for fraction in (stats.frame_discard_fraction, stats.data_discard_fraction):
            assert 0.0 <= fraction <= 1.0
            assert math.isfinite(fraction)

    @pytest.mark.parametrize("discarded", [0, 1_500, 2_500])
    def test_every_warning_code_is_in_the_closed_set(self, discarded: int) -> None:
        stats = _evaluate(
            frame_rows=BOUNDARY_ROWS, data_rows=BOUNDARY_ROWS, discarded=discarded
        )
        for warning in stats.warnings:
            assert warning.code in PASS1_WARNING_CODES

    def test_default_stats_are_an_empty_ok(self) -> None:
        """``Pass1DiscardStats()`` is the default on ``BootstrapPass1Result``."""
        stats = Pass1DiscardStats()
        assert stats.status == DISCARD_STATUS_OK
        assert stats.is_failing is False
        assert stats.warnings == ()
