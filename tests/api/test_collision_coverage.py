"""Task 2c item 6: a stuck_fault onset colliding with a statistical hit.

Fixture A (``native_dec_stuck_fault.csv``) never exercises Task 2's
severity/detector-tier ranking: all 17 of its two-candidate timestamps are
statistical-vs-statistical and already differ at ``|z|``, and none of its
four stuck_fault onsets share a timestamp with any other detector's hit.
Neither do the other three native fixtures. So the ranking levels that
decide trigger attribution and display-cap survival when a stuck onset and
a statistical hit land on the SAME timestamp have never been run against
real data through the full upload path — only against hand-built
``UploadHighAnomalyDetail`` instances (``test_anomaly_rank_contract.py``).

This builds a synthetic CSV, drives it through ``run_cold_run_upload`` (the
same production entry point ``tests/api/test_stuck_fault_upload.py`` uses),
and checks both call sites that depend on the ranking at a real collision:
trigger attribution (``build_trigger_data_by_timestamp``) and the display
cap (``anomalies_page_data.trim_cold_run_rows``).

Construction, reproduced and verified directly (not assumed):
  - ``humidity`` is noisy (oscillating by 5, epsilon is 0.01) through row
    289, then exactly constant from row 290 for 110 rows -- long enough to
    cross the 6 h / 72-sample stuck gate. The value change at 289->290
    resets WallclockStuckDetector's freeze state, so onset_ts is exactly
    row 290's timestamp.
  - ``temperature`` carries a +15 deg spike at rows 288-290 (three
    consecutive samples -- COLD_RUN_STAT_MIN_CONSECUTIVE) against a tight
    baseline. The rolling detector's streak counter reaches 3 at row 290,
    so the statistical hit lands on exactly that same timestamp. +15 is
    deliberately small enough to stay under the adapter's absolute
    plausibility ceiling (above it, the whole chunk fails validation and
    no rows score at all) while still clearing the z >= 5.5 gate by a wide
    margin (measured z ~= 10.3, not asserted exactly since it is not the
    point of this test).
  - Both columns need >= 288 leading baseline rows before row 290, because
    the incremental rolling detector always uses the full configured
    ``COLD_RUN_STAT_ROLLING_WINDOW`` (288) and masks every earlier position
    as a non-candidate, regardless of file length.
  - ``co2`` is excluded from ``is_cold_run_stat_column`` entirely (it only
    matches temp/humidity/wind/deficit-like names), so it cannot also fire
    here; kept oscillating only to avoid it accidentally satisfying the
    (temperature, humidity, co2) wall-clock stuck channel list.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from src.api.services.cold_run_upload import (
    build_trigger_data_by_timestamp,
    run_cold_run_upload,
)
from src.dashboard.services.anomalies_page_data import trim_cold_run_rows
from src.models.inference import AnomalyDetector
from src.vendors.csv_adapter import CSVAdapter

N_ROWS = 400
CADENCE_MINUTES = 5
SPIKE_START_IDX = 288  # three consecutive candidates -> streak hits 3 here
SPIKE_LEN = 3
ONSET_IDX = SPIKE_START_IDX + SPIKE_LEN - 1  # 290: shared collision timestamp
SPIKE_OFFSET_DEGREES = 15.0  # stays under the adapter's plausibility ceiling
START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _temperature_baseline(index: int) -> float:
    return 20.0 + ((index % 9) - 4) * 0.3


def _co2_baseline(index: int) -> float:
    return 400.0 + ((index % 11) - 5) * 3.0


def _build_collision_csv() -> str:
    """A stuck humidity onset and a statistical temperature hit, same row."""
    lines = ["timestamp,temperature,humidity,co2,light"]
    for index in range(N_ROWS):
        ts = (START + timedelta(minutes=CADENCE_MINUTES * index)).isoformat()
        temperature = _temperature_baseline(index)
        if SPIKE_START_IDX <= index < SPIKE_START_IDX + SPIKE_LEN:
            temperature += SPIKE_OFFSET_DEGREES
        co2 = _co2_baseline(index)
        humidity = 55.0 if index >= ONSET_IDX else (60.0 if index % 2 == 0 else 65.0)
        lines.append(f"{ts},{temperature},{humidity},{co2},500.0")
    return "\n".join(lines) + "\n"


def _onset_timestamp() -> str:
    return (START + timedelta(minutes=CADENCE_MINUTES * ONSET_IDX)).isoformat()


def _enriched_side_effect(seqs):
    scores = np.full(len(seqs), 0.05, dtype=np.float32)
    embeddings = np.zeros((len(seqs), 8), dtype=np.float32)
    uncertainties = np.full(len(seqs), 0.1, dtype=np.float32)
    return scores, embeddings, uncertainties


@pytest.fixture(scope="module")
def collision_response():
    """Score the synthetic collision CSV through the full upload path once."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write(_build_collision_csv())
        tmp_name = tmp.name

    try:
        adapter = CSVAdapter({"filepath": tmp_name})
        adapter.connect()
        with patch(
            "src.models.inference.AnomalyDetector.predict_batch_enriched"
        ) as mock_pred:
            mock_pred.side_effect = _enriched_side_effect
            detector = AnomalyDetector.__new__(AnomalyDetector)
            detector._model = None
            detector.predict_batch_enriched = mock_pred

            response, _sequence_timestamps = asyncio.run(
                run_cold_run_upload(
                    tmp_path=tmp_name,
                    adapter=adapter,
                    detector=detector,
                    tenant_id="t_collision",
                    greenhouse_id="gh_collision",
                )
            )
        return response
    finally:
        Path(tmp_name).unlink(missing_ok=True)


class TestTheSyntheticFixtureActuallyCollides:
    """Non-vacuity: both hits must exist, on the same timestamp, before the
    ranking claims below mean anything.
    """

    def test_a_stuck_fault_hit_exists_at_the_onset_timestamp(
        self, collision_response
    ) -> None:
        stuck_hits = [
            h for h in collision_response.high_anomalies if h.detector == "stuck_fault"
        ]
        assert len(stuck_hits) == 1
        assert stuck_hits[0].timestamp == _onset_timestamp()
        assert stuck_hits[0].column == "humidity"
        assert stuck_hits[0].severity == "urgent"

    def test_a_statistical_hit_exists_at_the_same_timestamp(
        self, collision_response
    ) -> None:
        stat_hits = [
            h
            for h in collision_response.high_anomalies
            if h.detector == "statistical" and h.column == "temperature"
        ]
        assert len(stat_hits) == 1
        assert stat_hits[0].timestamp == _onset_timestamp()
        assert stat_hits[0].z_score >= 5.5


class TestTriggerAttributionPicksTheStuckFault:
    """The collision this fixture closes: stuck_fault must win the shared
    timestamp's trigger, through the real production call site.
    """

    def test_trigger_at_the_shared_timestamp_is_stuck_fault(
        self, collision_response
    ) -> None:
        triggers = build_trigger_data_by_timestamp(collision_response.high_anomalies)
        trigger = triggers[_onset_timestamp()]
        assert trigger["detector"] == "stuck_fault"
        assert trigger["channel"] == "humidity"
        assert trigger["severity"] == "urgent"


class TestDisplayCapKeepsTheStuckFault:
    """The cap must keep the urgent stuck row over the critical statistical
    row when forced to choose between exactly these two.
    """

    def test_capped_to_one_row_the_survivor_is_stuck_fault(
        self, collision_response
    ) -> None:
        dict_rows = [h.model_dump() for h in collision_response.high_anomalies]
        capped = trim_cold_run_rows(dict_rows, limit=1)
        assert len(capped) == 1
        assert capped[0]["detector"] == "stuck_fault"
        assert capped[0]["column"] == "humidity"
