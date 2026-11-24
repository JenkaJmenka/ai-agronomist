"""ADR-014 step A2: the three production scoring profiles.

``BATCH_PROFILE`` (upload, sync and async), ``STREAM_PROFILE`` (live) and
``API_PROFILE`` (the inference routes) reproduce TODAY's behaviour of each
path (docs/architecture/ADR-014-one-scoring-path.md §2.3). Every number is
read from ``src.config``: intentional fields from the constants the paths
read today, ``legacy_*`` fields from the ``LEGACY_*`` dicts.

Nothing reads these profiles yet (A2), so no output changes. When the core
is wired in (A3-A8) a path's profile must keep its output byte-identical to
the parity snapshot (tests/fixtures/scoring_parity_snapshot.json).

Note on ``API_PROFILE.lstm_decision``: it describes ``/comprehensive`` and
``/batch`` (max(EWMA, POT), fallback ``ANOMALY_THRESHOLD``). ``/api/inference``
is LSTM-only at a fixed ``ANOMALY_THRESHOLD`` and does not run the rules;
whether it joins the core is OQ6.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Tuple

from src import config
from src.models.scoring.profile import (
    ExplainPolicy,
    LegacyEpisodeGrouping,
    LegacyLabelling,
    LegacyNormalisation,
    LegacyPrecedence,
    LegacyWallclock,
    LstmDecision,
    LstmDecisionRule,
    NoiseBaseline,
    OobPolicy,
    ScoringProfile,
    SpikeConfirm,
    SpikeConfirmMode,
    SpikePair,
)

BATCH: str = "batch"
STREAM: str = "stream"
API: str = "api"


def _legacy_fields(name: str) -> dict[str, object]:
    """Return the ``legacy_*`` field values for profile ``name``.

    Args:
        name: ``batch``, ``stream`` or ``api``.

    Returns:
        Keyword arguments for ``ScoringProfile``. An unknown enum string in
        ``src.config`` raises ``ValueError`` here, at import.
    """
    return {
        "legacy_normalisation": LegacyNormalisation(config.LEGACY_NORMALISATION[name]),
        "legacy_duplicate_lstm_forward": config.LEGACY_DUPLICATE_LSTM_FORWARD[name],
        "legacy_rules_behind_lstm": config.LEGACY_RULES_BEHIND_LSTM[name],
        "legacy_window_rules_enabled": config.LEGACY_WINDOW_RULES_ENABLED[name],
        "legacy_spike_floor_seasonal_scaling": config.LEGACY_SPIKE_FLOOR_SEASONAL_SCALING[name],
        "legacy_sensor_pairs_supported": config.LEGACY_SENSOR_PAIRS_SUPPORTED[name],
        "legacy_pipeline_per_episode": config.LEGACY_PIPELINE_PER_EPISODE[name],
        "legacy_wallclock": LegacyWallclock(config.LEGACY_WALLCLOCK[name]),
        "legacy_rolling_z_enabled": config.LEGACY_ROLLING_Z_ENABLED[name],
        "legacy_duration_stuck_enabled": config.LEGACY_DURATION_STUCK_ENABLED[name],
        "legacy_precedence": LegacyPrecedence(config.LEGACY_PRECEDENCE[name]),
        "legacy_labelling": LegacyLabelling(config.LEGACY_LABELLING[name]),
        "legacy_episode_grouping": LegacyEpisodeGrouping(config.LEGACY_EPISODE_GROUPING[name]),
        "legacy_drift_watch_hook": config.LEGACY_DRIFT_WATCH_HOOK[name],
        "legacy_tracker_key_tenant_only": config.LEGACY_TRACKER_KEY_TENANT_ONLY[name],
    }


def _batch_spike_pairs() -> Tuple[SpikePair, ...]:
    """Upload confirmed pairs: one shared c, a floor per keyed sensor (row 9)."""
    return tuple(
        SpikePair(
            sensor=str(sensor),
            tuning_c=float(config.UPLOAD_SPIKE_CONFIRMED_TUNING_C),
            floor=float(floor),
        )
        for sensor, floor in config.UPLOAD_SPIKE_CONFIRMED_FLOOR.items()
    )


def _stream_spike_pairs() -> Tuple[SpikePair, ...]:
    """Enrichment confirmed pairs, one (c, floor) per sensor (row 9)."""
    return tuple(
        SpikePair(sensor=str(sensor), tuning_c=float(c), floor=float(floor))
        for sensor, (c, floor) in config.ENRICHMENT_SPIKE_CONFIRMED_PAIRS.items()
    )


BATCH_PROFILE: ScoringProfile = ScoringProfile(
    name=BATCH,
    spike_confirm=SpikeConfirm(
        mode=SpikeConfirmMode.LOOK_AHEAD, steps=int(config.UPLOAD_SPIKE_REVERT_HORIZON)
    ),
    spike_pairs=_batch_spike_pairs(),
    spike_unpaired_fallback=False,
    lstm_decision=LstmDecision(
        rule=LstmDecisionRule.FILE_PERCENTILE_ROBUST,
        percentile=float(config.COLD_RUN_PERCENTILE),
        robust_gate_z=(
            float(config.COLD_RUN_ROBUST_GATE_Z) if config.COLD_RUN_ROBUST_GATE_ENABLED else None
        ),
    ),
    oob_policy=OobPolicy.HIT_AND_CLAMP,
    explain_policy=ExplainPolicy.NONE,
    noise_baseline=NoiseBaseline.NONE,
    climate_context=False,
    **_legacy_fields(BATCH),  # type: ignore[arg-type]
)

STREAM_PROFILE: ScoringProfile = ScoringProfile(
    name=STREAM,
    spike_confirm=SpikeConfirm(
        mode=SpikeConfirmMode.LAG, steps=max(0, int(config.ENRICHMENT_SPIKE_CONFIRM_LAG))
    ),
    spike_pairs=_stream_spike_pairs(),
    spike_unpaired_fallback=True,
    lstm_decision=LstmDecision(
        rule=LstmDecisionRule.FIXED, threshold=float(config.LIVE_ANOMALY_THRESHOLD)
    ),
    oob_policy=OobPolicy.GATE,
    explain_policy=ExplainPolicy.EPISODE_START,
    noise_baseline=NoiseBaseline.RING_CALIBRATED,
    climate_context=False,
    **_legacy_fields(STREAM),  # type: ignore[arg-type]
)

API_PROFILE: ScoringProfile = ScoringProfile(
    name=API,
    spike_confirm=SpikeConfirm(
        mode=SpikeConfirmMode.LAG, steps=max(0, int(config.ENRICHMENT_SPIKE_CONFIRM_LAG))
    ),
    spike_pairs=_stream_spike_pairs(),
    spike_unpaired_fallback=True,
    lstm_decision=LstmDecision(
        rule=LstmDecisionRule.ADAPTIVE_EWMA_POT, threshold=float(config.ANOMALY_THRESHOLD)
    ),
    oob_policy=OobPolicy.GATE,
    explain_policy=ExplainPolicy.EVERY_CALL,
    noise_baseline=NoiseBaseline.FIXED,
    climate_context=True,
    **_legacy_fields(API),  # type: ignore[arg-type]
)

#: The three profiles by name.
PROFILES: Mapping[str, ScoringProfile] = MappingProxyType(
    {BATCH: BATCH_PROFILE, STREAM: STREAM_PROFILE, API: API_PROFILE}
)
