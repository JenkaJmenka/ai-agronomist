"""Which guard claims a skipped channel, and why the order matters (BACKLOG-14).

``_skip_reason`` tests three conditions in a fixed order: the exclusion list,
then zero spread, then modal share. The middle two were **swapped** in the
review fixes, and the swap is invisible to any test that only checks whether a
channel was skipped — the cull counts are identical before and after. Only the
reported reason changes. I reported that gap last round: nothing in ``tests/``
asserted ``co2``'s skip reason, so the swap could have been reverted silently.
This module closes it.

**Why the order is load-bearing.** ``mad == 0`` implies at least half the
values sit exactly at the median, so the modal share is necessarily at or above
about 0.5 — always past the 0.40 limit. With modal share tested first,
``SKIP_REASON_NO_SPREAD`` was therefore **unreachable in production**, and a
constant channel was reported as ``modal_share_above_limit``, i.e. *bimodal*,
which is false and was being persisted per model. ``co2`` is the live case: the
CSV adapter fills an absent column with a constant 400.0.

The sharpest case is ``humidity`` on ``native_dec_stuck_fault.csv``, where
``mad == 0.0`` **and** modal share is 0.9362. Both conditions hold
emphatically, so the reported reason is decided by precedence and by nothing
else — which is exactly what makes it a test of the swap rather than of the
data.

A note on scope: asserting the MAD scrub's skip *reason* on a stuck-sensor
fixture is a statement about guard precedence, not about stuck-sensor
detection. BACKLOG-21's flatline rule is deliberately untouched here and no
assertion below pins current stuck-sensor behaviour.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import BOOTSTRAP_MAD_MAX_MODAL_SHARE, BOOTSTRAP_MAD_MIN_SPREAD
from src.models.bootstrap import _SENSOR_COLS, _load_and_resample
from src.models.mad_scrub import compute_mad_scrub, modal_share
from src.models.mad_scrub_report import (
    SKIP_REASON_EXCLUDED,
    SKIP_REASON_MISSING,
    SKIP_REASON_MODAL_SHARE,
    SKIP_REASON_NO_FINITE,
    SKIP_REASON_NO_SPREAD,
    MadScrubOptions,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"

JULY_CLEARSKY = _FIXTURE_DIR / "native_july_clearsky.csv"
DEC_STUCK_FAULT = _FIXTURE_DIR / "native_dec_stuck_fault.csv"
KNIFE_EDGE_HIGH_MODAL = _FIXTURE_DIR / "pass1_light_knife_edge_high_modal.csv"

# Measured on these committed fixtures. All four changed cells are channels
# whose MAD is exactly 0.0; every one reported `modal_share_above_limit` before
# the swap and `mad_at_or_below_min_spread` after it. Cull counts are identical
# across the swap, which is why the reason is the only thing worth asserting.
EXPECTED_SKIPS = {
    JULY_CLEARSKY: {
        "co2": SKIP_REASON_NO_SPREAD,
        "light": SKIP_REASON_EXCLUDED,
    },
    DEC_STUCK_FAULT: {
        "humidity": SKIP_REASON_NO_SPREAD,
        "co2": SKIP_REASON_NO_SPREAD,
        "light": SKIP_REASON_EXCLUDED,
    },
}

# Cull counts, unchanged by the swap. The temperature=15 on the December
# fixture is the one real cull in this set, and it is what distinguishes the
# swap from a change that quietly disabled a channel.
EXPECTED_CULLS = {
    JULY_CLEARSKY: {"temperature": 0, "humidity": 0, "co2": 0, "light": 0},
    DEC_STUCK_FAULT: {"temperature": 15, "humidity": 0, "co2": 0, "light": 0},
}

_FIXTURE_IDS = {JULY_CLEARSKY: "july_clearsky", DEC_STUCK_FAULT: "dec_stuck_fault"}
_CORPORA = [pytest.param(path, id=name) for path, name in _FIXTURE_IDS.items()]


def _scrub(path: Path):
    return compute_mad_scrub(_load_and_resample(str(path)), _SENSOR_COLS)


def _report_for(result, column: str):
    return next(report for report in result.channels if report.column == column)


class TestZeroSpreadIsReportedTruthfully:
    """A constant channel is reported as having no spread, not as bimodal."""

    @pytest.mark.parametrize("fixture_path", _CORPORA)
    def test_skipped_channels_match_the_measured_reasons(
        self, fixture_path: Path
    ) -> None:
        assert _scrub(fixture_path).skipped_channels == EXPECTED_SKIPS[fixture_path]

    @pytest.mark.parametrize("fixture_path", _CORPORA)
    def test_every_no_spread_channel_really_has_zero_mad(
        self, fixture_path: Path
    ) -> None:
        """The reported reason is true of the data, not merely consistent.

        Without this the suite would accept ``mad_at_or_below_min_spread`` on a
        channel that has plenty of spread — the mirror image of the bug.
        """
        result = _scrub(fixture_path)
        for column, reason in EXPECTED_SKIPS[fixture_path].items():
            if reason != SKIP_REASON_NO_SPREAD:
                continue
            report = _report_for(result, column)
            assert report.mad <= BOOTSTRAP_MAD_MIN_SPREAD
            assert report.mad == 0.0

    @pytest.mark.parametrize("fixture_path", _CORPORA)
    def test_cull_counts_are_unchanged_by_the_precedence_swap(
        self, fixture_path: Path
    ) -> None:
        """Only the reason moved. If a count moved, the swap did more than claimed."""
        assert _scrub(fixture_path).cull_counts == EXPECTED_CULLS[fixture_path]

    def test_the_decisive_case_trips_both_conditions_at_once(self) -> None:
        """``humidity`` on the December fixture: mad == 0 AND modal share 0.936.

        Both guards' conditions hold, so precedence alone decides the reason.
        This is the single test that fails if the swap is reverted, and it
        cannot pass for an incidental reason.
        """
        report = _report_for(_scrub(DEC_STUCK_FAULT), "humidity")

        assert report.mad == 0.0
        assert report.modal_share > BOOTSTRAP_MAD_MAX_MODAL_SHARE
        assert report.modal_share == pytest.approx(0.93623, abs=1e-5)
        assert report.skip_reason == SKIP_REASON_NO_SPREAD

    @pytest.mark.parametrize("fixture_path", _CORPORA)
    def test_light_is_still_claimed_by_the_exclusion_list(
        self, fixture_path: Path
    ) -> None:
        """The exclusion list is tested first and returns before measuring.

        So ``light``'s median/mad/modal_share stay NaN, and the swap of the two
        later conditions cannot reach it.
        """
        report = _report_for(_scrub(fixture_path), "light")

        assert report.skip_reason == SKIP_REASON_EXCLUDED
        assert np.isnan(report.median)
        assert np.isnan(report.mad)
        assert np.isnan(report.modal_share)


class TestModalShareIsStillReachable:
    """``modal_share_above_limit`` now means genuinely multi-modal *with spread*.

    The swap narrowed what this reason can mean; it did not retire it. A
    channel that is bimodal with real spread must still be caught, or the
    BACKLOG-14 defect comes back for any channel not on the exclusion list.
    """

    def test_a_bimodal_channel_with_real_spread_is_caught(self) -> None:
        """45% zeros against a bright mode: modal share 0.45, MAD far from zero."""
        rows = 1_000
        zeros = 450
        light = np.concatenate(
            [np.zeros(zeros), np.linspace(600.0, 900.0, rows - zeros)]
        )
        frame = pd.DataFrame({"light": light})

        result = compute_mad_scrub(
            frame, ["light"], options=MadScrubOptions(excluded_columns=frozenset())
        )
        report = _report_for(result, "light")

        assert report.modal_share == pytest.approx(zeros / rows)
        assert report.mad > 1.0, "this case must not be a zero-spread case"
        assert report.skip_reason == SKIP_REASON_MODAL_SHARE
        assert report.cull_rows == 0

    def test_the_knife_edge_light_channel_is_not_a_zero_spread_case(self) -> None:
        """Keeps the two guards distinguishable on the fixture they share.

        On the knife-edge fixture ``mad == median == 1.35``, which is non-zero,
        so with the exclusion list removed this channel must be claimed by
        modal share — not by the spread guard that now runs ahead of it.
        """
        frame = _load_and_resample(str(KNIFE_EDGE_HIGH_MODAL))
        result = compute_mad_scrub(
            frame, _SENSOR_COLS, options=MadScrubOptions(excluded_columns=frozenset())
        )
        report = _report_for(result, "light")

        assert report.mad == pytest.approx(1.35)
        assert report.mad > BOOTSTRAP_MAD_MIN_SPREAD
        assert report.skip_reason == SKIP_REASON_MODAL_SHARE


class TestRemainingSkipReasons:
    """The other two members of the closed set, neither reachable from a corpus."""

    def test_an_all_nan_channel_reports_no_finite_values(self) -> None:
        frame = pd.DataFrame({"co2": [np.nan, np.nan, np.nan]})
        report = _report_for(compute_mad_scrub(frame, ["co2"]), "co2")

        assert report.skip_reason == SKIP_REASON_NO_FINITE
        assert report.finite_rows == 0
        assert report.cull_fraction == 0.0

    def test_a_requested_but_absent_column_reports_column_absent(self) -> None:
        """Reported as skipped rather than silently dropped from the report."""
        frame = pd.DataFrame({"temperature": [20.0, 21.0, 22.0]})
        result = compute_mad_scrub(frame, ["temperature", "co2"])
        report = _report_for(result, "co2")

        assert report.skip_reason == SKIP_REASON_MISSING
        assert report.is_skipped is True
        assert report.finite_rows == 0

    def test_an_excluded_column_is_claimed_before_it_is_measured(self) -> None:
        """Ordering check on the first condition, independent of any fixture."""
        frame = pd.DataFrame({"light": [0.0, 0.0, 500.0, 900.0]})
        report = _report_for(
            compute_mad_scrub(
                frame,
                ["light"],
                options=MadScrubOptions(excluded_columns=frozenset({"light"})),
            ),
            "light",
        )

        assert report.skip_reason == SKIP_REASON_EXCLUDED
        assert np.isnan(report.modal_share), "measured despite being excluded"

    def test_zero_spread_beats_modal_share_on_a_constant_channel(self) -> None:
        """The swap stated as a unit fact, with no corpus involved.

        A constant channel has modal share 1.0 and MAD 0.0. Both conditions
        hold; the spread one must win.
        """
        frame = pd.DataFrame({"co2": [400.0] * 50})
        report = _report_for(compute_mad_scrub(frame, ["co2"]), "co2")

        assert modal_share(frame["co2"].to_numpy(dtype=float)) == 1.0
        assert report.mad == 0.0
        assert report.skip_reason == SKIP_REASON_NO_SPREAD
