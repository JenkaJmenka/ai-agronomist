"""ADR-014 step A2: scoring context and profile types.

The profiles are compared with the names each production path actually
binds today (``upload_portions``, ``comprehensive_pipeline``, ``live``), not
with ``src.config`` alone, so a later change to a path's constant that is not
mirrored in its profile fails here.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from src import config
from src.models.scoring import (
    API_PROFILE,
    BATCH_PROFILE,
    PROFILES,
    STREAM_PROFILE,
    LstmDecisionRule,
    ScoringContext,
    ScoringProfile,
    SpikeConfirmMode,
)


def _pairs(profile: ScoringProfile) -> dict[str, tuple[float, float]]:
    return {p.sensor: (p.tuning_c, p.floor) for p in profile.spike_pairs}


def test_batch_profile_matches_upload_path() -> None:
    from src.api.services import upload_portions as up
    from src.models import cold_run_threshold as crt

    assert BATCH_PROFILE.spike_confirm.mode is SpikeConfirmMode.LOOK_AHEAD
    assert BATCH_PROFILE.spike_confirm.steps == up.UPLOAD_SPIKE_REVERT_HORIZON
    assert _pairs(BATCH_PROFILE) == {
        s: (float(up.UPLOAD_SPIKE_CONFIRMED_TUNING_C), float(f))
        for s, f in up.UPLOAD_SPIKE_CONFIRMED_FLOOR.items()
    }
    assert BATCH_PROFILE.humidity_spike_enabled is ("humidity" in up.UPLOAD_SPIKE_CONFIRMED_FLOOR)
    decision = BATCH_PROFILE.lstm_decision
    assert decision.rule is LstmDecisionRule.FILE_PERCENTILE_ROBUST
    assert decision.percentile == crt.COLD_RUN_PERCENTILE
    expected_z = crt.COLD_RUN_ROBUST_GATE_Z if crt.COLD_RUN_ROBUST_GATE_ENABLED else None
    assert decision.robust_gate_z == expected_z


def test_stream_and_api_profiles_match_pipeline_paths() -> None:
    from src.api.services import comprehensive_pipeline as cp
    from src.live import runtime_builder, scoring

    expected_pairs = {
        s: (float(c), float(f)) for s, (c, f) in cp.ENRICHMENT_SPIKE_CONFIRMED_PAIRS.items()
    }
    for profile in (STREAM_PROFILE, API_PROFILE):
        assert profile.spike_confirm.mode is SpikeConfirmMode.LAG
        assert profile.spike_confirm.steps == max(0, int(cp.ENRICHMENT_SPIKE_CONFIRM_LAG))
        assert _pairs(profile) == expected_pairs
        assert profile.humidity_spike_enabled is ("humidity" in cp.ENRICHMENT_SPIKE_CONFIRMED_PAIRS)

    assert STREAM_PROFILE.lstm_decision.rule is LstmDecisionRule.FIXED
    assert STREAM_PROFILE.lstm_decision.threshold == runtime_builder.LIVE_ANOMALY_THRESHOLD
    assert STREAM_PROFILE.lstm_decision.threshold == scoring.LIVE_ANOMALY_THRESHOLD
    assert API_PROFILE.lstm_decision.rule is LstmDecisionRule.ADAPTIVE_EWMA_POT
    assert API_PROFILE.lstm_decision.threshold == cp.ANOMALY_THRESHOLD


def test_legacy_fields_mirror_config_one_to_one() -> None:
    legacy_fields = {
        f.name for f in dataclasses.fields(ScoringProfile) if f.name.startswith("legacy_")
    }
    legacy_constants = {
        name.lower() for name in vars(config) if name.startswith("LEGACY_")
    }
    assert legacy_fields == legacy_constants
    for field_name in legacy_fields:
        table = getattr(config, field_name.upper())
        assert set(table) == set(PROFILES)
        for profile_name, profile in PROFILES.items():
            value = getattr(profile, field_name)
            # Enum fields compare by their .value; bool fields directly.
            assert getattr(value, "value", value) == table[profile_name], (
                profile_name,
                field_name,
            )


def test_profiles_and_context_are_immutable() -> None:
    ctx = ScoringContext(
        tenant_id="t1",
        greenhouse_id="gh1",
        sequence_timestamp=datetime(2026, 7, 2, 2, 0, tzinfo=timezone.utc),
        profile=STREAM_PROFILE,
        climate={"setpoint_temp": 21.0},
        sensor_pairs=({"a": "temperature", "b": "temperature_2"},),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        BATCH_PROFILE.oob_policy = API_PROFILE.oob_policy  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        BATCH_PROFILE.spike_confirm.steps = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.sequence_timestamp = datetime(2030, 1, 1)  # type: ignore[misc]
    with pytest.raises(TypeError):
        ctx.climate["setpoint_temp"] = 0.0  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.sensor_pairs[0]["a"] = "humidity"  # type: ignore[index]
    with pytest.raises(TypeError):
        PROFILES["batch"] = API_PROFILE  # type: ignore[index]
    assert isinstance(BATCH_PROFILE.spike_pairs, tuple)


def test_context_requires_a_timestamp_with_no_wall_clock_default() -> None:
    sequence_field = {f.name: f for f in dataclasses.fields(ScoringContext)}["sequence_timestamp"]
    assert sequence_field.default is dataclasses.MISSING
    assert sequence_field.default_factory is dataclasses.MISSING
    with pytest.raises(TypeError):
        ScoringContext(tenant_id="t1", greenhouse_id="gh1", profile=API_PROFILE)  # type: ignore[call-arg]
    for bad in (None, "2026-07-02T02:00:00Z", 1_782_957_600.0):
        with pytest.raises(TypeError):
            ScoringContext(
                tenant_id="t1",
                greenhouse_id="gh1",
                sequence_timestamp=bad,  # type: ignore[arg-type]
                profile=API_PROFILE,
            )
