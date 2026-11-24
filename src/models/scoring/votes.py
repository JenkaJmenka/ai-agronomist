"""ADR-014 step A3: the per-window detector votes as pure core functions.

Each vote is copied, unchanged in behaviour, from
``ComprehensiveInferencePipeline._run_with_components``
(``src/api/services/comprehensive_pipeline.py``, steps 0a noise, 0b OOB,
0c spike, 0f diurnal, 0h BOCPD) or, for the batch look-ahead spike, from
``upload_portions._detect_upload_mad_spikes``. Setpoint / flatline, band and
dual-sensor votes are in ``window_rules``.
The originals stay where they are until A4 / A7 switch their callers;
``tests/models/test_scoring_core_a3.py`` proves the copies equal on the
parity corpus and on seeded random windows.

Every vote has the signature ``(window, context, profile, state)``:

* ``window`` is the ``(seq_len, n_sensors)`` physical-unit window, columns in
  ``SENSOR_NAMES`` order (the pipeline's ``_denormed_for_noise``);
* ``context`` gives the data clock (``sequence_timestamp``) and history;
* ``profile`` selects the per-path behaviour;
* ``state`` is the ``WindowState`` derived once per window from the context
  (``window_state``); ``None`` derives it on the spot.

No I/O, no wall clock, no module-level caches: detectors are built per call.
Precedence between votes is NOT here (A4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from src.config import (
    BOCPD_MIN_SENSORS,
    NOISE_CALIBRATED_RATIO_BY_SENSOR,
    NOISE_CALIBRATED_RATIO_TARGET,
    SENSOR_NAMES,
    SENSOR_NOISE_TARGETS,
    SPIKE_MAD_FLOOR,
    UPLOAD_SPIKE_MAD_FLOOR,
    UPLOAD_SPIKE_MAD_TUNING_C,
)
from src.models.bocpd_detector import BOCPDDetector
from src.models.diurnal_profile_detector import DiurnalProfileDetector
from src.models.noise_detector import (
    NoiseDetector,
    calibrate_noise_baseline,
    resolve_noise_baseline,
)
from src.models.operational_config_store import default_operational_config
from src.models.out_of_bounds_detector import OutOfBoundsDetector
from src.models.scoring.context import ScoringContext
from src.models.scoring.profile import NoiseBaseline, ScoringProfile, SpikeConfirmMode
from src.models.seasonal_scaler import SeasonalScaler
from src.models.spike_mad_detector import SpikeMADDetector

#: Sensor the noise, diurnal, BOCPD and spike votes skip: the diurnal ramp
#: and night zeros are the natural signal, not a probe fault.
LIGHT_SENSOR: str = "light"

#: Season names the scaler knows; any other ``season_map`` value is winter.
_SEASONS: frozenset[str] = frozenset({"summer", "winter"})
_DEFAULT_SEASON: str = "winter"

#: Vote keys (the pipeline's ``rule_votes`` names).
SPIKE_VOTE: str = "spike_mad"
NOISE_VOTE: str = "noise"
OOB_VOTE: str = "out_of_bounds"
CHANGE_POINT_VOTE: str = "change_point"
DIURNAL_VOTE: str = "diurnal"


@dataclass(frozen=True)
class VoteResult:
    """What one detector contributes for one window.

    Attributes:
        votes: Vote key -> affected sensors, in detection order. Empty when
            the detector did not fire. A key is never mapped to ``()``.
        findings: Detector findings that are not votes by themselves
            (``ScadaAnomalyRecord.to_dict()`` for setpoint and band rules).
    """

    votes: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    findings: Tuple[Mapping[str, Any], ...] = ()

    @property
    def fired(self) -> bool:
        """Whether any vote key fired."""
        return bool(self.votes)


def _single(key: str, sensors: Sequence[str]) -> VoteResult:
    """A result with one vote key, or an empty result for no sensors."""
    if not sensors:
        return VoteResult()
    return VoteResult(votes=MappingProxyType({key: tuple(sensors)}))


@dataclass(frozen=True)
class WindowState:
    """Values every vote of one window reads, derived once from the context.

    Attributes:
        season: ``summer`` or ``winter`` for the window's data timestamp.
        scaler: Seasonal sigma scaler (factor 1.0 when there is none).
        operational_config: Effective operational config (code defaults
            merged with the scope's overrides).
        diurnal_profiles: ``{sensor: {hour: (mean, std)}}``; empty when the
            scope has no mature profile for the anchor date.
        noise_clean_windows: Physical clean windows for noise calibration,
            or ``None``.
    """

    season: str
    scaler: SeasonalScaler
    operational_config: Mapping[str, Any]
    diurnal_profiles: Mapping[str, Mapping[int, Tuple[float, float]]]
    noise_clean_windows: Optional[Sequence[Any]]

    def factor(self, sensor: str) -> float:
        """Seasonal scale for ``sensor`` in this window's season."""
        return self.scaler.factor(self.season, sensor)


def season_for_timestamp(ts: datetime, season_map: Mapping[str, str]) -> str:
    """Map a timestamp's month onto summer|winter (``cp._season_for_timestamp``).

    Args:
        ts: Data timestamp.
        season_map: Month number as a string -> season name.

    Returns:
        ``summer`` or ``winter``.
    """
    season = str(season_map.get(str(int(ts.month)), _DEFAULT_SEASON)).lower()
    return season if season in _SEASONS else _DEFAULT_SEASON


def window_state(context: ScoringContext) -> WindowState:
    """Derive the per-window state from the context's prefetched history.

    The history's ``operational_overrides()`` must return the EFFECTIVE
    config (``OperationalConfigStore.get``), not the raw override dict.
    With no history the code defaults apply, the scaler is identity and
    there are no diurnal profiles or clean windows.

    Args:
        context: The scoring context.

    Returns:
        The state.
    """
    history = context.history
    if history is None:
        config: Mapping[str, Any] = default_operational_config()
        scaler: Optional[Any] = None
        profiles: Optional[Mapping[str, Any]] = None
        clean: Optional[Sequence[Any]] = None
    else:
        config = history.operational_overrides()
        scaler = history.seasonal_scaler()
        profiles = history.diurnal_profile(context.sequence_timestamp.date())
        clean = history.noise_clean_windows()
    return WindowState(
        season=season_for_timestamp(context.sequence_timestamp, config.get("season_map") or {}),
        scaler=scaler if scaler is not None else SeasonalScaler(),
        operational_config=config,
        diurnal_profiles=profiles or {},
        noise_clean_windows=clean or None,
    )


def _sensors(window: np.ndarray) -> Sequence[str]:
    """Sensor names of the window's columns."""
    return SENSOR_NAMES[: window.shape[1]]


# --- Spike (MAD), §1 row 9 ---------------------------------------------------


def unpaired_spike_floor(sensor: str) -> Optional[float]:
    """Unconfirmed MAD floor of the stream / API spike vote.

    Copy of ``cp.enrichment_spike_mad_floor``: light never votes; an explicit
    ``UPLOAD_SPIKE_MAD_FLOOR`` key is used as-is; otherwise the live floor is
    scaled by the upload/live temperature ratio. ``None`` = do not vote.

    Args:
        sensor: Canonical sensor name.

    Returns:
        The floor in physical units, or ``None``.
    """
    if sensor == LIGHT_SENSOR:
        return None
    if sensor in UPLOAD_SPIKE_MAD_FLOOR:
        return float(UPLOAD_SPIKE_MAD_FLOOR[sensor])
    live_floor = SPIKE_MAD_FLOOR.get(sensor)
    if live_floor is None:
        return None
    live_temp = float(SPIKE_MAD_FLOOR["temperature"])
    if live_temp <= 0.0:
        return None
    return float(live_floor) * (float(UPLOAD_SPIKE_MAD_FLOOR["temperature"]) / live_temp)


def _lag_spike_pair(sensor: str, profile: ScoringProfile) -> Optional[Tuple[float, float]]:
    """(c, floor) the lag-mode vote uses for ``sensor``, before seasonal scaling."""
    floor = unpaired_spike_floor(sensor)
    if floor is None:
        return None
    pair = profile.spike_pair(sensor)
    if profile.spike_confirm.steps > 0 and pair is not None:
        return float(pair.tuning_c), float(pair.floor)
    if not profile.spike_unpaired_fallback:
        return None
    return float(UPLOAD_SPIKE_MAD_TUNING_C), floor


def spike_mad_vote(
    window: np.ndarray,
    context: ScoringContext,
    profile: ScoringProfile,
    state: Optional[WindowState] = None,
) -> VoteResult:
    """One MAD spike vote per window, at ``t - lag`` (pipeline step 0c).

    With ``lag > 0`` sample ``t - lag`` votes only if one of the ``lag``
    samples after it reverts; with ``lag == 0`` the last sample votes,
    unconfirmed. The floor is seasonally scaled when
    ``legacy_spike_floor_seasonal_scaling`` is set.

    Raises:
        ValueError: For a look-ahead profile; batch spikes are a row stage
            (``spike_mad_rows``), not a window vote.
    """
    if profile.spike_confirm.mode is not SpikeConfirmMode.LAG:
        raise ValueError("look-ahead spike confirmation is a row stage: use spike_mad_rows")
    st = state if state is not None else window_state(context)
    lag = profile.spike_confirm.steps
    alarmed: List[str] = []
    for idx, sensor in enumerate(_sensors(window)):
        pair = _lag_spike_pair(sensor, profile)
        if pair is None:
            continue
        tuning_c, floor = pair
        if profile.legacy_spike_floor_seasonal_scaling:
            floor = floor * st.factor(sensor)
        detector = SpikeMADDetector(tuning_c=tuning_c)
        series = window[:, idx]
        if lag > 0:
            offsets = detector.reversion_offsets(series, mad_floor=floor, horizon=lag)
            voted = len(offsets) > lag and offsets[-1 - lag] is not None
        else:
            flags = detector.detect(series, mad_floor=floor)
            voted = bool(flags and flags[-1])
        if voted:
            alarmed.append(sensor)
    return _single(SPIKE_VOTE, alarmed)


@dataclass(frozen=True)
class SpikeRow:
    """One confirmed batch spike: ``column`` at row ``index`` with ``value``."""

    column: str
    index: int
    value: float


def _batch_spike_pairs(profile: ScoringProfile) -> Tuple[Tuple[str, float, float], ...]:
    """(sensor, c, floor) the look-ahead stage scores, in table order."""
    if profile.spike_confirm.steps > 0:
        return tuple((p.sensor, float(p.tuning_c), float(p.floor)) for p in profile.spike_pairs)
    c = float(UPLOAD_SPIKE_MAD_TUNING_C)
    return tuple((str(s), c, float(f)) for s, f in UPLOAD_SPIKE_MAD_FLOOR.items())


def spike_mad_rows(
    columns: Mapping[str, np.ndarray],
    context: ScoringContext,
    profile: ScoringProfile,
    state: int = 0,
) -> List[SpikeRow]:
    """Confirmed look-ahead spikes over a portion (``_detect_upload_mad_spikes``).

    A row flagged by MAD is reported when the series reverts within the
    horizon. Rows before ``state`` (the overlap tail) are reported only when
    their first reverting row is a new row. With horizon 0 the unconfirmed
    upload pair applies. Non-finite values are never reported.

    Args:
        columns: Column name -> physical series of the overlap + portion.
        context: The scoring context (only the clock; unused today).
        profile: A look-ahead profile.
        state: ``new_row_start``, the first row of this portion.

    Returns:
        Hits in (table order, row order).
    """
    del context  # the batch stage reads no history (no seasonal scaling, row 9)
    horizon = profile.spike_confirm.steps
    start = max(0, state)
    hits: List[SpikeRow] = []
    for column, tuning_c, floor in _batch_spike_pairs(profile):
        if column not in columns:
            continue
        values = np.asarray(columns[column], dtype=float)
        detector = SpikeMADDetector(tuning_c=tuning_c)
        if horizon > 0:
            offsets = detector.reversion_offsets(values, mad_floor=floor, horizon=horizon)
            emit = [
                i for i in range(max(0, start - horizon), len(offsets))
                if offsets[i] is not None and (i >= start or i + int(offsets[i] or 0) >= start)
            ]
        else:
            flags = detector.detect(values, mad_floor=floor)
            emit = [i for i in range(start, len(flags)) if flags[i]]
        hits.extend(
            SpikeRow(column=column, index=i, value=float(values[i]))
            for i in emit
            if np.isfinite(values[i])
        )
    return hits


# --- Noise, §1 row 8 ---------------------------------------------------------


def noise_detector_for(state: WindowState, profile: ScoringProfile) -> NoiseDetector:
    """The noise detector for this window (copy of ``cp.build_noise_detector``).

    ``RING_CALIBRATED`` calibrates from the history's clean windows when
    they yield a positive variance; otherwise, and for ``FIXED``, the fixed
    ``NOISE_BASELINE_VARIANCE`` map applies.
    """
    base = NoiseDetector()
    if profile.noise_baseline is not NoiseBaseline.RING_CALIBRATED:
        return base
    if state.noise_clean_windows is None:
        return base
    calibrated = calibrate_noise_baseline(state.noise_clean_windows)
    if not calibrated:
        return base
    return NoiseDetector(
        hp_cutoff=base._hp_cutoff,
        window=base._window,
        baseline_variance=resolve_noise_baseline(calibrated),
        ratio_threshold={
            name: float(NOISE_CALIBRATED_RATIO_BY_SENSOR.get(name, NOISE_CALIBRATED_RATIO_TARGET))
            for name in SENSOR_NAMES
        },
        use_highpass=base._use_highpass,
        default_ratio=float(NOISE_CALIBRATED_RATIO_TARGET),
    )


def noise_vote(
    window: np.ndarray,
    context: ScoringContext,
    profile: ScoringProfile,
    state: Optional[WindowState] = None,
) -> VoteResult:
    """Window variance-ratio noise vote, light excluded (pipeline step 0a)."""
    if profile.noise_baseline is NoiseBaseline.NONE or not profile.legacy_window_rules_enabled:
        return VoteResult()
    st = state if state is not None else window_state(context)
    detector = noise_detector_for(st, profile)
    alarmed: List[str] = []
    for idx, sensor in enumerate(_sensors(window)):
        if sensor == LIGHT_SENSOR:
            continue
        base_target = SENSOR_NOISE_TARGETS.get(sensor)
        target = None if base_target is None else base_target * st.factor(sensor)
        if detector.detect(window[:, idx], sensor, target_override=target):
            alarmed.append(sensor)
    return _single(NOISE_VOTE, alarmed)


# --- Out of bounds, §1 row 2 -------------------------------------------------


def oob_vote(
    window: np.ndarray,
    context: ScoringContext,
    profile: ScoringProfile,
    state: Optional[WindowState] = None,
) -> VoteResult:
    """Sensors with any reading outside ``OOB_LIMITS`` (pipeline step 0b).

    Only the detection. What a hit does (gate the window, or clamp and
    continue on batch) is ``profile.oob_policy``, applied by A4 / A7.
    """
    del context, profile, state  # the limits are the same on every path
    detector = OutOfBoundsDetector()
    alarmed = [
        sensor
        for idx, sensor in enumerate(_sensors(window))
        if any(detector.detect_series(window[:, idx], sensor))
    ]
    return _single(OOB_VOTE, alarmed)


# --- Diurnal, §1 row 12 ------------------------------------------------------


def diurnal_vote(
    window: np.ndarray,
    context: ScoringContext,
    profile: ScoringProfile,
    state: Optional[WindowState] = None,
) -> VoteResult:
    """Last reading vs its hourly profile, light excluded (pipeline step 0f).

    The hour is ``context.sequence_timestamp.hour`` as given (no time-zone
    conversion), as the pipeline reads ``seq_ts.hour``.
    """
    if not profile.legacy_window_rules_enabled:
        return VoteResult()
    st = state if state is not None else window_state(context)
    if not st.diurnal_profiles:
        return VoteResult()
    detector = DiurnalProfileDetector()
    hour = context.sequence_timestamp.hour
    alarmed: List[str] = []
    for idx, sensor in enumerate(_sensors(window)):
        if sensor == LIGHT_SENSOR:
            continue
        hourly = st.diurnal_profiles.get(sensor)
        if hourly and detector.detect(
            float(window[-1, idx]), hour, dict(hourly), sensor_name=sensor
        ):
            alarmed.append(sensor)
    return _single(DIURNAL_VOTE, alarmed)


# --- Change point (BOCPD), §1 row 13 ----------------------------------------


def change_point_vote(
    window: np.ndarray,
    context: ScoringContext,
    profile: ScoringProfile,
    state: Optional[WindowState] = None,
) -> VoteResult:
    """BOCPD change point anywhere in the window on ``BOCPD_MIN_SENSORS``+ sensors.

    Light excluded so nightfall is not a second channel (pipeline step 0h).
    """
    del context, state
    if not profile.legacy_window_rules_enabled:
        return VoteResult()
    detector = BOCPDDetector()
    sensors = [
        sensor
        for idx, sensor in enumerate(_sensors(window))
        if sensor != LIGHT_SENSOR and detector.any_in_window(window[:, idx])
    ]
    if len(sensors) < BOCPD_MIN_SENSORS:
        return VoteResult()
    return _single(CHANGE_POINT_VOTE, sensors)
