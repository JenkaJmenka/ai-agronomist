"""ADR-014 step A7 (ml half): the upload stream detectors as core stages.

The upload path scores a file portion by portion. Four of its detectors need
more than one portion's rows, so they carry state across the boundary. A3
left them in ``src/api/services/upload_portions.py``; this module is their
core form, copied unchanged in behaviour:

* **OOB clamp-and-mark** (§1 row 2, ``oob_policy = HIT_AND_CLAMP``): one hit
  per out-of-limit reading, then the readings are clamped for every later
  stage. Per row, so it carries nothing.
* **Duration stuck** (§1 row 18): ``WallclockStuckDetector``. "Wall clock"
  is historical naming: the freeze duration is measured on the rows' own
  timestamps (``_parse_timestamp``), never on the machine clock. Carry:
  ``StuckStageState`` (a canonical ``StuckCarry.to_dict()`` payload).
* **Rolling z** (§1 row 17): ``ColdRunStatisticalDetector.detect_rolling``
  in incremental mode, on rows whose frozen readings are excluded from the
  baseline. Carry: ``RollingZState``.
* **Batch spike look-ahead** (§1 row 9): ``votes.spike_mad_rows`` over the
  previous portion's tail plus this portion. Carry: ``SpikeTailState``.

Every stage is pure: state in, new state out, the input state is never
mutated, no I/O, no clock. ``score_upload_stream`` runs them in the
load-bearing order of ``score_portion`` (OOB, stuck, baseline exclusion,
rolling z, spike). ``tests/models/test_scoring_stream_stages_a7.py`` proves
outputs and carried state equal to ``score_portion`` portion by portion.
Until the backend half of A7 switches the caller, the originals stay in
``upload_portions``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.config import (
    COLD_RUN_STAT_MIN_CONSECUTIVE,
    COLD_RUN_STAT_ROLLING_WINDOW,
    OOB_LIMITS,
    SENSOR_NAMES,
)
from src.models.cold_run_detector import ColdRunStatisticalDetector, RollingAnomaly
from src.models.scoring.context import ScoringContext
from src.models.scoring.profile import OobPolicy, ScoringProfile, SpikeConfirmMode
from src.models.scoring.votes import spike_mad_rows
from src.models.stuck_fault_detector import StuckCarry, WallclockStuckDetector

#: Detector / method / severity names the upload schema carries today.
OOB_DETECTOR: str = "out_of_bounds"
OOB_METHOD: str = "oob"
SPIKE_DETECTOR: str = "spike_mad"
SPIKE_METHOD: str = "mad"
ROW_HIT_SEVERITY: str = "high"

#: Label of a row with no usable timestamp (``upload_portions`` spelling).
UNKNOWN_TIMESTAMP: str = "unknown"
TIMESTAMP_COLUMN: str = "timestamp"

#: Raw rows the rolling-z carry keeps: the rolling window plus
#: ``min_consecutive - 1`` so a streak across a boundary is not severed.
ROLLING_Z_CARRY_ROWS: int = COLD_RUN_STAT_ROLLING_WINDOW + COLD_RUN_STAT_MIN_CONSECUTIVE - 1

# Psychrometric context columns (air temperature, RH, measured deficit).
_PSYCHRO_COLUMNS: Tuple[str, str, str] = ("temperature", "humidity", "measured_hd")


# --- Hits ---------------------------------------------------------------------


@dataclass(frozen=True)
class RowHit:
    """One per-row detection (OOB or spike), already in upload-schema terms.

    Attributes:
        detector: ``out_of_bounds`` or ``spike_mad``.
        method: ``oob`` or ``mad``.
        column: Sensor column.
        timestamp: Row label, spelled as ``upload_portions`` spells it.
        value: The reading (raw for OOB, clamped series value for spike).
        severity: Always ``high`` today.
    """

    detector: str
    method: str
    column: str
    timestamp: str
    value: float
    severity: str = ROW_HIT_SEVERITY


@dataclass(frozen=True)
class StuckAlert:
    """One duration-stuck alert (the ``WallclockStuckDetector`` alert dict)."""

    timestamp: str
    detected_ts: Optional[str]
    channel: str
    severity: str
    detector: str
    value: float
    duration_hours: float
    method: str

    @classmethod
    def from_dict(cls, alert: Mapping[str, Any]) -> "StuckAlert":
        """Build from the detector's alert dict."""
        return cls(**{name: alert[name] for name in cls.__dataclass_fields__})

    def as_dict(self) -> Dict[str, Any]:
        """The alert dict ``upload_portions._stuck_dict_to_high_detail`` reads."""
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


# --- Timestamp labels (exact copies of the two upload spellings) ---------------


def timestamp_label(value: Any) -> str:
    """``upload_portions._timestamp_label``: ISO for datetimes, else ``str``."""
    if value is None:
        return UNKNOWN_TIMESTAMP
    if isinstance(value, float) and math.isnan(value):
        return UNKNOWN_TIMESTAMP
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _oob_label(value: Any) -> str:
    """The OOB spelling (``_detect_oob_hits``): no NaN / None special case."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# --- OOB clamp-and-mark (§1 row 2) ---------------------------------------------


def _require_clamp(profile: ScoringProfile) -> None:
    """Reject a profile whose OOB policy is the window gate."""
    if profile.oob_policy is not OobPolicy.HIT_AND_CLAMP:
        raise ValueError("OOB gate is a window vote (votes.oob_vote); this stage clamps")


def oob_hits(frame: pd.DataFrame, profile: ScoringProfile) -> List[RowHit]:
    """One hit per finite reading outside ``OOB_LIMITS`` (``_detect_oob_hits``).

    Args:
        frame: The portion's new rows, physical units, before clamping.
        profile: A clamp-and-continue profile.

    Returns:
        Hits in (``OOB_LIMITS`` order, row order).
    """
    _require_clamp(profile)
    if frame is None or frame.empty:
        return []
    stamps = frame[TIMESTAMP_COLUMN] if TIMESTAMP_COLUMN in frame.columns else None
    hits: List[RowHit] = []
    for column, (lo, hi) in OOB_LIMITS.items():
        if column not in frame.columns:
            continue
        series = frame[column]
        for idx in range(len(series)):
            try:
                value = float(series.iloc[idx])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and (value < lo or value > hi):
                label = UNKNOWN_TIMESTAMP if stamps is None else _oob_label(stamps.iloc[idx])
                hits.append(RowHit(OOB_DETECTOR, OOB_METHOD, column, label, value))
    return hits


def clamp_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """A copy with the ``OOB_LIMITS`` columns clipped (``_clamp_df_for_scoring``)."""
    result = frame.copy()
    for column, (lo, hi) in OOB_LIMITS.items():
        if column in result.columns:
            result[column] = result[column].clip(lower=lo, upper=hi)
    return result


def clamp_records(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of raw records with finite OOB readings clamped (``_clamp_raw_rows_for_scoring``)."""
    result: List[Dict[str, Any]] = []
    for row in rows:
        clamped = dict(row)
        for column, (lo, hi) in OOB_LIMITS.items():
            raw = clamped.get(column)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and (value < lo or value > hi):
                clamped[column] = max(lo, min(hi, value))
        result.append(clamped)
    return result


def oob_clamp_stage(
    frame: Optional[pd.DataFrame], profile: ScoringProfile
) -> Tuple[List[RowHit], Optional[pd.DataFrame]]:
    """Mark OOB readings, then return the frame every later stage scores.

    The frame is clamped only when there is a hit, as in ``score_portion``
    (a no-hit clip could still change an integer column's dtype).

    Returns:
        ``(hits, scoring_frame)``; ``scoring_frame`` is ``frame`` when there
        is nothing to clamp, ``None`` / empty passes through.
    """
    _require_clamp(profile)
    if frame is None or frame.empty:
        return [], frame
    hits = oob_hits(frame, profile)
    return hits, (clamp_frame(frame) if hits else frame)


# --- Duration stuck (§1 row 18) -------------------------------------------------


@dataclass(frozen=True)
class StuckStageState:
    """Duration-stuck carry as an immutable, JSON-safe value.

    ``payload`` is ``StuckCarry.to_dict()`` encoded as JSON with the two
    set-valued fields sorted, so equal carries give equal payloads. Channel
    order is kept (it fixes the order of flushed alerts).
    """

    payload: str

    @classmethod
    def initial(cls, skip_channels: Sequence[str] = ()) -> "StuckStageState":
        """A fresh carry; ``skip_channels`` are the upload's imputed channels."""
        return cls.from_carry(StuckCarry(skip_channels=frozenset(skip_channels)))

    @classmethod
    def from_carry(cls, carry: StuckCarry) -> "StuckStageState":
        """Snapshot a mutable ``StuckCarry`` (the carry itself is not kept)."""
        return cls.from_dict(carry.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StuckStageState":
        """From the ``StuckCarry.to_dict`` shape (the async portion message field)."""
        canonical = StuckCarry.from_dict(dict(data)).to_dict()
        canonical["emitted_onsets"] = sorted(canonical["emitted_onsets"])
        canonical["skip_channels"] = sorted(canonical["skip_channels"])
        return cls(payload=json.dumps(canonical))

    def to_dict(self) -> Dict[str, Any]:
        """The ``StuckCarry.to_dict`` shape (message format unchanged)."""
        return dict(json.loads(self.payload))

    def to_carry(self) -> StuckCarry:
        """A new mutable ``StuckCarry`` (finalize reads completed / open faults)."""
        return StuckCarry.from_dict(self.to_dict())


def duration_stuck_stage(
    frame: Optional[pd.DataFrame],
    state: StuckStageState,
    profile: ScoringProfile,
) -> Tuple[List[StuckAlert], StuckStageState]:
    """``WallclockStuckDetector.detect_chunk`` on one portion, state in / out.

    Args:
        frame: The portion's (clamped) new rows with a ``timestamp`` column.
        state: The carry before this portion.
        profile: Disabled (``legacy_duration_stuck_enabled`` false) returns
            no alerts and the same state.

    Returns:
        ``(alerts, state_after)``.
    """
    if not profile.legacy_duration_stuck_enabled or frame is None or frame.empty:
        return [], state
    carry = state.to_carry()
    alerts = WallclockStuckDetector().detect_chunk(frame, carry)
    return [StuckAlert.from_dict(a) for a in alerts], StuckStageState.from_carry(carry)


def flush_duration_stuck(
    state: StuckStageState, profile: ScoringProfile
) -> Tuple[List[StuckAlert], StuckStageState]:
    """End of file: alert still-open freezes and finalise the cadence check."""
    if not profile.legacy_duration_stuck_enabled:
        return [], state
    carry = state.to_carry()
    alerts = WallclockStuckDetector().flush(carry)
    return [StuckAlert.from_dict(a) for a in alerts], StuckStageState.from_carry(carry)


# --- Baseline exclusion (feeds rolling z) ---------------------------------------


def _parse_iso_utc(ts: str) -> datetime:
    """``upload_portions._parse_iso_utc``: naive ISO strings are UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _exclusion_windows(carry: StuckCarry) -> List[Tuple[str, datetime, Optional[datetime]]]:
    """``[onset, last_frozen]`` per fired release, ``[onset, +inf)`` per fired open freeze."""
    windows: List[Tuple[str, datetime, Optional[datetime]]] = [
        (fw.channel, fw.onset_dt, fw.last_frozen_dt) for fw in carry.completed_faults
    ]
    for channel, st in carry.channels.items():
        if carry.has_onset_fired(channel, st.onset_ts) and not any(
            fw.channel == channel and fw.onset_ts == st.onset_ts for fw in carry.completed_faults
        ):
            windows.append((channel, st.onset_dt, None))
    return windows


def exclude_frozen_readings(
    rows: Sequence[Mapping[str, Any]], carry: StuckCarry
) -> List[Dict[str, Any]]:
    """Copies of ``rows`` with fired-freeze readings set to NaN.

    Exact copy of ``upload_portions._exclude_faults_from_baseline``'s
    cleaning (exact column name, timestamp parsed once per row, rows whose
    timestamp does not parse are left alone). A freeze that never crossed
    the gate excludes nothing.
    """
    out = [dict(r) for r in rows]
    windows = _exclusion_windows(carry)
    if not windows:
        return out
    for row in out:
        try:
            row_dt = _parse_iso_utc(row.get(TIMESTAMP_COLUMN, ""))
        except (ValueError, AttributeError):
            continue
        for channel, onset_dt, end_dt in windows:
            if channel in row and row_dt >= onset_dt and (end_dt is None or row_dt <= end_dt):
                row[channel] = float("nan")
    return out


# --- Rolling z (§1 row 17) ------------------------------------------------------


def _freeze_rows(rows: Sequence[Mapping[str, Any]]) -> Tuple[Mapping[str, Any], ...]:
    return tuple(MappingProxyType(dict(r)) for r in rows)


@dataclass(frozen=True)
class RollingZState:
    """Rolling-z carry (``upload_portions.StatCarry``) as an immutable value.

    Attributes:
        rows: The last ``ROLLING_Z_CARRY_ROWS`` scored raw rows (clamped,
            frozen readings excluded).
        total_rows: Raw rows seen so far (global index of the next row).
        columns: Columns scored, fixed by the first non-empty portion;
            ``None`` until then.
        seen: ``(timestamp, column)`` already reported (cross-portion dedup).
        psychro_context: ``{timestamp: (T_air, RH, measured_hd)}`` for hit
            timestamps where all three readings are finite.
    """

    rows: Tuple[Mapping[str, Any], ...] = ()
    total_rows: int = 0
    columns: Optional[Tuple[str, ...]] = None
    seen: FrozenSet[Tuple[str, str]] = frozenset()
    psychro_context: Mapping[str, Tuple[float, float, float]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def from_parts(
        cls,
        rows: Sequence[Mapping[str, Any]] = (),
        total_rows: int = 0,
        columns: Optional[Sequence[str]] = None,
        seen: Optional[Sequence[Tuple[str, str]]] = None,
        psychro_context: Optional[Mapping[str, Sequence[float]]] = None,
    ) -> "RollingZState":
        """Build from plain values (e.g. the async message's carry rows)."""
        return cls(
            rows=_freeze_rows(rows),
            total_rows=int(total_rows),
            columns=None if columns is None else tuple(columns),
            seen=frozenset((str(t), str(c)) for t, c in (seen or ())),
            psychro_context=MappingProxyType({
                str(ts): (float(v[0]), float(v[1]), float(v[2]))
                for ts, v in (psychro_context or {}).items()
            }),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Plain values; ``from_dict(to_dict())`` is the same state."""
        return {
            "rows": [dict(r) for r in self.rows],
            "total_rows": self.total_rows,
            "columns": None if self.columns is None else list(self.columns),
            "seen": sorted([t, c] for t, c in self.seen),
            "psychro_context": {ts: list(v) for ts, v in self.psychro_context.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RollingZState":
        """Inverse of ``to_dict``."""
        return cls.from_parts(
            rows=data.get("rows", ()),
            total_rows=data.get("total_rows", 0),
            columns=data.get("columns"),
            seen=[tuple(p) for p in data.get("seen", ())],  # type: ignore[misc]
            psychro_context=data.get("psychro_context"),
        )


def _psychro_entries(
    rows: Sequence[Mapping[str, Any]],
    hit_timestamps: FrozenSet[str],
    known: Mapping[str, Tuple[float, float, float]],
) -> Dict[str, Tuple[float, float, float]]:
    """New ``(T, RH, HD)`` entries (``_extract_psychro_context``), first row wins."""
    found: Dict[str, Tuple[float, float, float]] = {}
    for row in rows:
        ts = row.get(TIMESTAMP_COLUMN, "")
        if ts not in hit_timestamps or ts in known or ts in found:
            continue
        raw = [row.get(c) for c in _PSYCHRO_COLUMNS]
        if any(v is None for v in raw):
            continue
        try:
            vals = tuple(float(v) for v in raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in vals):
            found[ts] = vals  # type: ignore[assignment]
    return found


def rolling_z_stage(
    rows: Sequence[Mapping[str, Any]],
    state: RollingZState,
    profile: ScoringProfile,
    stuck: Optional[StuckStageState] = None,
) -> Tuple[List[RollingAnomaly], RollingZState]:
    """Rolling z over carry + portion (``_detect_stats_incremental``), state in / out.

    With ``stuck`` (the carry AFTER this portion's stuck stage), fired-freeze
    readings are first excluded from both the carried rows and the new rows,
    as ``_exclude_faults_from_baseline`` does. The carried rows are cleaned
    even when the portion has no rows.

    Args:
        rows: The portion's raw records, OOB-clamped (``clamp_records``).
        state: The carry before this portion.
        profile: Disabled (``legacy_rolling_z_enabled`` false) returns no
            hits and the same state.
        stuck: Duration-stuck carry for baseline exclusion, or ``None``.

    Returns:
        ``(new hits, state_after)``; hits already in ``state.seen`` are dropped.
    """
    if not profile.legacy_rolling_z_enabled:
        return [], state
    carried: List[Dict[str, Any]] = [dict(r) for r in state.rows]
    new_rows: List[Dict[str, Any]] = [dict(r) for r in rows]
    if stuck is not None:
        carry = stuck.to_carry()
        carried = exclude_frozen_readings(carried, carry)
        new_rows = exclude_frozen_readings(new_rows, carry)
    if not new_rows:
        return [], RollingZState(
            rows=_freeze_rows(carried), total_rows=state.total_rows, columns=state.columns,
            seen=state.seen, psychro_context=state.psychro_context,
        )
    detector = ColdRunStatisticalDetector()
    columns = state.columns if state.columns is not None else tuple(detector._discover_columns(new_rows))
    combined = carried + new_rows
    found = detector.detect_rolling(
        combined,
        columns=list(columns),
        n_prior_rows=max(0, state.total_rows - len(carried)),
        scored_prefix=len(carried),
    )
    seen = set(state.seen)
    fresh: List[RollingAnomaly] = []
    for hit in found:
        key = (hit.timestamp, hit.column)
        if key not in seen:
            seen.add(key)
            fresh.append(hit)
    psychro = dict(state.psychro_context)
    if fresh:
        psychro.update(_psychro_entries(combined, frozenset(h.timestamp for h in fresh), psychro))
    return fresh, RollingZState(
        rows=_freeze_rows(combined[-ROLLING_Z_CARRY_ROWS:]),
        total_rows=state.total_rows + len(new_rows),
        columns=columns,
        seen=frozenset(seen),
        psychro_context=MappingProxyType(psychro),
    )


# --- Batch spike look-ahead (§1 row 9) -----------------------------------------


@dataclass(frozen=True)
class SpikeTailState:
    """The previous portion's last rows, which the look-ahead spike re-reads.

    Attributes:
        tail_rows: Rows kept (``seq_len - 1`` on upload, the LSTM overlap).
        labels: Each tail row's timestamp label (``timestamp_label``).
        values: Sensor -> tail readings, OOB-clamped.
    """

    tail_rows: int
    labels: Tuple[str, ...] = ()
    values: Mapping[str, Tuple[float, ...]] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Reject a negative tail length or ragged columns."""
        if self.tail_rows < 0:
            raise ValueError(f"tail_rows must be >= 0, got {self.tail_rows}")
        if any(len(v) != len(self.labels) for v in self.values.values()):
            raise ValueError("every tail column must have one value per label")

    @classmethod
    def from_frame(cls, frame: Optional[pd.DataFrame], tail_rows: int) -> "SpikeTailState":
        """From the overlap rows the async message carries (clamped here)."""
        if frame is None or frame.empty:
            return cls(tail_rows=tail_rows)
        tail = clamp_frame(frame).iloc[-tail_rows:] if tail_rows else frame.iloc[0:0]
        stamps = tail[TIMESTAMP_COLUMN].tolist() if TIMESTAMP_COLUMN in tail.columns else [None] * len(tail)
        return cls(
            tail_rows=tail_rows,
            labels=tuple(timestamp_label(t) for t in stamps),
            values=MappingProxyType({
                s: tuple(float(v) for v in tail[s].to_numpy(dtype=float))
                for s in SENSOR_NAMES if s in tail.columns
            }),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Plain values (``from_dict`` inverts it)."""
        return {"tail_rows": self.tail_rows, "labels": list(self.labels),
                "values": {s: list(v) for s, v in self.values.items()}}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpikeTailState":
        """Inverse of ``to_dict``."""
        return cls(
            tail_rows=int(data["tail_rows"]),
            labels=tuple(str(x) for x in data.get("labels", ())),
            values=MappingProxyType({str(s): tuple(float(x) for x in v)
                                     for s, v in data.get("values", {}).items()}),
        )


def spike_lookahead_stage(
    frame: Optional[pd.DataFrame],
    state: SpikeTailState,
    context: ScoringContext,
    profile: ScoringProfile,
) -> Tuple[List[RowHit], SpikeTailState]:
    """Confirmed look-ahead spikes for one portion (``_detect_upload_mad_spikes``).

    A tail row is reported only when its first reverting row is new, so a
    spike confirmed across the boundary is reported once. A frame with no
    ``timestamp`` column gives no hits (as today); its tail labels are
    ``unknown``.

    Args:
        frame: The portion's scoring frame (``oob_clamp_stage`` output).
        state: The tail before this portion.
        context: Scoring context (unused by the batch stage today).
        profile: A look-ahead profile.

    Returns:
        ``(hits, state_after)``; an empty frame leaves the state unchanged.
    """
    if profile.spike_confirm.mode is not SpikeConfirmMode.LOOK_AHEAD:
        raise ValueError("lag spike confirmation is a window vote: use spike_mad_vote")
    if frame is None or frame.empty:
        return [], state
    n_tail = len(state.labels)
    has_ts = TIMESTAMP_COLUMN in frame.columns
    new_labels = [timestamp_label(t) for t in frame[TIMESTAMP_COLUMN].tolist()] if has_ts \
        else [UNKNOWN_TIMESTAMP] * len(frame)
    labels = list(state.labels) + new_labels
    columns: Dict[str, np.ndarray] = {}
    for sensor in SENSOR_NAMES:
        if sensor not in frame.columns and sensor not in state.values:
            continue
        old = np.asarray(state.values.get(sensor, (math.nan,) * n_tail), dtype=float)
        new = frame[sensor].to_numpy(dtype=float) if sensor in frame.columns \
            else np.full(len(frame), math.nan)
        lo, hi = OOB_LIMITS.get(sensor, (-math.inf, math.inf))
        columns[sensor] = np.concatenate([np.clip(old, lo, hi), new])
    hits: List[RowHit] = [
        RowHit(SPIKE_DETECTOR, SPIKE_METHOD, r.column, labels[r.index], r.value)
        for r in spike_mad_rows(columns, context, profile, n_tail)
    ] if has_ts else []
    keep = state.tail_rows
    next_state = SpikeTailState(
        tail_rows=keep,
        labels=tuple(labels[-keep:]) if keep else (),
        values=MappingProxyType({
            s: tuple(float(x) for x in (v[-keep:] if keep else v[0:0])) for s, v in columns.items()
        }),
    )
    return hits, next_state


# --- The portion driver ---------------------------------------------------------


@dataclass(frozen=True)
class UploadStreamState:
    """Everything the upload stream stages carry from one portion to the next.

    ``stuck`` is ``None`` when duration-stuck is not run (``score_portion``
    with no ``stuck_carry``): then no baseline exclusion happens either.
    """

    rolling_z: RollingZState
    spike_tail: SpikeTailState
    stuck: Optional[StuckStageState] = None

    @classmethod
    def initial(cls, seq_len: int, skip_channels: Sequence[str] = ()) -> "UploadStreamState":
        """Start of a file: empty carries, tail of ``seq_len - 1`` rows."""
        return cls(
            rolling_z=RollingZState(),
            spike_tail=SpikeTailState(tail_rows=max(0, seq_len - 1)),
            stuck=StuckStageState.initial(skip_channels),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Plain, JSON-safe values (NaN allowed in rolling-z rows)."""
        return {"rolling_z": self.rolling_z.to_dict(), "spike_tail": self.spike_tail.to_dict(),
                "stuck": None if self.stuck is None else self.stuck.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UploadStreamState":
        """Inverse of ``to_dict``."""
        stuck = data.get("stuck")
        return cls(
            rolling_z=RollingZState.from_dict(data["rolling_z"]),
            spike_tail=SpikeTailState.from_dict(data["spike_tail"]),
            stuck=None if stuck is None else StuckStageState.from_dict(stuck),
        )


@dataclass(frozen=True)
class UploadStreamOutput:
    """One portion's stream-stage output.

    Attributes:
        oob_hits: Raw-value OOB hits on the new rows.
        stuck_alerts: Duration-stuck alerts fired in this portion.
        rolling_z_hits: New rolling-z hits (all severities, as today).
        spike_hits: Confirmed look-ahead spikes, tail rows included once.
        scoring_frame: The (clamped) new rows the LSTM sequences are cut
            from, or the input when ``None`` / empty.
    """

    oob_hits: Tuple[RowHit, ...]
    stuck_alerts: Tuple[StuckAlert, ...]
    rolling_z_hits: Tuple[RollingAnomaly, ...]
    spike_hits: Tuple[RowHit, ...]
    scoring_frame: Optional[pd.DataFrame]


def score_upload_stream(
    frame: Optional[pd.DataFrame],
    raw_rows: Sequence[Mapping[str, Any]],
    state: UploadStreamState,
    context: ScoringContext,
    profile: ScoringProfile,
) -> Tuple[UploadStreamOutput, UploadStreamState]:
    """Run the four stream stages on one portion in ``score_portion``'s order.

    OOB is marked on the new rows and clamped; duration stuck runs FIRST on
    the clamped rows so its fired windows are known before rolling z
    excludes them; the spike look-ahead runs on the tail plus the clamped
    rows. The LSTM is not here (``ModelScorer`` / ``lstm``).

    Args:
        frame: The portion's adapter-normalised rows (physical units), or
            ``None`` when the portion failed normalisation.
        raw_rows: The portion's raw records (``_strip_raw_records``), unclamped.
        state: Carries before this portion.
        context: Scoring context.
        profile: ``BATCH_PROFILE`` (look-ahead spike, clamp-and-continue).

    Returns:
        ``(output, state_after)``. ``state`` is not modified.
    """
    oob, scoring = oob_clamp_stage(frame, profile)
    stuck_alerts: List[StuckAlert] = []
    stuck = state.stuck
    if stuck is not None:
        stuck_alerts, stuck = duration_stuck_stage(scoring, stuck, profile)
    z_hits, rolling = rolling_z_stage(clamp_records(raw_rows), state.rolling_z, profile, stuck)
    spikes, tail = spike_lookahead_stage(scoring, state.spike_tail, context, profile)
    output = UploadStreamOutput(
        oob_hits=tuple(oob), stuck_alerts=tuple(stuck_alerts), rolling_z_hits=tuple(z_hits),
        spike_hits=tuple(spikes), scoring_frame=scoring,
    )
    return output, UploadStreamState(rolling_z=rolling, spike_tail=tail, stuck=stuck)
