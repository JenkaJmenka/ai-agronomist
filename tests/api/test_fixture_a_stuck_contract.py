"""Fixture A: the four stuck alerts, and trigger selection across chunk sizes.

Step-5 verification, run through the real upload path
(``run_cold_run_upload``) rather than the detector in isolation.

Fixture A ground truth is ``tests/fixtures/NATIVE_FIXTURES.md:31`` onward:
five humidity freeze runs at 100.0, of which exactly FOUR cross the wall-clock
gate. "Four alerts" and "6.00 h frozen at detection" are ONE fact, not two
independent checks — the gate stamps ``detected_ts = onset +
STUCK_WALLCLOCK_MIN_HOURS``, so it fixes the count and the duration together.
The only discriminating case is run #4 (2026-12-18 11:30 → 12:30, 1.00 h),
which is below the gate and must produce no alert at all; that is pinned
separately and explicitly.

Statistical hit counts are pinned as an invariant ACROSS chunk sizes, not
against an absolute figure. The absolute count on this fixture is an
unverified self-referential pin (``docs/runbooks/
native-dec-fixture-baseline.md:61-79``) and already lives in exactly one
place, ``tests/workers/test_stat_chunk_invariance.py``; re-stating the number
here would create a second copy of an unverified figure without adding
evidence for it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.api.services.cold_run_upload import (
    build_trigger_data_by_timestamp,
    run_cold_run_upload,
)
from src.config import LATENT_DIM, STUCK_WALLCLOCK_MIN_HOURS
from src.vendors.csv_adapter import CSVAdapter

FIXTURE_A = Path(__file__).resolve().parents[1] / "fixtures" / "native_dec_stuck_fault.csv"

CHUNK_SIZES = (50, 288, 500, 5000, 10**7)
REFERENCE_CHUNK_SIZE = 10**7

FROZEN_CHANNEL = "humidity"
FROZEN_VALUE = 100.0

EXPECTED_ONSETS = (
    "2026-12-14T00:00:00+00:00",
    "2026-12-15T11:05:00+00:00",
    "2026-12-16T11:20:00+00:00",
    "2026-12-18T13:00:00+00:00",
)
EXPECTED_DETECTED_TS = (
    "2026-12-14T06:00:00+00:00",
    "2026-12-15T17:05:00+00:00",
    "2026-12-16T17:20:00+00:00",
    "2026-12-18T19:00:00+00:00",
)

# Run #4 in NATIVE_FIXTURES.md: 2026-12-18 11:30 → 12:30, 1.00 h. Below the
# 6 h gate, so no alert may be stamped anywhere inside it. This is the one
# case that is NOT implied by the alert count.
SUB_GATE_RUN_START = "2026-12-18T11:30:00+00:00"
SUB_GATE_RUN_END = "2026-12-18T12:30:00+00:00"


def _mock_detector() -> MagicMock:
    """Constant sub-threshold LSTM score: stuck detection is model-independent."""
    return_value = MagicMock()
    return_value.predict_batch_enriched.side_effect = lambda seqs: (
        np.full(len(seqs), 0.05, dtype=np.float32),
        np.zeros((len(seqs), LATENT_DIM), dtype=np.float32),
        np.zeros(len(seqs), dtype=np.float32),
    )
    return return_value


_response_cache: dict[int, Any] = {}


def fixture_a_response(chunk_size: int) -> Any:
    """Score fixture A at ``chunk_size``, cached for the module's lifetime.

    Public because ``tests/dashboard/test_display_cap_survivors.py`` needs the
    same rows and this is the only place the run is set up; sharing the cache
    also keeps the fixture from being scored twice per session.

    Each run takes a few seconds and this file needs all five sizes twice
    over; the cache is keyed only by chunk size and the pipeline is pure with
    respect to it, so no test can observe another's state through it.
    """
    if chunk_size not in _response_cache:
        adapter = CSVAdapter({"filepath": str(FIXTURE_A)})
        adapter.connect()
        response, _ = asyncio.run(
            run_cold_run_upload(
                tmp_path=str(FIXTURE_A),
                adapter=adapter,
                detector=_mock_detector(),
                tenant_id="step5_fixture_a",
                greenhouse_id="gh_step5_fixture_a",
                chunk_size=chunk_size,
            )
        )
        _response_cache[chunk_size] = response
    return _response_cache[chunk_size]


def detector_members(detector: str | None) -> set[str]:
    """Split a possibly-compound label. Never compare the label for equality."""
    return set((detector or "").split("+"))


def stuck_alerts(response: Any) -> list[Any]:
    return sorted(
        (
            hit for hit in response.high_anomalies
            if "stuck_fault" in detector_members(hit.detector)
        ),
        key=lambda hit: hit.timestamp,
    )


def statistical_hits(response: Any) -> set[tuple[str, str | None]]:
    """Statistical hits by membership, so a compound label is counted too."""
    return {
        (hit.timestamp, hit.column)
        for hit in response.high_anomalies
        if "statistical" in detector_members(hit.detector)
    }


@pytest.fixture(scope="module")
def reference_alerts() -> list[Any]:
    assert FIXTURE_A.is_file(), f"Fixture A not found: {FIXTURE_A}"
    return stuck_alerts(fixture_a_response(REFERENCE_CHUNK_SIZE))


# ---------------------------------------------------------------------------
# 8. the four stuck alerts
# ---------------------------------------------------------------------------

class TestFourStuckAlerts:
    """Spec 8. Every per-alert assertion is made on the alerts themselves.

    Not on a filtered sublist: a filter that selects nothing turns
    ``all(...)`` into ``all([])`` and reports green while asserting nothing.
    The count is pinned first in each test so a short or empty list fails
    loudly before any per-alert claim is evaluated.
    """

    def test_exactly_four_alerts_on_the_frozen_channel(self, reference_alerts) -> None:
        assert [alert.column for alert in reference_alerts] == [FROZEN_CHANNEL] * 4

    def test_every_alert_is_urgent(self, reference_alerts) -> None:
        assert [alert.severity for alert in reference_alerts] == ["urgent"] * 4

    def test_onsets_are_the_measured_freeze_starts(self, reference_alerts) -> None:
        assert tuple(alert.timestamp for alert in reference_alerts) == EXPECTED_ONSETS

    def test_detected_ts_is_stamped_one_gate_after_each_onset(
        self, reference_alerts
    ) -> None:
        assert tuple(
            alert.detected_ts for alert in reference_alerts
        ) == EXPECTED_DETECTED_TS

    def test_every_alert_reports_the_gate_duration_as_frozen_hours(
        self, reference_alerts
    ) -> None:
        """Asserted on all four alerts directly — an empty list cannot pass."""
        assert len(reference_alerts) == 4
        assert [
            alert.frozen_hours_at_detection for alert in reference_alerts
        ] == [float(STUCK_WALLCLOCK_MIN_HOURS)] * 4

    def test_no_alert_carries_a_z_score(self, reference_alerts) -> None:
        """A numeric z_score here would be a duration read as a z-score."""
        assert len(reference_alerts) == 4
        assert [alert.z_score for alert in reference_alerts] == [None] * 4

    def test_the_sub_gate_freeze_run_produces_no_alert(self, reference_alerts) -> None:
        """The discriminating case: run #4 lasts 1.00 h and must be silent.

        Unlike the count and the 6.0 h — which the gate fixes together — this
        is an independent fact about the gate's threshold. Checked as a range
        rather than an exact timestamp so that an alert stamped anywhere
        inside the 1 h run fails, not only one stamped at its first sample.
        """
        inside_sub_gate_run = [
            alert.timestamp for alert in reference_alerts
            if SUB_GATE_RUN_START <= alert.timestamp <= SUB_GATE_RUN_END
        ]
        assert inside_sub_gate_run == [], (
            f"The 1.00 h freeze run must not alert; got {inside_sub_gate_run}"
        )

    def test_every_alert_names_the_frozen_value(self, reference_alerts) -> None:
        assert len(reference_alerts) == 4
        assert [alert.value for alert in reference_alerts] == [FROZEN_VALUE] * 4
