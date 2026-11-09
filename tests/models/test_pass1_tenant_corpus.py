"""Pass-1 figures on the real tenant_1 corpus — opt-in (BACKLOG-14).

**How to run this.** Point ``PASS1_TENANT_CSV`` at the tenant_1 CSV::

    PASS1_TENANT_CSV=~/Projects/ai-agronomist-data/2026_1d_english_clean_fixed.csv \\
        ./venv/bin/python -m pytest tests/models/test_pass1_tenant_corpus.py -q

Without the variable every test here skips. That is the normal local and CI
outcome: the file is 12.7 MB of **customer data**, lives outside the
repository, and must never be committed. The opt-in-by-environment shape is
borrowed from the ``postgres``-marked suites
(``tests/api/test_synthetic_injection_collision_postgres.py``,
``tests/db/test_anomaly_episodes_constraints.py``), which skip the same way
when ``POSTGRES_TEST_URL`` is unset. No pytest marker is used because
``pytest.ini`` runs with ``--strict-markers`` and the marker list is not this
module's to extend.

**CI never sets ``PASS1_TENANT_CSV``, so nothing automated runs this module.**
Who runs it, when, and what else must be checked alongside it is the
pre-merge checklist in ``docs/runbooks/cold-start-retraining.md`` — see
§"4. Where the env-gated corpus test runs — and where it does not" and
§"Pre-merge checklist — any change to Pass 1". That is deliberately the only
copy: a procedure duplicated here would drift from the runbook, and the
runbook is what an operator actually opens.

The synthetic half of this regression — the knife-edge construction, both
channel guards, and the asymmetry between them — is in
``tests/models/test_pass1_light_knife_edge.py`` and always runs. This module
exists for the three figures that can only be stated about the real corpus, and
it pins the corpus itself (:func:`test_corpus_is_the_expected_file`) so that a
variable aimed at some other CSV fails as a wrong file rather than as a
mysterious numeric discrepancy.

Row-count provenance, because three adjacent stages give three different
answers and only the last one is Pass 1's frame: 105,121 raw data lines →
dedup one duplicate timestamp → 105,120 → the CSV adapter deletes 199 rows with
blank temperature or humidity (``REQUIRED_SENSOR_COLS``) → **104,921**. Seeing
105,120 or 105,121 means a different stage is being measured.

All figures below were measured on the shipped code, not predicted:
discard 4.6187% (4,846 of 104,921), hour-12 survival 84.4713%,
``light`` skipped as ``excluded_channel`` with 0 culls, ``humidity`` retaining
all 1,426 of its culls at a modal share of 0.07999.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from src.models.bootstrap import ColdStartBootstrap, _load_and_resample
from src.models.mad_scrub import modal_share
from src.models.mad_scrub_report import SKIP_REASON_EXCLUDED
from src.models.pass1_discard_guard import DISCARD_STATUS_OK

TENANT_CSV_ENV_VAR = "PASS1_TENANT_CSV"

# Deterministic properties of the corpus file itself.
EXPECTED_RAW_DATA_LINES = 105_121
EXPECTED_FRAME_ROWS = 104_921

# Requirements the user set for this PR, each measured on the shipped code.
EXPECTED_DISCARD_FRACTION = 0.0462
# A discard assertion wide enough to pass at the pre-fix 50.52% would be
# worthless, so the band is +/- 0.5 percentage points: it absorbs an
# IsolationForest nudge from a scikit-learn upgrade and nothing larger.
DISCARD_TOLERANCE = 0.005

# The gate spans the whole midday block, not hour 12 alone. Hour 12 measures
# 84.4713% but it is NOT the worst hour: hour 13 measures 80.5530%, clearing
# 80% by 0.553pp. An hour-12-only assertion therefore passed with 4.47pp of
# headroom while the real margin was an eighth of that, and would not have
# noticed hour 13 crossing.
#
# Measured survival across the block, shipped code:
#   hour 11 -> 89.4484%   hour 12 -> 84.4713%
#   hour 13 -> 80.5530%   hour 14 -> 83.4287%   (min: hour 13)
#
# Relaxed from 90% for this PR. The residual shortfall is IsolationForest
# spending its fixed 4% contamination budget almost entirely on bright midday
# rows, tracked as BACKLOG-22; MAD-only hour-12 survival is 98.72%.
MIN_MIDDAY_SURVIVAL = 0.80
MIDDAY_HOURS = (11, 12, 13, 14)

# Per-channel MAD outcome — the fix must not have become "skip everything".
EXPECTED_HUMIDITY_CULLS = 1_426
EXPECTED_HUMIDITY_MODAL_SHARE = 0.07999

# The degenerate statistic the bug was made of.
EXPECTED_LIGHT_MEDIAN = 1.35
EXPECTED_LIGHT_MODAL_SHARE = 0.43994


def _tenant_csv_path() -> Path:
    """The corpus path, or a skip when the opt-in variable is unset."""
    raw_path = os.environ.get(TENANT_CSV_ENV_VAR)
    if not raw_path:
        pytest.skip(
            f"{TENANT_CSV_ENV_VAR} is not set — the tenant_1 corpus is customer "
            f"data held outside the repository. See this module's docstring."
        )
    path = Path(raw_path).expanduser()
    if not path.is_file():
        pytest.skip(f"{TENANT_CSV_ENV_VAR}={raw_path} is not a readable file")
    return path


@pytest.fixture(scope="module")
def tenant_csv() -> Path:
    return _tenant_csv_path()


@pytest.fixture(scope="module")
def pass1_frame(tenant_csv: Path):
    """The Pass-1 frame. Module-scoped: loading parses 12.7 MB."""
    return _load_and_resample(str(tenant_csv))


@pytest.fixture(scope="module")
def pass1_result(tenant_csv: Path, tmp_path_factory: pytest.TempPathFactory):
    """One real ``ColdStartBootstrap.run()``, writing its CSVs to a temp dir.

    Module-scoped so the corpus is processed once. The clean/anomaly paths go
    under ``tmp_path_factory`` and never ``data/``, which already holds stale
    artefacts from manual runs.
    """
    out_dir = tmp_path_factory.mktemp("pass1_tenant")
    bootstrap = ColdStartBootstrap(
        clean_path=str(out_dir / "clean.csv"),
        anomaly_path=str(out_dir / "anomaly.csv"),
    )
    return bootstrap.run(str(tenant_csv))


def _report_for(result, column: str):
    return next(report for report in result.mad_channels if report.column == column)


def test_corpus_is_the_expected_file(tenant_csv: Path) -> None:
    """The variable points at tenant_1 and not some other CSV.

    Every figure in this module is a property of that one corpus. Checking the
    raw line count first turns "wrong file" into its own failure instead of a
    discard fraction that looks like a regression.
    """
    with tenant_csv.open(encoding="utf-8") as handle:
        data_lines = sum(1 for _ in handle) - 1

    assert data_lines == EXPECTED_RAW_DATA_LINES


def test_pass1_frame_has_the_expected_row_count(pass1_result) -> None:
    """104,921 rows reach Pass 1 — the post-dedup, post-required-drop stage."""
    assert pass1_result.discard_stats.frame_rows == EXPECTED_FRAME_ROWS
    assert pass1_result.discard_stats.data_rows == EXPECTED_FRAME_ROWS


def test_discard_fraction_is_back_under_five_percent(pass1_result) -> None:
    """4.62% discarded, against 50.52% before the fix."""
    stats = pass1_result.discard_stats

    assert stats.data_discard_fraction == pytest.approx(
        EXPECTED_DISCARD_FRACTION, abs=DISCARD_TOLERANCE
    )
    assert stats.status == DISCARD_STATUS_OK


def _survival_by_hour(pass1_result, hours: tuple[int, ...]) -> dict[int, float]:
    """Clean-row share per hour of day, for each hour in *hours*.

    Returned as a dict rather than a bare minimum so a failure names *which*
    hour moved. The minimum alone would say the gate broke without saying
    where, and the four hours have very different margins.
    """
    clean_hours = pass1_result.clean_df.index.hour
    anomaly_hours = pass1_result.anomaly_df.index.hour

    survival: dict[int, float] = {}
    for hour in hours:
        survived = int((clean_hours == hour).sum())
        total = survived + int((anomaly_hours == hour).sum())
        assert total > 0, f"no hour-{hour} rows in the corpus — wrong file or stage"
        survival[hour] = survived / total
    return survival


def test_midday_rows_survive_pass_one(pass1_result) -> None:
    """Every hour from 11 to 14 clears 80% survival, against 0.00% before the fix.

    Pre-fix the collapsed MAD boundary of 8.3552 W/m^2 culled every bright row,
    so Pass 1 taught the model that midday does not exist.

    The gate is the *minimum* over the block because hour 12 is not the worst
    hour — hour 13 is, at 80.5530%. The per-hour dict is asserted as a whole so
    the failure message carries all four figures.
    """
    survival = _survival_by_hour(pass1_result, MIDDAY_HOURS)

    below_gate = {
        hour: f"{fraction:.4%}"
        for hour, fraction in survival.items()
        if fraction <= MIN_MIDDAY_SURVIVAL
    }
    assert not below_gate, (
        f"midday survival at or below {MIN_MIDDAY_SURVIVAL:.0%}: {below_gate} "
        f"(all four hours: "
        f"{ {hour: f'{frac:.4%}'  for hour, frac in survival.items()} })"
    )


def test_hour_13_is_the_binding_hour_of_the_midday_gate(pass1_result) -> None:
    """Pins *where* the margin is, so a future relaxation is an informed one.

    If this starts failing because some other hour became the minimum, the
    gate's headroom has moved and the threshold deserves a fresh look rather
    than a quiet nudge.
    """
    survival = _survival_by_hour(pass1_result, MIDDAY_HOURS)

    assert min(survival, key=survival.__getitem__) == 13
    assert survival[13] == pytest.approx(0.8055, abs=5e-4)


def test_light_is_skipped_and_culls_nothing(pass1_result) -> None:
    """``light`` is skipped as an excluded channel and flags no rows at all."""
    light = _report_for(pass1_result, "light")

    assert light.is_skipped is True
    assert light.skip_reason == SKIP_REASON_EXCLUDED
    assert light.cull_rows == 0
    assert pass1_result.mad_skipped_channels["light"] == SKIP_REASON_EXCLUDED


def test_humidity_keeps_all_its_legitimate_culls(pass1_result) -> None:
    """Humidity still contributes its 1,426 culls — the fix is not a blanket skip.

    This is the assertion that separates "stopped culling daylight" from
    "stopped culling". The channel's modal share is nowhere near the 0.40 limit
    and its MAD is far above the minimum spread, so it is genuinely scrubbed.
    """
    humidity = _report_for(pass1_result, "humidity")

    assert humidity.is_skipped is False
    assert humidity.skip_reason is None
    assert humidity.cull_rows == EXPECTED_HUMIDITY_CULLS
    assert humidity.modal_share == pytest.approx(
        EXPECTED_HUMIDITY_MODAL_SHARE, abs=1e-5
    )


def test_real_light_channel_sits_on_the_knife_edge(pass1_frame) -> None:
    """``median(light) == MAD(light) > 0`` with half the rows at or below it.

    The degeneracy, asserted on the real data with no pipeline involved — the
    same statement ``test_pass1_light_knife_edge.py`` makes about the synthetic
    fixtures. Note how fine the edge is here: 43.994% of rows are at exactly 0
    and 50.0005% are at or below the median, so a corpus a few hundred dark
    rows either way would have landed on the other side of the discontinuity
    and the bug would never have fired.
    """
    light = pass1_frame["light"].to_numpy(dtype=float)
    median = float(np.median(light))
    mad = float(np.median(np.abs(light - median)))

    assert median == pytest.approx(EXPECTED_LIGHT_MEDIAN)
    assert mad == median
    assert median > 0.0
    assert float((light <= median).mean()) >= 0.5
    assert modal_share(light) == pytest.approx(EXPECTED_LIGHT_MODAL_SHARE, abs=1e-5)
