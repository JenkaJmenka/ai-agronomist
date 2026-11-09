"""Regression tests for the Pass-1 bimodal-light MAD cull (BACKLOG-14).

``compute_mad_flags`` took one whole-file median/MAD per channel, which assumes
a unimodal channel. ``light`` is structurally bimodal. When the dark rows sit
just **under** 50% of the window, every zero deviates from the median by exactly
the median, so the median of those deviations *is* the median — ``MAD ==
median`` identically — and the robust-Z cull boundary collapses to::

    cut = median * (1 + 3.5 / 0.6745) = 6.18903 * median

On the tenant_1 corpus that was ``6.18903 * 1.35 = 8.3552 W/m^2``, culling
49,741 rows (47.41%) for the crime of being daylight and discarding 50.52% of
the upload. The fix drops ``light`` from the scrubbed channel list
(``BOOTSTRAP_MAD_EXCLUDED_CHANNELS``) and adds a generic skip for any channel
whose modal share exceeds ``BOOTSTRAP_MAD_MAX_MODAL_SHARE`` or whose MAD is at
or below ``BOOTSTRAP_MAD_MIN_SPREAD``.

**The discontinuity is what these tests are built around.** *Above* 50% dark the
MAD is 0, the guard in ``mad_z_scores`` short-circuits and the channel culls
nothing all by itself; *just below* 50% it culls the entire other mode. A
fixture on the wrong side of that line proves nothing and passes for the wrong
reason, so :func:`test_fixture_sits_on_the_knife_edge` asserts the degenerate
statistic directly on the raw data, with no pipeline involved. That is the test
that fails loudest if whole-file MAD is ever reintroduced on ``light``.

Both fixtures are synthetic and committed; see
``tests/fixtures/make_pass1_light_knife_edge.py`` for the construction and for
why a downsample of the real customer CSV was not used. The real-corpus figures
are pinned separately and opt-in by
``tests/models/test_pass1_tenant_corpus.py``.

Why two fixtures: **neither guard subsumes the other.**

* ``high_modal`` — 43.9% of light rows at exactly 0, mirroring tenant_1. Modal
  share is above the 0.40 limit, so the exclusion list *and* the generic rule
  each disqualify ``light`` independently.
* ``low_modal`` — 34.9% at exactly 0, mirroring ``native_july_clearsky.csv``.
  Modal share is below the limit, so **only** the exclusion list disqualifies
  it. A suite with the ``high_modal`` case alone would stay green if someone
  emptied ``BOOTSTRAP_MAD_EXCLUDED_CHANNELS``.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import (
    BOOTSTRAP_MAD_EXCLUDED_CHANNELS,
    BOOTSTRAP_MAD_MAX_MODAL_SHARE,
    BOOTSTRAP_MAD_Z_THRESHOLD,
)
from src.models.bootstrap import ColdStartBootstrap, _load_and_resample, _SENSOR_COLS
from src.models.mad_scrub import compute_mad_scrub, modal_share
from src.models.mad_scrub_report import (
    SKIP_REASON_EXCLUDED,
    SKIP_REASON_MODAL_SHARE,
    MadScrubOptions,
)
from src.models.pass1_discard_guard import DISCARD_STATUS_OK
from src.models.pass1_warning import WARNING_CODE_CHANNEL_CULL_HIGH

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"

HIGH_MODAL_FIXTURE = _FIXTURE_DIR / "pass1_light_knife_edge_high_modal.csv"
LOW_MODAL_FIXTURE = _FIXTURE_DIR / "pass1_light_knife_edge_low_modal.csv"

# Properties of both fixtures, stated here rather than imported from the
# generator on purpose: a regression test that reads its expectations out of the
# code that produced the data cannot notice the data changing.
FIXTURE_ROWS = 401
LIGHT_FLOOR = 1.35                  # == median(light) == MAD(light)
EXPECTED_BRIGHT_ROWS = 200          # the mode the pre-fix boundary culled wholesale
EXPECTED_HUMIDITY_CULLS = 12        # the deliberate wet-sensor excursion
HIGH_MODAL_DARK_SHARE = 176 / FIXTURE_ROWS   # 0.43890 — above the 0.40 limit
LOW_MODAL_DARK_SHARE = 140 / FIXTURE_ROWS    # 0.34913 — below it

# IsolationForest still spends its ~4% contamination budget, so "the cull is
# gone" is an upper bound a little above 4%, not zero. Loose enough to absorb a
# sklearn version nudge, tight enough that the 49.9% pre-fix cull — or even a
# tenth of it — fails the assertion.
MAX_DISCARD_FRACTION = 0.10

_CASES = (
    pytest.param(HIGH_MODAL_FIXTURE, HIGH_MODAL_DARK_SHARE, id="high_modal"),
    pytest.param(LOW_MODAL_FIXTURE, LOW_MODAL_DARK_SHARE, id="low_modal"),
)


def _frame(path: Path) -> pd.DataFrame:
    """The Pass-1 frame exactly as ``ColdStartBootstrap.run`` builds it."""
    return _load_and_resample(str(path))


def _run_pass1(path: Path, tmp_path: Path):
    """Run the real Pass 1, writing its CSVs under *tmp_path* and never ``data/``."""
    bootstrap = ColdStartBootstrap(
        clean_path=str(tmp_path / "clean.csv"),
        anomaly_path=str(tmp_path / "anomaly.csv"),
    )
    return bootstrap.run(str(path))


def _report_for(result, column: str):
    return next(report for report in result.mad_channels if report.column == column)


# ---------------------------------------------------------------------------
# The degenerate statistic itself — no pipeline, no mocks, just the data.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_fixture_sits_on_the_knife_edge(fixture_path: Path, dark_share: float) -> None:
    """median(light) == MAD(light) > 0 with at least half the rows at or below it.

    All three conditions together are the knife edge. ``MAD == median`` is the
    degeneracy; ``median > 0`` is what places the fixture *below* the 50%-dark
    line rather than above it, where the MAD would be 0 and the channel would
    cull nothing for an entirely different reason.
    """
    light = _frame(fixture_path)["light"].to_numpy(dtype=float)
    median = float(np.median(light))
    mad = float(np.median(np.abs(light - median)))

    assert median > 0.0, "median(light) == 0 means the fixture is above 50% dark"
    assert mad == median
    assert float((light <= median).mean()) >= 0.5
    assert dark_share < 0.5
    assert math.isclose(median, LIGHT_FLOOR)


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_collapsed_cull_boundary_would_take_the_whole_bright_mode(
    fixture_path: Path, dark_share: float
) -> None:
    """The boundary the pre-fix code computed culls every daylight row.

    This is the bug stated as arithmetic rather than as behaviour, so it stays
    legible even if the scrub is rewritten: with ``MAD == median`` the cutoff is
    ``6.18903 * median``, which on this fixture is 8.3552 W/m^2 and sits above
    the floor mode but below every bright reading.
    """
    light = _frame(fixture_path)["light"].to_numpy(dtype=float)
    median = float(np.median(light))
    collapsed_cut = median * (1.0 + BOOTSTRAP_MAD_Z_THRESHOLD / 0.6745)

    assert math.isclose(collapsed_cut, 8.35519, abs_tol=1e-5)
    assert int((light > collapsed_cut).sum()) == EXPECTED_BRIGHT_ROWS
    assert float((light > collapsed_cut).mean()) > 0.45


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_modal_share_places_each_fixture_on_its_intended_side(
    fixture_path: Path, dark_share: float
) -> None:
    """The two fixtures straddle ``BOOTSTRAP_MAD_MAX_MODAL_SHARE`` as designed.

    Without this the pair could silently become two copies of the same case,
    and the asymmetry the suite relies on would stop being tested.
    """
    light = _frame(fixture_path)["light"].to_numpy(dtype=float)
    assert math.isclose(modal_share(light), dark_share)

    if fixture_path == HIGH_MODAL_FIXTURE:
        assert modal_share(light) > BOOTSTRAP_MAD_MAX_MODAL_SHARE
    else:
        assert modal_share(light) < BOOTSTRAP_MAD_MAX_MODAL_SHARE


# ---------------------------------------------------------------------------
# The fix, through the real ColdStartBootstrap.run().
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_light_is_skipped_and_culls_nothing(
    fixture_path: Path, dark_share: float, tmp_path: Path
) -> None:
    """``light`` comes back skipped as an excluded channel, having culled no rows.

    The reason is ``SKIP_REASON_EXCLUDED`` for *both* fixtures, including the
    high-modal one whose share also breaches the generic limit, because
    ``_skip_reason`` tests the exclusion list first. Which guard would catch it
    in the other's absence is asserted separately below.
    """
    result = _run_pass1(fixture_path, tmp_path)
    light = _report_for(result, "light")

    assert light.is_skipped is True
    assert light.skip_reason == SKIP_REASON_EXCLUDED
    assert light.cull_rows == 0
    assert light.cull_fraction == 0.0
    assert result.mad_skipped_channels == {"light": SKIP_REASON_EXCLUDED}
    assert result.mad_channel_cull_rows["light"] == 0


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_overall_discard_stays_low(
    fixture_path: Path, dark_share: float, tmp_path: Path
) -> None:
    """Pass 1 keeps the corpus: well under 10% discarded, and the guard says ok."""
    result = _run_pass1(fixture_path, tmp_path)
    stats = result.discard_stats

    assert stats.frame_rows == FIXTURE_ROWS
    assert stats.data_rows == FIXTURE_ROWS
    assert stats.data_discard_fraction < MAX_DISCARD_FRACTION
    assert stats.status == DISCARD_STATUS_OK
    assert len(result.clean_df) + len(result.anomaly_df) == FIXTURE_ROWS


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_the_bright_mode_survives_pass_one(
    fixture_path: Path, dark_share: float, tmp_path: Path
) -> None:
    """Most daylight rows reach the clean set.

    The pre-fix code sent *every* bright row to the anomaly set — hour-12
    survival on tenant_1 was 0.00%. This is the fixture-scale statement of the
    same property, and the residual loss here is IsolationForest's, not MAD's.
    """
    result = _run_pass1(fixture_path, tmp_path)
    collapsed_cut = LIGHT_FLOOR * (1.0 + BOOTSTRAP_MAD_Z_THRESHOLD / 0.6745)

    bright_clean = int((result.clean_df["light"] > collapsed_cut).sum())
    assert bright_clean > 0.80 * EXPECTED_BRIGHT_ROWS


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_mad_flags_on_bright_rows_are_humiditys_alone(
    fixture_path: Path, dark_share: float, tmp_path: Path
) -> None:
    """Every MAD flag landing on a daylight row is attributable to humidity.

    The wet-sensor excursion sits at midday by construction, so a handful of
    MAD flags do fall on bright rows — but exactly that handful. A regression
    that restored whole-file MAD on ``light`` would push this to all 200.
    """
    result = _run_pass1(fixture_path, tmp_path)
    collapsed_cut = LIGHT_FLOOR * (1.0 + BOOTSTRAP_MAD_Z_THRESHOLD / 0.6745)
    is_bright = _frame(fixture_path)["light"] > collapsed_cut

    assert int((result.mad_flags & is_bright).sum()) == EXPECTED_HUMIDITY_CULLS


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_humidity_keeps_its_legitimate_culls(
    fixture_path: Path, dark_share: float, tmp_path: Path
) -> None:
    """The fix must not have become "skip every channel".

    ``humidity`` carries a deliberate wet-sensor excursion at a modal share far
    below the limit and a MAD far above the minimum spread, so it is genuinely
    scrubbed and genuinely culls those rows. On the real corpus the equivalent
    assertion is humidity's 1,426 culls at a modal share of 0.07999.
    """
    result = _run_pass1(fixture_path, tmp_path)
    humidity = _report_for(result, "humidity")

    assert humidity.is_skipped is False
    assert humidity.skip_reason is None
    assert humidity.cull_rows == EXPECTED_HUMIDITY_CULLS
    assert humidity.modal_share < BOOTSTRAP_MAD_MAX_MODAL_SHARE


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_the_other_unimodal_channels_are_still_scrubbed(
    fixture_path: Path, dark_share: float, tmp_path: Path
) -> None:
    """``temperature`` and ``co2`` are evaluated and clean — not skipped.

    A fix that disabled the MAD stage outright would satisfy every "no light
    cull" assertion above. This is what distinguishes the two.
    """
    result = _run_pass1(fixture_path, tmp_path)

    for column in ("temperature", "co2"):
        report = _report_for(result, column)
        assert report.is_skipped is False, f"{column} should still be scrubbed"
        assert report.skip_reason is None
        assert report.cull_rows == 0
        assert report.finite_rows == FIXTURE_ROWS


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_no_per_channel_cull_warning_is_raised(
    fixture_path: Path, dark_share: float, tmp_path: Path
) -> None:
    """A shipped run over either fixture produces no operator warning.

    The pre-fix run produced one on every upload — "culled 49.9% of channel
    'light'" — and it reached only the log.
    """
    result = _run_pass1(fixture_path, tmp_path)
    assert result.warnings == ()


# ---------------------------------------------------------------------------
# Neither guard subsumes the other. This is the asymmetry Phase 1 measured.
# ---------------------------------------------------------------------------


def test_exclusion_list_is_load_bearing_below_the_modal_limit() -> None:
    """With the exclusion list emptied, the low-modal fixture culls daylight again.

    This is the test that fails if someone deletes ``light`` from
    ``BOOTSTRAP_MAD_EXCLUDED_CHANNELS`` on the grounds that the generic modal
    share rule already covers it. At a 34.9% dark share it does not.
    """
    frame = _frame(LOW_MODAL_FIXTURE)
    without_exclusions = compute_mad_scrub(
        frame, _SENSOR_COLS, options=MadScrubOptions(excluded_columns=frozenset())
    )

    assert "light" not in without_exclusions.skipped_channels
    assert without_exclusions.cull_counts["light"] == EXPECTED_BRIGHT_ROWS


def test_generic_modal_rule_is_load_bearing_above_the_modal_limit() -> None:
    """With the exclusion list emptied, the high-modal fixture is still protected.

    The reason changes from ``excluded_channel`` to
    ``modal_share_above_limit`` — the second guard catching what the first no
    longer does — and the cull count stays at zero.
    """
    frame = _frame(HIGH_MODAL_FIXTURE)
    without_exclusions = compute_mad_scrub(
        frame, _SENSOR_COLS, options=MadScrubOptions(excluded_columns=frozenset())
    )

    assert without_exclusions.skipped_channels["light"] == SKIP_REASON_MODAL_SHARE
    assert without_exclusions.cull_counts["light"] == 0


@pytest.mark.parametrize(("fixture_path", "dark_share"), _CASES)
def test_disabling_both_guards_reproduces_the_pre_fix_cull(
    fixture_path: Path, dark_share: float
) -> None:
    """Turning both guards off restores the defect, on both fixtures.

    Without this the suite could pass because the fixtures are harmless rather
    than because the guards work. ~49.9% here against 47.41% measured on
    tenant_1 — the same failure, and the whole bright mode either way.
    """
    frame = _frame(fixture_path)
    unguarded = compute_mad_scrub(
        frame,
        _SENSOR_COLS,
        options=MadScrubOptions(excluded_columns=frozenset(), max_modal_share=1.0),
    )

    assert unguarded.skipped_channels == {}
    assert unguarded.cull_counts["light"] == EXPECTED_BRIGHT_ROWS
    assert float(unguarded.flags.mean()) > 0.45

    # Keyed on the stable machine-readable code, not on the prose: the message
    # is tenant-facing copy and will be edited. The channel name is checked
    # separately because the code cannot carry it — it repeats once per
    # offending channel, so the message is the only place `light` is named, and
    # that identification is contract rather than wording.
    assert [warning.code for warning in unguarded.warnings] == [
        WARNING_CODE_CHANNEL_CULL_HIGH
    ]
    assert "light" in unguarded.warnings[0].message


def test_light_is_excluded_by_default_configuration() -> None:
    """``light`` is in the shipped exclusion list.

    Pinned on its own because every pipeline assertion above depends on it, and
    a config-only regression should name itself rather than surface as six
    confusing failures.
    """
    assert "light" in BOOTSTRAP_MAD_EXCLUDED_CHANNELS
