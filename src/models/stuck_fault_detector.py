"""
Stuck-fault detector for greenhouse sensors.

Two detection modes:

1. **Sample-indexed** (``StuckFaultDetector``) — rolling-variance analysis
   over fixed-length windows.  Used by ``ComprehensiveInferencePipeline``.

2. **Wall-clock** (``WallclockStuckDetector``) — elapsed-time gate with a
   per-channel epsilon.  Used by the CSV upload path
   (``score_portion`` / ``finalize_scores``).  Correctly handles irregular
   cadence and NaN gaps inside a fault window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Set

import numpy as np
import pandas as pd

from src.config import (
    CADENCE_EXPECTED_SECONDS,
    CADENCE_PIN_MIN_INTERVALS,
    CADENCE_TOLERANCE_FACTOR,
    SENSOR_NAMES,
    STUCK_FLAT_RANGE_THRESHOLD,
    STUCK_WALLCLOCK_CHANNELS,
    STUCK_WALLCLOCK_EPSILON,
    STUCK_WALLCLOCK_MIN_HOURS,
    STUCK_WALLCLOCK_MIN_SAMPLES,
)

logger = logging.getLogger(__name__)

try:
    from src.config import STUCK_EPSILON, STUCK_WINDOW_K, STUCK_VARIANCE_THRESHOLD
except ImportError:  # pragma: no cover
    STUCK_WINDOW_K = 10
    STUCK_VARIANCE_THRESHOLD = 1e-4
    STUCK_EPSILON = 1e-4

_SENSOR_NAMES: list[str] = list(SENSOR_NAMES)


class StuckFaultDetector:
    """Detect sensors stuck at a constant value via rolling variance analysis."""

    def __init__(
        self,
        variance_threshold: float = STUCK_VARIANCE_THRESHOLD,
        window_k: int = STUCK_WINDOW_K,
        flat_range_threshold: float = STUCK_FLAT_RANGE_THRESHOLD,
    ) -> None:
        self._variance_threshold = variance_threshold
        self._window_k = window_k
        self._flat_range_threshold = flat_range_threshold

    def detect(
        self,
        sequence: np.ndarray,
        skip_channels: Optional[Set[str]] = None,
    ) -> dict[str, bool]:
        """Check each sensor channel for stuck-fault condition."""
        if sequence.ndim != 2:
            raise ValueError(
                f"Expected 2D array (seq_len, sensors), got {sequence.ndim}D"
            )

        skip = skip_channels or set()
        num_sensors = min(sequence.shape[1], len(_SENSOR_NAMES))
        results: dict[str, bool] = {}

        seq_active = sequence[:, :num_sensors]
        if sequence.shape[0] >= self._window_k:
            windows = np.lib.stride_tricks.sliding_window_view(seq_active, self._window_k, axis=0)
            variances = np.var(windows, axis=-1, ddof=1)
            min_variances = np.min(variances, axis=0)
        else:
            min_variances = np.var(seq_active, axis=0)

        # Standardize NaNs to 1.0
        min_variances = np.where(np.isnan(min_variances), 1.0, min_variances)

        value_ranges = np.max(seq_active, axis=0) - np.min(seq_active, axis=0)
        is_flats = value_ranges < self._flat_range_threshold

        for idx in range(num_sensors):
            name = _SENSOR_NAMES[idx]
            if name in skip:
                results[name] = False
                continue
            is_flat = bool(is_flats[idx])
            variance = float(min_variances[idx])
            results[name] = bool(
                is_flat and variance < self._variance_threshold
            )
        return results

    @staticmethod
    def detect_series(
        series: np.ndarray,
        w: int = STUCK_WINDOW_K,
        epsilon: float = STUCK_EPSILON,
    ) -> list[bool]:
        """Per-timestep stuck-at detection on a 1-D time series.

        Uses rolling variance with Bessel's correction (``ddof=1``).

        Args:
            series: 1-D array of sensor readings.
            w: Rolling window size.
            epsilon: Minimum variance threshold.

        Returns:
            List of booleans; ``True`` when the rolling variance
            falls below *epsilon*.  The first ``w-1`` entries are
            always ``False`` (insufficient window).
        """
        series = np.asarray(series, dtype=float)
        n = len(series)
        flags: list[bool] = [False] * n

        if n < w or w < 2:
            return flags

        for t in range(w - 1, n):
            window = series[t - w + 1 : t + 1]
            var = float(np.var(window, ddof=1))
            flags[t] = var < epsilon

        return flags


# =========================================================================
# Wall-clock stuck-fault detection (upload path)
# =========================================================================

def _parse_timestamp(ts_val: object) -> datetime | None:
    """Parse a timestamp value into a timezone-aware UTC datetime."""
    if isinstance(ts_val, pd.Timestamp):
        dt = ts_val.to_pydatetime()
    elif isinstance(ts_val, datetime):
        dt = ts_val
    elif isinstance(ts_val, str):
        try:
            dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _canonical_onset_key(channel: str, onset_ts: str) -> tuple[str, str]:
    """Normalise an onset key so equal instants compare equal.

    ``emitted_onsets`` is keyed on a timestamp *string*, which makes naive
    string equality wrong: ``2026-12-18T13:00:00Z`` and
    ``2026-12-18T13:00:00+00:00`` are the same instant but different
    strings, and a miss is indistinguishable from "this freeze never
    fired" — a silent wrong answer rather than an error.  Both inserts and
    lookups go through here, so the set holds one canonical spelling per
    instant and membership stays an O(1) hash lookup.

    The canonical form is ``_parse_timestamp(onset_ts).isoformat()``: UTC
    with an explicit ``+00:00`` offset.  A naive timestamp is therefore
    read as UTC (``_parse_timestamp``'s existing rule for this module, and
    the only reading consistent with every other timestamp here) rather
    than rejected.  An unparseable string falls back to itself, preserving
    today's exact-match behaviour instead of collapsing distinct garbage
    keys together.
    """
    onset_dt = _parse_timestamp(onset_ts)
    if onset_dt is None:
        return (channel, onset_ts)
    return (channel, onset_dt.isoformat())


def expected_slot_count(
    onset_dt: datetime,
    last_frozen_dt: datetime,
    cadence_seconds: float,
) -> int:
    """Slots a channel should have produced over a closed frozen span.

    ``(last_frozen_dt - onset_dt) / cadence_seconds + 1`` — inclusive of
    both endpoints, so a span of zero length is one slot.  This is the
    definition of ``FaultWindow.raw_slot_count`` and of
    ``anomaly_episodes.raw_slot_count``; callers that need the figure for
    a freeze still open must call this rather than re-deriving it, so the
    two answers cannot diverge.

    The quotient is rounded to nearest, not truncated: real cadence
    jitters by a second or two either side of nominal, and truncation
    would shed a whole slot every time the span landed just under an
    exact multiple.

    Raises:
        ValueError: if *cadence_seconds* is not positive, or the span runs
            backwards.  Both are caller bugs that would otherwise produce
            a plausible-looking count.
    """
    if cadence_seconds <= 0:
        raise ValueError(f"cadence_seconds must be positive, got {cadence_seconds}")
    span_seconds = (last_frozen_dt - onset_dt).total_seconds()
    if span_seconds < 0:
        raise ValueError(
            f"frozen span runs backwards: {onset_dt} .. {last_frozen_dt}"
        )
    return int(round(span_seconds / cadence_seconds)) + 1


# Which evidence pinned a freeze's cadence: recorded on
# ``ChannelFreezeState.cadence_branch`` and logged on every pin.  The pinned
# cadence scales ``raw_slot_count``, so "where did this number come from" has
# to be answerable from the state and from the log, never inferred.
CADENCE_BRANCH_PRE_ONSET: str = "pre_onset"
CADENCE_BRANCH_IN_FREEZE: str = "in_freeze"
CADENCE_BRANCH_CARRIED: str = "carried"


def _pre_onset_cadence_seconds(
    chunk_medians: list[float],
    pre_onset_intervals: list[float],
) -> float | None:
    """Pin a freeze's cadence from intervals observed before its onset.

    BRANCH 1 of two.  Returns the median of the pre-onset evidence, or
    ``None`` when there is none — which means "no answer yet", NOT
    ``CADENCE_EXPECTED_SECONDS``.  The caller leaves such a freeze
    unpinned, and branch 2 (``_in_freeze_cadence_seconds``) pins the
    median of the freeze's own intervals when the gate fires.  Either way
    the value lands in ``ChannelFreezeState.cadence_seconds`` once and is
    never recomputed — see that field for why re-deriving it later would
    make ``raw_slot_count`` depend on chunk size.

    The MEASURED median is preferred over ``CADENCE_EXPECTED_SECONDS``:
    the definition is "slots at the channel's nominal cadence", and a file
    logged every 900 s really does owe a third of the readings per hour
    that the platform's 300 s default implies.  Scoring such a file
    against the constant inflates expected slots threefold and invents a
    missing-readings gap that is not there — measured on a 900 s file
    whose freeze opens inside the first three rows: 178 expected slots
    against 60 frozen samples, a fabricated gap of 118 readings that were
    never missing.  ``raw_slot_count - frozen_sample_count`` is read as
    "readings missing inside the freeze", so a fabricated gap is worse
    than no gap — which is why NEITHER branch falls back to the constant.
    ``CADENCE_EXPECTED_SECONDS`` remains a deviation *reference* elsewhere
    in this module — ``flush``'s cadence flag asks whether the measured
    median strayed from it — and is no longer a pinning input.

    Evidence is combined across the whole file, not just the current
    chunk.  Only ``chunk_medians`` survives from completed chunks (the raw
    intervals are discarded by design, for the carry's bounded-memory
    guarantee), so each completed chunk contributes one number, and the
    current chunk contributes the median of its own pre-onset intervals.
    A completed chunk's median is itself a median over up to a full
    portion of intervals, so it counts as evidence on its own; the
    ``CADENCE_PIN_MIN_INTERVALS`` floor applies only to the thin,
    partial sample from the current chunk, which is discarded rather than
    allowed to drag the result when it is too small to be robust.
    """
    evidence = list(chunk_medians)
    if len(pre_onset_intervals) >= CADENCE_PIN_MIN_INTERVALS:
        evidence.append(float(np.median(pre_onset_intervals)))
    if not evidence:
        return None
    pinned = float(np.median(evidence))
    return pinned if pinned > 0 else None


def _in_freeze_cadence_seconds(in_freeze_intervals: list[float]) -> float | None:
    """Pin a freeze's cadence from the intervals observed INSIDE it.

    BRANCH 2 of two, for a freeze that opened with no pre-onset evidence
    (see ``_pre_onset_cadence_seconds``).  Its own intervals are the only
    cadence measurement such a freeze has, and they are a real one: a
    900 s file's freeze observes 900 s intervals whether or not anything
    preceded its onset.

    ``CADENCE_PIN_MIN_INTERVALS`` is deliberately NOT applied here.  That
    floor guards a thin PRE-onset sample, which can be as small as one
    interval; this list is only read at the moment the dual gate fires,
    and firing requires ``STUCK_WALLCLOCK_MIN_SAMPLES`` frozen samples —
    so at least that many minus one intervals have already been observed,
    an order of magnitude above the floor.

    Returns ``None`` only for an empty list, which no freeze reaching the
    gate through ``detect_chunk`` can present (see
    ``_pin_in_freeze_cadence`` for the one path that can).
    """
    if not in_freeze_intervals:
        return None
    pinned = float(np.median(in_freeze_intervals))
    return pinned if pinned > 0 else None


@dataclass
class ChannelFreezeState:
    """Per-channel frozen-value tracking for wall-clock stuck detection.

    One counter, ``sample_count``: finite samples observed at the frozen
    value, maintained by ``WallclockStuckDetector.detect_chunk`` as rows
    are consumed — never derived afterwards from timestamps, which would
    be wrong exactly when cadence is irregular.  ``last_ts`` is by
    definition the last frozen sample, so ``sample_count`` is the
    frozen-sample count of the span ``[onset_ts, last_ts]``.

    This state deliberately holds NO slot counter.  It used to tally the
    file rows the frozen span covered, nulls included, and
    ``FaultWindow.raw_slot_count`` was snapped from that tally — but the
    tally could never show a gap on the production upload path, because
    ``CSVAdapter._handle_nan`` drops rows with a null ``temperature`` or
    ``humidity`` before the detector sees any of them (measured: 70 such
    rows in ``tests/fixtures/native_dec_stuck_fault.csv``, 3457 rows in,
    3387 out).  Counting rows that survived the adapter measures the
    adapter, not the sensor.  ``raw_slot_count`` is now EXPECTED slots at
    the nominal cadence (see ``expected_slot_count``), derived from the
    span's two endpoints; keeping the old tally beside it would leave two
    live counters of the same name meaning different things in one
    module, which is what produced the bug in the first place.

    ``cadence_seconds``
        The cadence used to turn this freeze's span into expected slots,
        pinned ONCE from measured intervals and never recomputed
        afterwards.  Two branches pin it: at onset from the intervals
        observed before it (``_pre_onset_cadence_seconds``), or — when
        the freeze opened too early in the file for there to be any — at
        the instant the gate fires, from the freeze's own intervals
        (``_in_freeze_cadence_seconds``).  It is pinned and carried rather
        than re-derived because in the async upload path a freeze spans
        portions that are scored in separate processes: a cadence
        re-derived per chunk would differ from the one a single pass over
        the same file arrives at, so ``raw_slot_count`` would depend on
        chunk size — a silent correctness bug, not a rounding difference.
        Pinning it once and shipping it with the carry is what makes split
        and single-pass runs agree.

        The field default, ``CADENCE_EXPECTED_SECONDS``, exists so a
        hand-built state (tests) need not supply one.  It is NOT a pinning
        outcome: no freeze that fired through ``detect_chunk`` or
        ``flush`` reports it, because both pinning branches measure.  Read
        ``cadence_branch``, not this field, to tell a pinned cadence from
        an unpinned placeholder.

    ``cadence_branch``
        ``None`` until a cadence is pinned, then one of
        ``CADENCE_BRANCH_PRE_ONSET`` / ``CADENCE_BRANCH_IN_FREEZE`` /
        ``CADENCE_BRANCH_CARRIED`` (the last for a state deserialised from
        a producer that pinned before this label existed).  Authoritative
        for "has this freeze pinned?", which ``cadence_seconds`` cannot
        answer on its own now that an unpinned freeze holds a placeholder
        there.

    ``in_freeze_intervals``
        Row-to-row intervals observed since this freeze opened, kept ONLY
        while ``cadence_branch is None`` — they are branch 2's evidence,
        and ``_pin_in_freeze_cadence`` clears the list the moment it uses
        them.  The list travels per channel in every portion message, so
        its bound matters to ``StuckCarry``'s bounded-memory guarantee:
        it is the dual gate's LATER limb, since pinning happens when the
        gate fires.  That is ``min_hours`` worth of intervals whenever
        cadence is fast enough for ``min_samples`` to arrive first — ~72
        at 300 s, ~24 at 900 s, ~360 at 60 s (6 h / cadence) — and
        ``min_samples - 1`` (11) for a file so sparse that 6 h elapse
        first.  A freeze that NEVER fires is bounded the same way by the
        limb it never crosses, and carries its list to end of file.
    """

    frozen_value: float
    onset_ts: str
    onset_dt: datetime
    last_ts: str
    last_dt: datetime
    sample_count: int
    cadence_seconds: float = float(CADENCE_EXPECTED_SECONDS)
    cadence_branch: str | None = None
    in_freeze_intervals: list[float] = field(default_factory=list)


def _log_cadence_pin_at_onset(channel: str, state: ChannelFreezeState) -> None:
    """Record a branch-1 pin, at DEBUG, when the freeze opens.

    DEBUG and not INFO because a freeze that opens is not yet a freeze
    that matters: most never cross the gate, so most of these records have
    no consumer.  Measured on ``tests/fixtures/native_dec_stuck_fault.csv``
    at INFO: 2011 records for 3387 rows, because temperature changes on
    nearly every row and each change opens a new freeze — while only 5 of
    those freezes ever fired.  The diagnostic record for the ones that
    matter is ``_log_cadence_at_gate``.

    Uses this module's ``logging.getLogger(__name__)`` logger, so the
    records flow through whatever ``src.logging.config.setup_logging``
    installed on the root logger.  Takes the state rather than loose
    values so it cannot report a cadence or branch that was not stored.
    """
    logger.debug(
        "Cadence pinned at onset for %s freeze at %s: branch=%s cadence=%.1fs",
        channel,
        state.onset_ts,
        state.cadence_branch,
        state.cadence_seconds,
    )


def _log_cadence_at_gate(channel: str, state: ChannelFreezeState) -> None:
    """Record the cadence of a freeze that just crossed the gate, at INFO.

    One record per FIRED freeze, which is exactly the set of freezes with
    a diagnostic consumer: only a fired freeze is persisted, so only a
    fired freeze ever presents a ``raw_slot_count`` that someone has to
    explain.  This line is that explanation — the branch and the cadence
    that scaled it — and it carries both even when the pin itself happened
    at onset, hours of data earlier, because a reader holding a surprising
    count should not have to find a second record to interpret it.

    Emitted from ``StuckCarry.mark_onset_fired``, after any branch-2 pin,
    so the cadence reported is the final pinned one and never a
    placeholder.
    """
    logger.info(
        "Stuck gate fired for %s freeze at onset %s: branch=%s cadence=%.1fs "
        "(the cadence that scales its raw_slot_count)",
        channel,
        state.onset_ts,
        state.cadence_branch,
        state.cadence_seconds,
    )


def _log_cadence_unpinned_at_gate(channel: str, state: ChannelFreezeState) -> None:
    """Record a gate fire that found no in-freeze intervals to pin from.

    WARNING, not INFO: unlike a pin, this leaves ``cadence_seconds`` at
    whatever placeholder the state arrived with, so the freeze's
    ``raw_slot_count`` is scaled by a guess.  Unreachable for a freeze
    the detector itself opened — the gate needs
    ``STUCK_WALLCLOCK_MIN_SAMPLES`` frozen samples and every one after
    the first contributes an interval — so this records a hand-built or
    legacy-carried state, which is exactly the case worth a warning.
    """
    logger.warning(
        "Gate fired for %s freeze at onset %s with no in-freeze intervals: "
        "cadence stays at %.1fs, which no evidence from this freeze "
        "supports; raw_slot_count for it is a guess, not a measurement",
        channel,
        state.onset_ts,
        state.cadence_seconds,
    )


def _log_cadence_absent_from_carry(channel: str, onset_ts: str) -> None:
    """Record a carried freeze state that shipped no pinned cadence at all.

    Deploy skew: a portion message produced before ``cadence_seconds``
    existed, consumed by a worker running after it.  The absence is
    tolerated rather than rejected — an in-flight message must stay
    consumable — but never silent.  WARNING because the freeze falls back
    to ``CADENCE_EXPECTED_SECONDS``, and if it has already fired, that
    constant is what scaled its ``raw_slot_count``: a silently wrong
    count is the failure class the two pinning branches exist to remove.
    """
    logger.warning(
        "Carried state for %s freeze at onset %s has no pinned cadence "
        "(producer predates the field): falling back to "
        "CADENCE_EXPECTED_SECONDS=%ds unless the gate pins a measured one",
        channel,
        onset_ts,
        CADENCE_EXPECTED_SECONDS,
    )


def _open_freeze(
    channel: str,
    value: float,
    onset_dt: datetime,
    cadence_seconds: float | None,
) -> ChannelFreezeState:
    """Start a freeze at *onset_dt*, pinning branch 1's cadence if it has one.

    *cadence_seconds* of ``None`` is ``_pre_onset_cadence_seconds``
    reporting no pre-onset evidence: the freeze opens UNPINNED and accrues
    its own intervals until the gate fires and branch 2 pins their median.
    Takes *channel* only to name it in the pin log — the state itself is
    identified by its key in ``StuckCarry.channels``.
    """
    onset_ts = onset_dt.isoformat()
    state = ChannelFreezeState(
        frozen_value=value,
        onset_ts=onset_ts,
        onset_dt=onset_dt,
        last_ts=onset_ts,
        last_dt=onset_dt,
        sample_count=1,
    )
    if cadence_seconds is not None:
        state.cadence_seconds = cadence_seconds
        state.cadence_branch = CADENCE_BRANCH_PRE_ONSET
        _log_cadence_pin_at_onset(channel, state)
    return state


def _pin_in_freeze_cadence(channel: str, state: ChannelFreezeState) -> None:
    """Pin branch 2's cadence: the median of the freeze's own intervals.

    Called from ``StuckCarry.mark_onset_fired`` and nowhere else — the
    instant the dual gate first fires, which is reached from BOTH
    ``detect_chunk`` (mid-freeze) and ``flush`` (freeze still open at end
    of input).  ``_check_gate`` returns early for a freeze already in
    ``emitted_onsets``, so ``mark_onset_fired`` runs exactly once per
    freeze per file, and the ``cadence_branch`` guard there keeps a
    branch-1 freeze from being re-pinned at all.

    Clears ``in_freeze_intervals`` unconditionally: pinned or not, the
    list has had its single read, and it rides in every portion message
    until it is empty.

    Logs nothing on success: this pin happens AT the gate fire, so
    ``_log_cadence_at_gate`` reports it from the caller a line later, and
    a second record for the same instant would only duplicate it.
    """
    cadence_seconds = _in_freeze_cadence_seconds(state.in_freeze_intervals)
    state.in_freeze_intervals.clear()
    if cadence_seconds is None:
        _log_cadence_unpinned_at_gate(channel, state)
        return
    state.cadence_seconds = cadence_seconds
    state.cadence_branch = CADENCE_BRANCH_IN_FREEZE


def _carried_cadence_branch(channel: str, state_dict: dict[str, Any]) -> str | None:
    """Which branch pinned a DESERIALISED freeze's cadence, if any.

    Three cases, in order:

    * ``cadence_branch`` present — the producer ran this code and its
      answer stands, ``None`` included: a freeze that has not pinned yet
      must stay free to pin later.
    * ``cadence_branch`` absent, ``cadence_seconds`` present — a producer
      that pinned before the label existed.  Marked
      ``CADENCE_BRANCH_CARRIED`` so its value is never recomputed: split
      and single-pass runs must agree on ``raw_slot_count``, and
      re-pinning mid-freeze is precisely what breaks that.
    * neither present — deploy skew; see
      ``_log_cadence_absent_from_carry``.  Left UNPINNED rather than
      marked, so a freeze that has not yet fired can still pin a measured
      cadence from its own intervals instead of keeping the constant.
    """
    if "cadence_branch" in state_dict:
        branch = state_dict["cadence_branch"]
        return None if branch is None else str(branch)
    if "cadence_seconds" in state_dict:
        return CADENCE_BRANCH_CARRIED
    _log_cadence_absent_from_carry(channel, str(state_dict.get("onset_ts", "unknown")))
    return None


@dataclass
class FaultWindow:
    """Recorded fault period for post-fault baseline cleanup.

    Three temporal markers per fault:

    ``onset_ts`` / ``onset_dt``
        First frozen sample — the beginning of the fault.

    ``last_frozen_ts`` / ``last_frozen_dt``
        Last sample that was still at the frozen value.  Required
        (always known by the detector at release time).  Baseline
        exclusion uses ``[onset_ts, last_frozen_ts]``.

    ``release_ts`` / ``release_dt``
        First **non-frozen** sample after the freeze ended — the
        release point.  Alert suppression uses
        ``[onset_ts, release_ts + N]``.

    ``end_ts`` is deliberately **not** a FaultWindow field.
    ``anomaly_episodes.end_ts`` (Task 3) equals ``last_frozen_ts``
    — the last sample inside the fault, not the release.

    Two counts over the frozen span ``[onset_ts, last_frozen_ts]`` — one
    observed, one expected:

    ``frozen_sample_count``
        Finite samples the detector counted at the frozen value, tallied
        inside ``WallclockStuckDetector`` as rows are consumed (see
        ``ChannelFreezeState``), never re-derived from timestamps.

    ``raw_slot_count``
        Slots the channel *should* have produced over that span at its
        nominal cadence — ``expected_slot_count(onset_dt,
        last_frozen_dt, cadence)``.  Derived from the two endpoints, not
        a tally of rows.

    ``raw_slot_count - frozen_sample_count > 0`` therefore means readings
    are MISSING inside the freeze: the sensor went offline, the row was
    never logged, or an upstream stage dropped it.  The gap was formerly
    a row tally and was structurally always 0 on the upload path, where
    ``CSVAdapter`` deletes null-``humidity`` rows before the detector sees
    them; expected slots are reachable there.
    ``None`` means "not tracked by a detector" (a hand-built window) and
    maps to a NULL column — do not substitute ``0``, which would assert a
    measurement that was never taken.

    Episode-eligibility contract
    ----------------------------
    ``has_fired`` is ``True`` only when this freeze actually crossed the
    detector's **dual** gate — ``STUCK_WALLCLOCK_MIN_HOURS`` *and*
    ``STUCK_WALLCLOCK_MIN_SAMPLES`` — and emitted an alert.  A window with
    ``has_fired=False`` MUST NOT become an ``anomaly_episodes`` row.
    Consumers filter on this flag and must **not** re-evaluate hours and
    samples themselves: a second copy of the gate is a divergence bug
    waiting to happen.  It defaults to ``False`` so that every future
    construction site has to state explicitly that its freeze fired.

    The equivalent question for a freeze that is still open — and so has
    no ``FaultWindow`` yet — is ``StuckCarry.has_onset_fired``.

    The serialised carry format (``to_dict`` / ``from_dict``) uses
    ``release_ts`` / ``release_dt`` as keys.  No legacy-key alias is
    maintained: the serialised carry exists only on this unmerged
    branch and was never deployed.
    """

    channel: str
    onset_ts: str
    onset_dt: datetime
    release_ts: str
    release_dt: datetime
    last_frozen_ts: str
    last_frozen_dt: datetime
    frozen_sample_count: int | None = None
    raw_slot_count: int | None = None
    has_fired: bool = False


@dataclass
class StuckCarry:
    """Rolling state for wall-clock stuck-fault detection across portions.

    Bounded memory: stores one ``ChannelFreezeState`` per monitored channel
    (constant-size) plus a short list of per-chunk cadence medians.

    ``skip_channels`` excludes imputed channels (constant fill values that
    would otherwise trigger a false stuck alert for the entire file).

    ``recovery_channels`` maps channel → FaultWindow for channels that just
    exited a detected fault.  Used by the statistical detector to purge
    contaminated baseline values and by episode grouping to tag recovery
    alerts.

    Serialisation
    -------------
    ``to_dict`` / ``from_dict`` produce a JSON-safe round-trip.  The async
    upload path (RabbitMQ workers) embeds the carry state in each portion
    message so workers can re-run detection independently — portions are
    processed concurrently and may arrive out of order, so the carry must be
    pre-computed by the sequential producer and shipped with each message
    rather than threaded through the result store.
    """

    channels: dict[str, ChannelFreezeState] = field(default_factory=dict)
    chunk_medians: list[float] = field(default_factory=list)
    last_timestamp_dt: datetime | None = None
    is_cadence_flagged: bool = False
    cadence_median_seconds: float | None = None
    emitted_onsets: set[tuple[str, str]] = field(default_factory=set)
    skip_channels: frozenset[str] = field(default_factory=frozenset)
    recovery_channels: dict[str, FaultWindow] = field(default_factory=dict)
    completed_faults: list[FaultWindow] = field(default_factory=list)

    def has_onset_fired(self, channel: str, onset_ts: str) -> bool:
        """Whether the freeze that began at *onset_ts* crossed the gate.

        ``emitted_onsets`` is the detector's authoritative record of which
        freezes satisfied the dual gate, so this is the only correct answer
        to "may this freeze become an episode?" for a freeze that is still
        open and therefore has no ``FaultWindow`` yet.  For a released
        freeze the same fact travels on the window as
        ``FaultWindow.has_fired``.  Callers must use one of the two rather
        than re-evaluating hours and samples.

        *onset_ts* is matched by INSTANT, not by spelling: it is
        canonicalised to UTC before the lookup (see
        ``_canonical_onset_key``), so a ``Z`` suffix, a non-UTC offset or
        a naive timestamp all find the key the detector stored.  Plain
        string equality returned ``False`` for every one of those, which
        reads identically to "did not fire" — the worst available failure
        mode for a caller deciding whether to persist an episode.
        """
        return _canonical_onset_key(channel, onset_ts) in self.emitted_onsets

    def mark_onset_fired(self, channel: str, state: ChannelFreezeState) -> None:
        """Record that the freeze in *state* crossed the gate, and pin branch 2.

        The write counterpart of ``has_onset_fired``; both canonicalise
        through ``_canonical_onset_key``, which is what keeps the set's
        keys in one spelling.

        Takes the state, not a bare ``onset_ts``, because this is also
        where a freeze that opened with no pre-onset cadence evidence pins
        one: the median of its OWN intervals, taken here because this is
        the single instant the gate first fires, and it is reached from
        both ``detect_chunk`` and ``flush``.  Pinning in either caller
        instead would pin twice or miss the end-of-input path.  A freeze
        that already pinned (branch 1, or a carried value) is left
        untouched — once pinned, never recomputed.

        The INFO record for the freeze is emitted here, last, for the same
        reason the pin lives here: this runs exactly once per freeze per
        file, from both the mid-chunk and the end-of-input path, and
        running after the pin is what lets it report the final cadence
        rather than a placeholder.
        """
        self.emitted_onsets.add(_canonical_onset_key(channel, state.onset_ts))
        if state.cadence_branch is None:
            _pin_in_freeze_cadence(channel, state)
        _log_cadence_at_gate(channel, state)

    # ------------------------------------------------------------------
    # Serialisation helpers (async upload path: carry travels in messages)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict.

        Round-trip contract:
        - All ``datetime`` fields are stored as ISO-8601 strings with UTC
          offset (``+00:00``), so ``fromisoformat`` reconstructs timezone-
          aware objects unambiguously.
        - ``emitted_onsets`` (``set[tuple[str, str]]``) is stored as a list
          of 2-element lists; ``from_dict`` restores the set of tuples,
          re-canonicalising each key so a carry hand-built with ``Z``
          suffixes is matchable after the round trip.
        - ``skip_channels`` (``frozenset``) is stored as a list of strings.
        """

        def _dt_str(dt: datetime | None) -> str | None:
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()

        def _freeze_state(s: ChannelFreezeState) -> dict[str, Any]:
            return {
                "frozen_value": s.frozen_value,
                "onset_ts": s.onset_ts,
                "onset_dt": _dt_str(s.onset_dt),
                "last_ts": s.last_ts,
                "last_dt": _dt_str(s.last_dt),
                "sample_count": s.sample_count,
                "cadence_seconds": s.cadence_seconds,
                "cadence_branch": s.cadence_branch,
                "in_freeze_intervals": list(s.in_freeze_intervals),
            }

        def _fault_window(fw: FaultWindow) -> dict[str, Any]:
            return {
                "channel": fw.channel,
                "onset_ts": fw.onset_ts,
                "onset_dt": _dt_str(fw.onset_dt),
                "release_ts": fw.release_ts,
                "release_dt": _dt_str(fw.release_dt),
                "last_frozen_ts": fw.last_frozen_ts,
                "last_frozen_dt": _dt_str(fw.last_frozen_dt),
                "frozen_sample_count": fw.frozen_sample_count,
                "raw_slot_count": fw.raw_slot_count,
                "has_fired": fw.has_fired,
            }

        return {
            "channels": {ch: _freeze_state(s) for ch, s in self.channels.items()},
            "chunk_medians": list(self.chunk_medians),
            "last_timestamp_dt": _dt_str(self.last_timestamp_dt),
            "is_cadence_flagged": self.is_cadence_flagged,
            "cadence_median_seconds": self.cadence_median_seconds,
            "emitted_onsets": [list(pair) for pair in self.emitted_onsets],
            "skip_channels": list(self.skip_channels),
            "recovery_channels": {
                ch: _fault_window(fw) for ch, fw in self.recovery_channels.items()
            },
            "completed_faults": [_fault_window(fw) for fw in self.completed_faults],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StuckCarry":
        """Deserialise from the dict produced by ``to_dict``.

        Restores timezone-aware UTC datetimes, set-of-tuples for
        ``emitted_onsets``, and ``frozenset`` for ``skip_channels``.
        """

        def _parse_dt(s: str | None) -> datetime | None:
            if s is None:
                return None
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        def _opt_int(v: object) -> int | None:
            """Preserve ``None`` ("never counted") distinctly from ``0``."""
            return None if v is None else int(v)  # type: ignore[arg-type]

        def _freeze_state(channel: str, sd: dict[str, Any]) -> ChannelFreezeState:
            return ChannelFreezeState(
                frozen_value=float(sd["frozen_value"]),
                onset_ts=sd["onset_ts"],
                onset_dt=_parse_dt(sd["onset_dt"]),  # type: ignore[arg-type]
                last_ts=sd["last_ts"],
                last_dt=_parse_dt(sd["last_dt"]),  # type: ignore[arg-type]
                sample_count=int(sd["sample_count"]),
                # The three cadence fields are tolerated as absent, unlike
                # the fields above: a portion message produced before they
                # existed must still be consumable by a worker running after
                # them.  Absence is not silent, though —
                # ``_carried_cadence_branch`` warns when a state carries no
                # pinned cadence at all, because the constant substituted
                # here is a guess at a number read as a measurement.
                cadence_seconds=float(
                    sd.get("cadence_seconds", CADENCE_EXPECTED_SECONDS)
                ),
                cadence_branch=_carried_cadence_branch(channel, sd),
                in_freeze_intervals=[
                    float(v) for v in sd.get("in_freeze_intervals", [])
                ],
            )

        def _fault_window(fd: dict[str, Any]) -> FaultWindow:
            return FaultWindow(
                channel=fd["channel"],
                onset_ts=fd["onset_ts"],
                onset_dt=_parse_dt(fd["onset_dt"]),  # type: ignore[arg-type]
                release_ts=fd["release_ts"],
                release_dt=_parse_dt(fd["release_dt"]),  # type: ignore[arg-type]
                last_frozen_ts=fd["last_frozen_ts"],
                last_frozen_dt=_parse_dt(fd["last_frozen_dt"]),  # type: ignore[arg-type]
                frozen_sample_count=_opt_int(fd["frozen_sample_count"]),
                raw_slot_count=_opt_int(fd["raw_slot_count"]),
                has_fired=bool(fd["has_fired"]),
            )

        return cls(
            channels={
                ch: _freeze_state(ch, sd)
                for ch, sd in d.get("channels", {}).items()
            },
            chunk_medians=list(d.get("chunk_medians", [])),
            last_timestamp_dt=_parse_dt(d.get("last_timestamp_dt")),
            is_cadence_flagged=bool(d.get("is_cadence_flagged", False)),
            cadence_median_seconds=d.get("cadence_median_seconds"),
            emitted_onsets={
                _canonical_onset_key(str(pair[0]), str(pair[1]))
                for pair in d.get("emitted_onsets", [])
            },
            skip_channels=frozenset(d.get("skip_channels", [])),
            recovery_channels={
                ch: _fault_window(fd)
                for ch, fd in d.get("recovery_channels", {}).items()
            },
            completed_faults=[
                _fault_window(fd) for fd in d.get("completed_faults", [])
            ],
        )


def _accrue_in_freeze_interval(carry: StuckCarry, delta_seconds: float) -> None:
    """Give one row-to-row interval to every open freeze still unpinned.

    Called once per row, BEFORE the per-channel loop that may open new
    freezes on that row: a freeze opening on the row that closed this
    interval must not count it, because for that freeze the interval is
    PRE-onset evidence and belongs to branch 1 instead.  A freeze that has
    already pinned is skipped — once pinned, never recomputed — which is
    also what stops the list growing after it has been cleared.

    The row that ENDS a freeze does contribute its interval, because
    accrual runs before the per-channel loop that notices the value
    changed: one interval reaching just past the frozen span, which a
    median absorbs, and the alternative — accruing per channel after the
    comparison — would make the evidence depend on which channel broke
    first.

    The quantity is the FILE's row-to-row delta, the same one branch 1
    takes the median of, not the channel's own frozen-sample delta.  Two
    reasons: the branches must measure the same thing, or a file's
    expected-slot count would depend on which branch happened to pin it;
    and a row logged with a null for THIS channel is still evidence of the
    file's cadence, so counting it keeps that channel's missing readings
    visible as a ``raw_slot_count`` gap instead of absorbing them into its
    cadence.
    """
    for state in carry.channels.values():
        if state.cadence_branch is None:
            state.in_freeze_intervals.append(delta_seconds)


class WallclockStuckDetector:
    """Wall-clock-gated stuck-fault detection for the CSV upload path.

    Unlike ``StuckFaultDetector`` (sample-indexed), this gates on elapsed
    time so it works correctly regardless of the input's sampling cadence.
    A freeze must persist for at least ``min_hours`` AND have at least
    ``min_samples`` identical readings.

    Usage (streaming, chunk-by-chunk)::

        carry = StuckCarry()
        det = WallclockStuckDetector()
        for chunk_df in chunks:
            alerts = det.detect_chunk(chunk_df, carry)
        final_alerts = det.flush(carry)
    """

    def __init__(
        self,
        min_hours: float = STUCK_WALLCLOCK_MIN_HOURS,
        min_samples: int = STUCK_WALLCLOCK_MIN_SAMPLES,
        channel_epsilon: dict[str, float] | None = None,
        channels: tuple[str, ...] = STUCK_WALLCLOCK_CHANNELS,
    ) -> None:
        self._min_hours = min_hours
        self._min_samples = min_samples
        self._epsilon = channel_epsilon or dict(STUCK_WALLCLOCK_EPSILON)
        self._channels = channels

    # -- public interface --------------------------------------------------

    def detect_chunk(
        self,
        normalized_df: pd.DataFrame,
        carry: StuckCarry,
    ) -> list[dict[str, Any]]:
        """Process one chunk, returning alerts for freezes that ended.

        Each alert is a dict with keys: ``timestamp``, ``channel``,
        ``severity``, ``detector``, ``value``, ``duration_hours``,
        ``method``.
        """
        alerts: list[dict[str, Any]] = []

        if normalized_df.empty or "timestamp" not in normalized_df.columns:
            return alerts

        timestamps = normalized_df["timestamp"]
        intervals: list[float] = []

        for row_idx in range(len(normalized_df)):
            ts_raw = timestamps.iloc[row_idx]
            if pd.isnull(ts_raw):
                continue

            row_dt = _parse_timestamp(ts_raw)
            if row_dt is None:
                continue

            # --- cadence tracking ---
            # Snapshot the interval count BEFORE this row contributes its
            # own: a freeze opening on this row must pin its cadence from
            # intervals observed strictly before its onset.
            pre_onset_interval_count = len(intervals)
            if carry.last_timestamp_dt is not None:
                delta = (row_dt - carry.last_timestamp_dt).total_seconds()
                if delta > 0:
                    intervals.append(delta)
                    _accrue_in_freeze_interval(carry, delta)
            carry.last_timestamp_dt = row_dt

            ts_iso = row_dt.isoformat()

            # Resolved at most once per row: every channel opening a
            # freeze here sees the same pre-onset evidence.  ``None`` is a
            # real answer (no evidence — branch 2 pins instead), so it
            # cannot double as "not resolved yet"; hence the flag.
            is_cadence_pin_resolved = False
            cadence_pin: float | None = None

            # --- per-channel freeze tracking ---
            for channel in self._channels:
                if channel not in normalized_df.columns:
                    continue
                if channel in carry.skip_channels:
                    continue

                value = normalized_df[channel].iloc[row_idx]
                if pd.isnull(value) or not np.isfinite(float(value)):
                    continue  # NaN → skip, don't break or extend

                value_f = float(value)
                epsilon = self._epsilon.get(channel, 0.01)
                state = carry.channels.get(channel)

                if state is None:
                    if not is_cadence_pin_resolved:
                        cadence_pin = _pre_onset_cadence_seconds(
                            carry.chunk_medians,
                            intervals[:pre_onset_interval_count],
                        )
                        is_cadence_pin_resolved = True
                    carry.channels[channel] = _open_freeze(
                        channel, value_f, row_dt, cadence_pin,
                    )
                elif abs(value_f - state.frozen_value) <= epsilon:
                    state.last_ts = ts_iso
                    state.last_dt = row_dt
                    state.sample_count += 1
                    # Task 2b: evaluate gate DURING the freeze so
                    # the alert fires as soon as the gate is met,
                    # not only when the freeze ends.  emitted_onsets
                    # prevents duplicate emission on later rows.
                    alert = self._check_gate(
                        channel, state, carry, detected_ts=ts_iso,
                    )
                    if alert is not None:
                        alerts.append(alert)
                else:
                    # Value changed → freeze ended.  Try to emit if
                    # not already emitted mid-freeze.  This gate check
                    # must stay AHEAD of the FaultWindow below: for a
                    # freeze that fires here, the cadence the window
                    # scales by is pinned inside it (branch 2, via
                    # mark_onset_fired), so reordering the two would
                    # hand the window an unpinned placeholder.
                    alert = self._check_gate(
                        channel, state, carry, detected_ts=ts_iso,
                    )
                    if alert is not None:
                        alerts.append(alert)
                    # Record recovery for baseline cleanup (Task 3).
                    # Only if a fault was actually detected for this
                    # freeze (key is in emitted_onsets).
                    if carry.has_onset_fired(channel, state.onset_ts):
                        # raw_slot_count is EXPECTED slots over
                        # [onset_ts, last_ts] — the span ending at the
                        # final FROZEN sample, so neither this changed row
                        # nor any null trailing it is inside it.  The
                        # cadence comes from the freeze's own pinned value,
                        # not from anything measured since: that is what
                        # makes the count independent of where the chunk
                        # boundaries fell.
                        fw = FaultWindow(
                            channel=channel,
                            onset_ts=state.onset_ts,
                            onset_dt=state.onset_dt,
                            release_ts=ts_iso,
                            release_dt=row_dt,
                            last_frozen_ts=state.last_ts,
                            last_frozen_dt=state.last_dt,
                            frozen_sample_count=state.sample_count,
                            raw_slot_count=expected_slot_count(
                                state.onset_dt,
                                state.last_dt,
                                state.cadence_seconds,
                            ),
                            has_fired=True,
                        )
                        carry.recovery_channels[channel] = fw
                        carry.completed_faults.append(fw)
                    if not is_cadence_pin_resolved:
                        cadence_pin = _pre_onset_cadence_seconds(
                            carry.chunk_medians,
                            intervals[:pre_onset_interval_count],
                        )
                        is_cadence_pin_resolved = True
                    carry.channels[channel] = _open_freeze(
                        channel, value_f, row_dt, cadence_pin,
                    )

        if intervals:
            carry.chunk_medians.append(float(np.median(intervals)))

        return alerts

    def flush(self, carry: StuckCarry) -> list[dict[str, Any]]:
        """Flush still-open freezes and finalise cadence check."""
        alerts: list[dict[str, Any]] = []
        for channel, state in carry.channels.items():
            alert = self._check_gate(
                channel, state, carry, detected_ts=state.last_ts,
            )
            if alert is not None:
                alerts.append(alert)

        # --- cadence check ---
        if carry.chunk_medians:
            overall_median = float(np.median(carry.chunk_medians))
            carry.cadence_median_seconds = overall_median
            ratio = overall_median / CADENCE_EXPECTED_SECONDS
            if (
                ratio > CADENCE_TOLERANCE_FACTOR
                or ratio < (1.0 / CADENCE_TOLERANCE_FACTOR)
            ):
                carry.is_cadence_flagged = True
                logger.warning(
                    "Cadence deviation: median interval %.1fs vs expected %ds "
                    "(ratio %.2f, tolerance %.1f)",
                    overall_median,
                    CADENCE_EXPECTED_SECONDS,
                    ratio,
                    CADENCE_TOLERANCE_FACTOR,
                )

        return alerts

    # -- internal ----------------------------------------------------------

    def _check_gate(
        self,
        channel: str,
        state: ChannelFreezeState,
        carry: StuckCarry,
        *,
        detected_ts: str | None = None,
    ) -> dict[str, Any] | None:
        """Emit an alert dict if the freeze meets the gate.

        ``detected_ts`` is the row timestamp at which the gate was first
        satisfied — distinct from ``state.onset_ts`` (when the freeze
        began).  The alert carries both so downstream code can compute
        true detection latency as ``detected_ts - onset_ts``.

        ``duration_hours`` is elapsed-so-far at detection time (the freeze
        may still be open).  Episode grouping updates the final duration
        when the freeze ends.

        This is the only caller of ``StuckCarry.mark_onset_fired``, and it
        reaches it only for a freeze not already in ``emitted_onsets`` —
        which is what makes the branch-2 cadence pin inside that method
        happen exactly once per freeze, whether the gate is first satisfied
        mid-chunk (``detect_chunk``) or at end of input (``flush``).
        """
        duration_hours = (
            (state.last_dt - state.onset_dt).total_seconds() / 3600.0
        )
        if duration_hours < self._min_hours:
            return None
        if state.sample_count < self._min_samples:
            return None

        if carry.has_onset_fired(channel, state.onset_ts):
            return None
        carry.mark_onset_fired(channel, state)

        return {
            "timestamp": state.onset_ts,
            "detected_ts": detected_ts or state.last_ts,
            "channel": channel,
            "severity": "urgent",
            "detector": "stuck_fault",
            "value": state.frozen_value,
            "duration_hours": round(duration_hours, 2),
            "method": "wallclock_stuck",
        }
