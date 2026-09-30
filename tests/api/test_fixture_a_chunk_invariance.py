"""Fixture A: chunk size must not be observable in the output.

Step-5 verification of spec items 10 and 12. The ranking change added tiebreak
levels specifically so that a tie is broken by a field on the row rather than
by arrival order — and arrival order is exactly what varies when an upload's
chunk boundary moves. Every size is compared against the single-chunk
reference, which is the direct test of that claim.

Statistical hit counts are pinned as an invariant ACROSS chunk sizes, not
against an absolute figure. The absolute count on this fixture is an
unverified, self-referential pin (``docs/runbooks/
native-dec-fixture-baseline.md:61-79``) that already superseded an earlier
number, and it lives in exactly one place —
``tests/workers/test_stat_chunk_invariance.py``. Re-stating it here would
create a second copy of an unverified figure without adding any evidence for
it.

The four stuck alerts themselves are pinned in
``test_fixture_a_stuck_contract.py``.
"""

from __future__ import annotations

import pytest

from src.api.services.cold_run_upload import build_trigger_data_by_timestamp
from tests.api.test_fixture_a_stuck_contract import (
    CHUNK_SIZES,
    EXPECTED_ONSETS,
    FROZEN_CHANNEL,
    REFERENCE_CHUNK_SIZE,
    detector_members,
    fixture_a_response,
    statistical_hits,
    stuck_alerts,
)


class TestChunkSizeInvariance:
    """Spec 10. Chunk size must not be observable in the output.

    The ranking change added tiebreak levels specifically so that a tie is
    broken by a field on the row instead of by arrival order, which does vary
    with where a chunk boundary falls. Comparing every size against the
    single-chunk reference is the direct test of that claim.
    """

    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    def test_statistical_hit_set_matches_the_single_chunk_reference(
        self, chunk_size: int
    ) -> None:
        reference = statistical_hits(fixture_a_response(REFERENCE_CHUNK_SIZE))
        measured = statistical_hits(fixture_a_response(chunk_size))
        assert measured == reference, (
            f"chunk {chunk_size}: only in reference {sorted(reference - measured)}, "
            f"only in chunk {sorted(measured - reference)}"
        )

    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    def test_statistical_hit_count_matches_the_single_chunk_reference(
        self, chunk_size: int
    ) -> None:
        """Count as well as set: a set comparison alone hides duplication."""
        reference_response = fixture_a_response(REFERENCE_CHUNK_SIZE)
        measured_response = fixture_a_response(chunk_size)
        reference_rows = [
            hit for hit in reference_response.high_anomalies
            if "statistical" in detector_members(hit.detector)
        ]
        measured_rows = [
            hit for hit in measured_response.high_anomalies
            if "statistical" in detector_members(hit.detector)
        ]
        assert len(measured_rows) == len(reference_rows)

    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    def test_trigger_selection_matches_the_single_chunk_reference(
        self, chunk_size: int
    ) -> None:
        """Identical trigger channel/detector/severity at every timestamp."""
        reference = build_trigger_data_by_timestamp(
            fixture_a_response(REFERENCE_CHUNK_SIZE).high_anomalies
        )
        measured = build_trigger_data_by_timestamp(
            fixture_a_response(chunk_size).high_anomalies
        )
        assert measured == reference

    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    def test_stuck_alerts_match_the_single_chunk_reference(
        self, chunk_size: int
    ) -> None:
        reference = stuck_alerts(fixture_a_response(REFERENCE_CHUNK_SIZE))
        measured = stuck_alerts(fixture_a_response(chunk_size))
        assert [alert.model_dump(mode="json") for alert in measured] == [
            alert.model_dump(mode="json") for alert in reference
        ]


# ---------------------------------------------------------------------------
# 12. the trigger attribution this fixture actually produces
# ---------------------------------------------------------------------------

class TestFixtureATriggerAttribution:
    """Spec 12. What the ranking change does — and does not — alter here.

    On this fixture the four stuck onsets are the ONLY hits at their
    timestamps: humidity statistical hits are suppressed inside the fault
    window, so no stuck-vs-statistical collision exists and the severity
    level never has to arbitrate one. The stuck rows win their timestamps by
    being alone there, which was equally true before the ranking changed.
    These pins therefore record the attribution, and the absence of a
    collision, rather than demonstrating the new ordering — the collision
    cases are covered synthetically in ``test_anomaly_rank_contract.py``.
    """

    def test_each_stuck_onset_is_attributed_to_the_stuck_detector(self) -> None:
        response = fixture_a_response(REFERENCE_CHUNK_SIZE)
        triggers = build_trigger_data_by_timestamp(response.high_anomalies)
        attributed = {
            onset: (
                triggers[onset]["channel"],
                triggers[onset]["detector"],
                triggers[onset]["severity"],
            )
            for onset in EXPECTED_ONSETS
        }
        assert attributed == {
            onset: (FROZEN_CHANNEL, "stuck_fault", "urgent")
            for onset in EXPECTED_ONSETS
        }

    def test_no_stuck_onset_shares_its_timestamp_with_another_detector(self) -> None:
        """Pins the premise of the class docstring, so it cannot rot silently.

        If a future fixture change introduces a collision, this fails and the
        tests above stop being merely descriptive — that is the signal to
        assert the severity ordering on real data here.
        """
        response = fixture_a_response(REFERENCE_CHUNK_SIZE)
        colliding = [
            (hit.timestamp, hit.column, hit.detector)
            for hit in response.high_anomalies
            if hit.timestamp in EXPECTED_ONSETS
            and "stuck_fault" not in detector_members(hit.detector)
        ]
        assert colliding == []

    def test_every_trigger_is_either_stuck_fault_or_statistical(self) -> None:
        """No solo-lstm trigger: the mock detector scores below threshold."""
        response = fixture_a_response(REFERENCE_CHUNK_SIZE)
        triggers = build_trigger_data_by_timestamp(response.high_anomalies)
        detectors = {trigger["detector"] for trigger in triggers.values()}
        assert detectors == {"stuck_fault", "statistical"}

    def test_exactly_four_timestamps_are_attributed_to_the_stuck_detector(self) -> None:
        response = fixture_a_response(REFERENCE_CHUNK_SIZE)
        triggers = build_trigger_data_by_timestamp(response.high_anomalies)
        stuck_triggers = [
            timestamp for timestamp, trigger in triggers.items()
            if "stuck_fault" in detector_members(trigger["detector"])
        ]
        assert sorted(stuck_triggers) == sorted(EXPECTED_ONSETS)
