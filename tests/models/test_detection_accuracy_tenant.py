"""Second accuracy fixture: the July composite on tenant_1 weeks — opt-in.

**How to run this.** Point ``PASS1_TENANT_CSV`` at the tenant_1 CSV::

    PASS1_TENANT_CSV=/path/to/2026_1d_english_clean_fixed.csv \\
        ./venv/bin/python -m pytest tests/models/test_detection_accuracy_tenant.py -q

The variable is the one ``tests/models/test_pass1_tenant_corpus.py`` already
uses, reused on purpose: it names the same file, and a second variable for
the same corpus would only let the two modules drift onto different files.
Without it every test here skips. CI never sets it. The corpus is customer
data. It stays outside the repository and is never copied into it. This
module reads it in place and states only aggregate figures.

**What it measures.** The composite from ``test_detection_accuracy.py``,
computed by that module's own harness (``_measure_pipeline(frame=...)``), on
clean tenant weeks from three seasons. The July gate already covers summer.
Same stride, same split, same CO2 replacement (the corpus has no CO2 column
either), same three injected faults, same ``_COMPOSITE_BAR``. Nothing is
re-implemented here.

The injected-fault half of the composite scores the harness's synthetic
quiet windows at the harness's fixed July noon. A tenant week therefore moves
that half only through its calibration windows (``noise_clean_windows``).
The clean-specificity half is where the week really shows.

**Week selection.** The rule is in :func:`_week_is_clean` and is fixed before
any week is scored. Blocks are 7 days, aligned to 2026-01-01 00:00 UTC. A
block qualifies when all of these are true:

* it has exactly 2016 rows at a regular 5-minute cadence, and no NaN;
* temperature, humidity and light stay inside ``OOB_LIMITS``;
* humidity never reads 100 %RH (the December freeze value);
* the longest flat run (``STUCK_WALLCLOCK_EPSILON``) on temperature and on
  humidity is shorter than half the wall-clock stuck gate (3 h, 36 samples).

From each season window the FIRST qualifying block is taken. No detector
output is used to pick a week.

**December.** The humidity freeze in this corpus is detected by the upload
path's wall-clock stuck gate, not by the windowed composite, so the check
runs ``run_cold_run_upload`` on the corpus file in place, as
``tests/api/test_fixture_a_stuck_contract.py`` does on its December fixture.
The ground truth is measured from the frame on its own: flat humidity runs
that cross the same gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.api.services.cold_run_upload import run_cold_run_upload
from src.config import (
    OOB_LIMITS,
    STUCK_WALLCLOCK_EPSILON,
    STUCK_WALLCLOCK_MIN_HOURS,
    STUCK_WALLCLOCK_MIN_SAMPLES,
)
from src.vendors.csv_adapter import CSVAdapter
from tests.api.test_fixture_a_stuck_contract import _mock_detector, stuck_alerts
from tests.models import test_detection_accuracy as harness
from tests.models.test_pass1_tenant_corpus import (
    EXPECTED_RAW_DATA_LINES,
    _tenant_csv_path,
)

# Adapter-stage row count. Not Pass 1's 104,921: the adapter keeps the one
# duplicate timestamp that Pass 1's dedup removes. Measured.
EXPECTED_ADAPTER_ROWS = 104_922

_CADENCE_SECONDS = 300
_ROWS_PER_WEEK = 7 * 24 * 3600 // _CADENCE_SECONDS
_YEAR_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Half the wall-clock stuck gate, in samples: 6 h / 2 at 5 min = 36.
_MAX_FLAT_RUN = int(STUCK_WALLCLOCK_MIN_HOURS * 3600 / 2 / _CADENCE_SECONDS)
_FLAT_CHANNELS = ("temperature", "humidity")
_OOB_CHANNELS = ("temperature", "humidity", "light")
_HUMIDITY_SATURATED = 100.0

# Season windows [start, end). Summer is the July gate's.
_SEASONS: dict[str, tuple[datetime, datetime]] = {
    "winter": (datetime(2026, 1, 1, tzinfo=timezone.utc),
               datetime(2026, 3, 1, tzinfo=timezone.utc)),
    "spring": (datetime(2026, 3, 1, tzinfo=timezone.utc),
               datetime(2026, 6, 1, tzinfo=timezone.utc)),
    "autumn": (datetime(2026, 9, 1, tzinfo=timezone.utc),
               datetime(2026, 12, 1, tzinfo=timezone.utc)),
}

# The rule's output, measured. Pinned so a change to the rule or the corpus
# fails here and not as a moved composite.
EXPECTED_WEEKS: dict[str, datetime] = {
    "winter": datetime(2026, 1, 8, tzinfo=timezone.utc),
    "spring": datetime(2026, 4, 2, tzinfo=timezone.utc),
    "autumn": datetime(2026, 9, 3, tzinfo=timezone.utc),
}

# December freeze, measured on the corpus with the shipped gate.
_DECEMBER_START = datetime(2026, 12, 1, tzinfo=timezone.utc)
_FREEZE_SEASON_START = datetime(2026, 11, 1, tzinfo=timezone.utc)
# Gate-crossing humidity runs that overlap December (one starts 28 Nov).
EXPECTED_DECEMBER_FREEZE_RUNS = 12
# Of those, runs whose onset is in December, and so alert in December.
EXPECTED_DECEMBER_ONSET_ALERTS = 11
# All gate-crossing humidity runs in the corpus (first onset 21 Nov).
EXPECTED_TOTAL_FREEZE_RUNS = 18
EXPECTED_TOTAL_HUMIDITY_STUCK_ALERTS = 18

_week_cache: dict[str, dict] = {}


@pytest.fixture(scope="module")
def tenant_csv() -> Path:
    return _tenant_csv_path()


@pytest.fixture(scope="module")
def tenant_frame(tenant_csv: Path) -> pd.DataFrame:
    """The whole corpus through ``CSVAdapter``, the July gate's ingest path."""
    return harness._load_csv(tenant_csv)


def _week_slice(frame: pd.DataFrame, start: datetime) -> pd.DataFrame:
    end = start + pd.Timedelta(days=7)
    stamps = frame["timestamp"]
    week = frame[(stamps >= start) & (stamps < end)]
    return week.reset_index(drop=True)


def _longest_flat_run(values: np.ndarray, epsilon: float) -> int:
    """Longest run of consecutive samples that move by at most *epsilon*."""
    if len(values) == 0:
        return 0
    flat = np.abs(np.diff(values)) <= epsilon
    best = current = 0
    for step_is_flat in flat:
        current = current + 1 if step_is_flat else 0
        best = max(best, current)
    return best + 1


def _week_quality(week: pd.DataFrame) -> dict:
    steps = week["timestamp"].diff().dropna().dt.total_seconds()
    quality: dict = {
        "rows": int(len(week)),
        "irregular_steps": int((steps != _CADENCE_SECONDS).sum()),
        "nan_cells": int(week.loc[:, list(_OOB_CHANNELS)].isna().sum().sum()),
        "humidity_saturated_rows": int(
            (week["humidity"] >= _HUMIDITY_SATURATED).sum()
        ),
    }
    for channel in _OOB_CHANNELS:
        low, high = OOB_LIMITS[channel]
        values = week[channel].to_numpy(dtype=float)
        quality[f"{channel}_oob_rows"] = int(((values < low) | (values > high)).sum())
    for channel in _FLAT_CHANNELS:
        quality[f"{channel}_longest_flat_run"] = _longest_flat_run(
            week[channel].to_numpy(dtype=float), STUCK_WALLCLOCK_EPSILON[channel]
        )
    return quality


def _week_is_clean(quality: dict) -> bool:
    return (
        quality["rows"] == _ROWS_PER_WEEK
        and quality["irregular_steps"] == 0
        and quality["nan_cells"] == 0
        and quality["humidity_saturated_rows"] == 0
        and all(quality[f"{c}_oob_rows"] == 0 for c in _OOB_CHANNELS)
        and all(quality[f"{c}_longest_flat_run"] < _MAX_FLAT_RUN for c in _FLAT_CHANNELS)
    )


def _select_weeks(frame: pd.DataFrame) -> dict[str, datetime]:
    """First clean 7-day block inside each season window."""
    chosen: dict[str, datetime] = {}
    for season, (low, high) in _SEASONS.items():
        start = _YEAR_START
        while start + pd.Timedelta(days=7) <= high:
            if start >= low and _week_is_clean(_week_quality(_week_slice(frame, start))):
                chosen[season] = start
                break
            start = start + pd.Timedelta(days=7)
    return chosen


def _week_report(frame: pd.DataFrame, season: str) -> dict:
    """One harness run per week, cached for the module."""
    if season not in _week_cache:
        week = _week_slice(frame, EXPECTED_WEEKS[season])
        _week_cache[season] = asyncio.run(harness._measure_pipeline(frame=week))
    return _week_cache[season]


def _freeze_runs(frame: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Humidity flat runs that cross the wall-clock stuck gate.

    Measured from the frame alone, with the gate's own constants, so it is
    ground truth independent of the detector under test.
    """
    values = frame["humidity"].to_numpy(dtype=float)
    stamps = frame["timestamp"].reset_index(drop=True)
    flat = np.abs(np.diff(values)) <= STUCK_WALLCLOCK_EPSILON["humidity"]
    runs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    index = 0
    while index < len(flat):
        if not flat[index]:
            index += 1
            continue
        end = index
        while end < len(flat) and flat[end]:
            end += 1
        hours = (stamps[end] - stamps[index]).total_seconds() / 3600.0
        if hours >= STUCK_WALLCLOCK_MIN_HOURS and end - index + 1 >= STUCK_WALLCLOCK_MIN_SAMPLES:
            runs.append((stamps[index], stamps[end]))
        index = end
    return runs


@pytest.fixture(scope="module")
def upload_response(tenant_csv: Path):
    """``run_cold_run_upload`` on the corpus file in place. No copy is made."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    adapter = CSVAdapter({"filepath": str(tenant_csv)})
    adapter.connect()
    try:
        response, _ = asyncio.run(
            run_cold_run_upload(
                tmp_path=str(tenant_csv),
                adapter=adapter,
                detector=_mock_detector(),
                tenant_id="tenant_accuracy_december",
                greenhouse_id="gh_tenant_accuracy_december",
            )
        )
    finally:
        adapter.disconnect()
        logging.disable(previous)
    return response


def _humidity_stuck_alerts(response) -> list:
    return [alert for alert in stuck_alerts(response) if alert.column == "humidity"]


# ---------------------------------------------------------------------------
# Corpus and week selection
# ---------------------------------------------------------------------------


def test_corpus_is_the_expected_file(tenant_csv: Path, tenant_frame: pd.DataFrame) -> None:
    """Wrong file fails here, not as a moved composite."""
    with tenant_csv.open(encoding="utf-8") as handle:
        data_lines = sum(1 for _ in handle) - 1
    assert data_lines == EXPECTED_RAW_DATA_LINES
    assert len(tenant_frame) == EXPECTED_ADAPTER_ROWS
    assert float(np.ptp(tenant_frame["co2"].to_numpy(dtype=float))) == 0.0


def test_selection_rule_picks_the_pinned_weeks(tenant_frame: pd.DataFrame) -> None:
    """The rule, run on the corpus, picks one week per season, as pinned."""
    chosen = _select_weeks(tenant_frame)
    qualities = {
        season: _week_quality(_week_slice(tenant_frame, start))
        for season, start in chosen.items()
    }
    message = json.dumps(qualities, indent=2)
    assert chosen == EXPECTED_WEEKS, message
    for season, quality in qualities.items():
        assert _week_is_clean(quality), f"{season}: {message}"


def test_selection_rule_rejects_the_freeze_weeks(tenant_frame: pd.DataFrame) -> None:
    """Every block from 26 Nov to 24 Dec fails the rule.

    These are the freeze weeks. A rule that let one through would score
    a stuck sensor as clean data.
    """
    start = datetime(2026, 11, 26, tzinfo=timezone.utc)
    rejected = []
    while start < datetime(2026, 12, 31, tzinfo=timezone.utc):
        quality = _week_quality(_week_slice(tenant_frame, start))
        rejected.append(not _week_is_clean(quality))
        assert quality["humidity_saturated_rows"] > 0, start.isoformat()
        start = start + pd.Timedelta(days=7)
    assert len(rejected) == 5
    assert all(rejected)


# ---------------------------------------------------------------------------
# Composite per week (same definition and bar as the July gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("season", list(EXPECTED_WEEKS))
def test_week_composite_meets_the_bar(tenant_frame: pd.DataFrame, season: str) -> None:
    """Clean specificity and injected recall on one tenant week."""
    report = _week_report(tenant_frame, season)
    message = json.dumps(report, indent=2, default=str)
    assert report["co2_replaced"] is True, message
    assert report["composite_bar"] == harness._COMPOSITE_BAR == 0.90, message
    assert report["n_eval_windows"] > 0, message
    assert float(report["composite"]) >= harness._COMPOSITE_BAR, message


@pytest.mark.parametrize("season", list(EXPECTED_WEEKS))
def test_week_injected_spike_meets_the_upload_recall_bar(
    tenant_frame: pd.DataFrame, season: str
) -> None:
    """+6 °C recall stays at the bar the July sweep asserts."""
    report = _week_report(tenant_frame, season)
    spike = report["recalls"]["spike"]
    assert float(spike["detection_recall"]) >= harness._UPLOAD_RECALL_BAR, json.dumps(
        spike, indent=2
    )


# ---------------------------------------------------------------------------
# December humidity freeze
# ---------------------------------------------------------------------------


def test_december_freeze_ground_truth(tenant_frame: pd.DataFrame) -> None:
    """Gate-crossing humidity runs in the corpus, measured from the frame."""
    runs = _freeze_runs(tenant_frame)
    december = [run for run in runs if run[1] >= _DECEMBER_START]
    assert len(runs) == EXPECTED_TOTAL_FREEZE_RUNS
    assert len(december) == EXPECTED_DECEMBER_FREEZE_RUNS
    assert all(start >= _FREEZE_SEASON_START for start, _end in runs)


def test_december_humidity_freeze_is_detected(
    tenant_frame: pd.DataFrame, upload_response
) -> None:
    """Every December freeze run gets a humidity stuck alert at its onset."""
    runs = [run for run in _freeze_runs(tenant_frame) if run[1] >= _DECEMBER_START]
    alerts = _humidity_stuck_alerts(upload_response)
    onsets = {alert.timestamp for alert in alerts}
    detected = [start for start, _end in runs if start.isoformat() in onsets]
    missed = [start.isoformat() for start, _end in runs if start.isoformat() not in onsets]

    assert len(runs) == EXPECTED_DECEMBER_FREEZE_RUNS
    assert len(detected) >= 1
    assert missed == []
    december_onsets = [a for a in alerts if a.timestamp >= _DECEMBER_START.isoformat()]
    assert len(december_onsets) == EXPECTED_DECEMBER_ONSET_ALERTS
    assert {alert.severity for alert in december_onsets} == {"urgent"}


def test_no_stuck_alert_before_the_freeze_season(upload_response) -> None:
    """Jan to Oct carries no stuck alert at all; the gate fires only in the fault."""
    alerts = stuck_alerts(upload_response)
    early = [a.timestamp for a in alerts if a.timestamp < _FREEZE_SEASON_START.isoformat()]
    assert early == []
    assert {alert.column for alert in alerts} == {"humidity"}
    assert len(_humidity_stuck_alerts(upload_response)) == EXPECTED_TOTAL_HUMIDITY_STUCK_ALERTS
