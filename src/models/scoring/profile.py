"""ADR-014 step A2: the scoring profile type.

``ScoringProfile`` carries every per-path difference of the scoring core as
a named field (docs/architecture/ADR-014-one-scoring-path.md §2.3). Fields
without a prefix are the INTENTIONAL differences of the ADR's §2.3 table.
Fields named ``legacy_*`` are ACCIDENTAL differences from the §1 table: they
hold today's value so the core can reproduce each path exactly during Phase
A, and each Phase B convergence PR flips one of them and deletes it.

Every field cites its §1 row. The three instances are in ``profiles.py``.

Nothing calls these types yet (A2). This module imports nothing from
``src.api``, ``src.db`` or ``src.live`` and never reads a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class SpikeConfirmMode(str, Enum):
    """How a MAD spike is confirmed by reversion (§1 row 9)."""

    #: The whole file is present: a sample is confirmed by one of the next
    #: ``steps`` samples (upload, ``UPLOAD_SPIKE_REVERT_HORIZON``).
    LOOK_AHEAD = "look_ahead"
    #: A stream: sample ``t - steps`` votes, confirmed by the ``steps``
    #: samples after it (``ENRICHMENT_SPIKE_CONFIRM_LAG``).
    LAG = "lag"


class LstmDecisionRule(str, Enum):
    """Which policy turns an LSTM score into a flag (§1 row 5, OQ2)."""

    #: Per-file COLD_RUN_PERCENTILE threshold, raised by the robust gate.
    FILE_PERCENTILE_ROBUST = "file_percentile_robust"
    #: One configured constant.
    FIXED = "fixed"
    #: max(EWMA, POT) from the tracker state, falling back to a constant.
    ADAPTIVE_EWMA_POT = "adaptive_ewma_pot"


class OobPolicy(str, Enum):
    """What an out-of-bounds reading does (§1 row 2)."""

    #: Per-row hit, clamp the value, keep scoring the other rows (batch).
    HIT_AND_CLAMP = "hit_and_clamp"
    #: The window short-circuits to an out_of_bounds result.
    GATE = "gate"


class ExplainPolicy(str, Enum):
    """When the explain stage runs for a flagged timestamp (§1 row 23)."""

    NONE = "none"
    EPISODE_START = "episode_start"
    EVERY_CALL = "every_call"


class NoiseBaseline(str, Enum):
    """Where the noise detector's baseline comes from (§1 row 8)."""

    #: No noise vote on this path.
    NONE = "none"
    #: Calibrated from the live ring buffer.
    RING_CALIBRATED = "ring_calibrated"
    #: The fixed configured baseline only.
    FIXED = "fixed"


class LegacyNormalisation(str, Enum):
    """How the LSTM input is normalised today (§1 row 1, until B5)."""

    FILE_ADAPTIVE = "file_adaptive"
    FIXED_SENSOR_RANGES = "fixed_sensor_ranges"
    CALLER_NORMALISED = "caller_normalised"


class LegacyWallclock(str, Enum):
    """When the pipeline reads the wall clock (§1 rows 12, 16, 26, until B1)."""

    NEVER = "never"
    ALWAYS = "always"
    WHEN_TIMESTAMP_ABSENT = "when_timestamp_absent"


class LegacyPrecedence(str, Enum):
    """Which precedence model picks the label (§1 row 20, until A4/A8)."""

    UPLOAD_RANK_KEY = "upload_rank_key"
    RULE_PRECEDENCE = "rule_precedence"


class LegacyLabelling(str, Enum):
    """When and where ``fault_type`` is produced (§1 row 21, until A8)."""

    READ_TIME_TRIGGER_MAP = "read_time_trigger_map"
    DEFERRED_ANOMALY_DETAIL = "deferred_anomaly_detail"
    IN_RESPONSE = "in_response"


class LegacyEpisodeGrouping(str, Enum):
    """How flags become episodes (§1 row 22)."""

    UPLOAD_GROUPER = "upload_grouper"
    IN_MEMORY_LSTM_EPISODE = "in_memory_lstm_episode"
    NONE = "none"


@dataclass(frozen=True)
class SpikeConfirm:
    """MAD spike reversion confirmation (§1 row 9).

    Attributes:
        mode: Look-ahead (batch) or lag (stream / API).
        steps: Horizon or lag in samples. 0 means confirmation is off and
            the path falls back to its unconfirmed pair.
    """

    mode: SpikeConfirmMode
    steps: int

    def __post_init__(self) -> None:
        """Reject a wrong mode type or a negative step count."""
        if not isinstance(self.mode, SpikeConfirmMode):
            raise TypeError(f"mode must be SpikeConfirmMode, got {type(self.mode).__name__}")
        if isinstance(self.steps, bool) or not isinstance(self.steps, int) or self.steps < 0:
            raise ValueError(f"steps must be an int >= 0, got {self.steps!r}")


@dataclass(frozen=True)
class SpikePair:
    """Confirmed MAD (tuning constant, floor) for one sensor (§1 row 9).

    Attributes:
        sensor: Canonical sensor name.
        tuning_c: MAD multiplier.
        floor: Minimum MAD in the sensor's physical unit.
    """

    sensor: str
    tuning_c: float
    floor: float


@dataclass(frozen=True)
class LstmDecision:
    """LSTM decision rule and the constants it reads (§1 row 5).

    Attributes:
        rule: The policy.
        threshold: The fixed threshold (``FIXED``) or the fallback used when
            the trackers are unavailable or not finite
            (``ADAPTIVE_EWMA_POT``). ``None`` for the per-file rule.
        percentile: Per-file quantile (``FILE_PERCENTILE_ROBUST`` only).
        robust_gate_z: Robust-gate z, or ``None`` when the gate is disabled
            (``FILE_PERCENTILE_ROBUST`` only).
    """

    rule: LstmDecisionRule
    threshold: Optional[float] = None
    percentile: Optional[float] = None
    robust_gate_z: Optional[float] = None

    def __post_init__(self) -> None:
        """Check that each rule carries exactly the constants it needs."""
        if not isinstance(self.rule, LstmDecisionRule):
            raise TypeError(f"rule must be LstmDecisionRule, got {type(self.rule).__name__}")
        if self.rule is LstmDecisionRule.FILE_PERCENTILE_ROBUST:
            if self.percentile is None or self.threshold is not None:
                raise ValueError("file_percentile_robust needs a percentile and no threshold")
        else:
            if self.threshold is None:
                raise ValueError(f"{self.rule.value} needs a threshold")
            if self.percentile is not None or self.robust_gate_z is not None:
                raise ValueError(f"{self.rule.value} takes no percentile or robust gate")


@dataclass(frozen=True)
class ScoringProfile:
    """Every per-path difference of the scoring core, as named fields.

    Intentional fields come from the ADR-014 §2.3 table; ``legacy_*`` fields
    are §1 accidental (or undecided) differences holding today's value. The
    ``# row N`` comments cite ADR-014 §1.
    """

    #: Profile name: ``batch`` (upload), ``stream`` (live) or ``api``.
    name: str

    # --- Intentional differences (ADR-014 §2.3) -------------------------
    spike_confirm: SpikeConfirm  # row 9: look-ahead horizon 3 vs lag 1
    #: Confirmed (c, floor) per sensor. A sensor's presence here enables its
    #: confirmed vote: humidity is absent on batch (B2 measured), present
    #: on stream / API. Used only when ``spike_confirm.steps > 0``.
    spike_pairs: Tuple[SpikePair, ...]  # row 9: temperature vs temperature + humidity
    #: Stream / API also vote a sensor with no confirmed pair, with the
    #: unconfirmed upload c and a scaled floor (light excluded); batch skips
    #: a sensor with no floor key.
    spike_unpaired_fallback: bool  # row 9
    lstm_decision: LstmDecision  # row 5 (policy pending OQ2 / B9)
    oob_policy: OobPolicy  # row 2: clamp-and-continue is intentional for batch
    explain_policy: ExplainPolicy  # row 23
    noise_baseline: NoiseBaseline  # row 8: calibration source intentional
    #: Setpoint / climate rules receive ``climate_context``. Live is off
    #: on purpose (P2.5 blocked on data); upload is off by accident and is
    #: covered by ``legacy_window_rules_enabled``.
    climate_context: bool  # row 6

    # --- Accidental differences held at today's value (ADR-014 §2.3) -----
    legacy_normalisation: LegacyNormalisation  # row 1 (until B5)
    legacy_duplicate_lstm_forward: bool  # row 4 (MSE copy gone in A6; 2nd pass until B2/B4)
    legacy_rules_behind_lstm: bool  # rows 2, 6-9, 12-13, 18 (until B4)
    legacy_window_rules_enabled: bool  # rows 6, 7, 8, 12, 13 (B7)
    legacy_spike_floor_seasonal_scaling: bool  # row 9 (until B8)
    legacy_sensor_pairs_supported: bool  # row 11
    legacy_pipeline_per_episode: bool  # rows 12, 15, 25 (until B2)
    legacy_wallclock: LegacyWallclock  # rows 12, 16, 26 (until B1)
    legacy_rolling_z_enabled: bool  # row 17 (core stage since A7)
    legacy_duration_stuck_enabled: bool  # row 18 (core stage since A7; B4)
    legacy_precedence: LegacyPrecedence  # row 20 (A4 / A8)
    legacy_labelling: LegacyLabelling  # row 21 (A8)
    legacy_episode_grouping: LegacyEpisodeGrouping  # row 22
    legacy_drift_watch_hook: bool  # row 24 (until B6)
    legacy_tracker_key_tenant_only: bool  # row 25 (until B3)

    def __post_init__(self) -> None:
        """Reject a non-tuple pair table or a duplicate sensor in it."""
        if not isinstance(self.spike_pairs, tuple):
            raise TypeError("spike_pairs must be a tuple so the profile stays immutable")
        sensors = [pair.sensor for pair in self.spike_pairs]
        if len(sensors) != len(set(sensors)):
            raise ValueError(f"duplicate sensor in spike_pairs: {sensors}")

    def spike_pair(self, sensor: str) -> Optional[SpikePair]:
        """Return the confirmed spike pair for ``sensor``, or ``None``.

        Args:
            sensor: Canonical sensor name.

        Returns:
            The pair, or ``None`` when the sensor has no confirmed pair.
        """
        for pair in self.spike_pairs:
            if pair.sensor == sensor:
                return pair
        return None

    @property
    def humidity_spike_enabled(self) -> bool:
        """Whether humidity has a confirmed spike vote on this path (row 9)."""
        return self.spike_pair("humidity") is not None
