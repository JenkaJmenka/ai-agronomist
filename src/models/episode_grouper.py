"""Episode grouping for anomaly alerts (Tasks 3 + 5 + 5b).

Groups contiguous per-channel alerts into episodes so one continuous
event is one alert with a duration instead of N rows.

**Design decisions (docs/architecture/episode_grouping.md):**

1. Grouping lives at the detection layer (``finalize_scores``).
2. Episodes are keyed by **(channel, detector)** — a ``stuck_fault`` alert
   is never absorbed into a ``statistical`` episode on the same channel.
3. Stuck-fault episodes derive BOTH their ``end_ts`` and their
   ``duration_seconds`` from the actual frozen span (via
   ``completed_faults`` / ``frozen_hours_at_detection``), not from the span
   of alert rows — which would be 0 for a single-row emission, since a
   freeze emits exactly one alert, stamped at its onset.
4. An ``is_ongoing`` flag marks episodes whose freeze had not yet ended
   at end of input.  Both their ``end_ts`` and their ``duration_seconds``
   describe the span observed SO FAR, which is a lower bound on the eventual
   fault — the freeze continues past the data.
5. ``end_ts - start_ts == duration_seconds`` holds for every episode, every
   detector, every stuck-fault strategy below.  One resolution produces the
   pair (``_stuck_fault_span``), so the two can never contradict each
   other — the bug this invariant replaces reported
   ``duration_seconds=589800`` alongside a zero-length timestamp span.
   ``frozen_sample_count`` and ``raw_slot_count`` come out of that same
   resolution, so all four describe one span.
6. A stuck fault's frozen span is resolved in this order, first hit wins:

   a. the matching **completed** ``FaultWindow`` (``completed_faults``) —
      an exact closed span ending at ``last_frozen_ts``;
   b. the matching **open** ``ChannelFreezeState`` (``open_freezes``, i.e.
      ``StuckCarry.channels``) — the span observed so far, ending at the
      real last frozen sample ``last_ts``;
   c. the alert's own ``frozen_hours_at_detection`` laid off from the onset;
   d. zero.

   (c) is a LAST RESORT reached only when the caller supplied no open
   states, and it is short by however long the freeze ran past the gate
   crossing — see ``_stuck_fault_span`` for why.  Callers holding a
   ``StuckCarry`` should pass ``open_freezes`` and get (b).

7. Three counts ride on a stuck-fault episode and they are NOT
   interchangeable.  ``sample_count`` counts ALERT ROWS, so it is always 1
   for a freeze (decision 3).  ``frozen_sample_count`` counts samples
   observed at the frozen value, and ``raw_slot_count`` counts the samples
   the channel owed over the same span at its pinned cadence, so their
   difference is readings missing inside the freeze.  Only arms (a) and (b)
   can supply the latter two; (c) and (d) leave them ``None``, meaning NOT
   MEASURED.  Each field says this again at its own definition, because
   reading ``sample_count`` as a reading count is the specific mistake
   these fields exist to make impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import EPISODE_GAP_SECONDS
from src.models.stuck_fault_detector import expected_slot_count

# Seconds per hour, used by stuck_fault duration conversion.
_SECONDS_PER_HOUR = 3600.0


def _parse_iso(ts: str) -> datetime:
    """Parse ISO timestamp to UTC-aware datetime."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class AnomalyEpisode:
    """One continuous anomaly event on a single channel.

    A stuck-fault episode carries THREE counts, and they mean three
    different things: ``sample_count`` counts alert rows, which for a
    freeze is always 1; ``frozen_sample_count`` counts samples observed at
    the frozen value; ``raw_slot_count`` counts the samples the channel
    should have produced.  Each is documented at its own definition below.
    """

    channel: str
    detector: str
    start_ts: str
    end_ts: str
    duration_seconds: float
    peak_score: float

    # The number of ALERT ROWS this episode was built from.  It is NOT a
    # count of readings, samples, or frozen samples.
    #
    # For a ``stuck_fault`` episode it is ALWAYS 1, because a freeze emits
    # exactly one alert and that alert is stamped at the freeze's onset
    # (module docstring, design decision 3).  A freeze that ran for seven
    # days across two thousand frozen samples still produces exactly one
    # alert row, so this field still reads 1.  Do not read it as "how many
    # samples were frozen" and do not change it to that: the frozen-sample
    # tally is ``frozen_sample_count`` below, and the slots the channel
    # owed over the same span are ``raw_slot_count``.
    #
    # For every other detector (``lstm``, ``statistical``) each flagged row
    # emits its own alert, so the alert count and the flagged-row count are
    # the same number and this field is both.
    sample_count: int

    severity: str
    # True when this episode is on a channel other than a stuck-fault
    # channel and starts no earlier than EPISODE_GAP_SECONDS before, and
    # within RECOVERY_KNOCKON_WINDOW_HOURS after, that fault's release.
    # Informational only; does not suppress or rescore.
    #
    # ALWAYS False on this (detection-time) path: the tagging needs a
    # released fault's release_ts, which may belong to an earlier upload
    # that this in-memory grouping pass never sees (stuck_carry is
    # per-upload). The real computation reads the persisted
    # anomaly_episodes table instead and lives entirely at query time
    # (src/api/services/anomaly_episode_service.py). Kept here, always
    # False, only so the upload response's AnomalyEpisodeSchema keeps a
    # stable shape.
    is_recovery: bool = False
    is_ongoing: bool = False
    onset_ts: str | None = None
    detected_ts: str | None = None
    frozen_value: float | None = None

    # Finite samples the detector observed at the frozen value over this
    # episode's span ``[start_ts, end_ts]``, tallied as rows were consumed
    # and never re-derived from timestamps.  ``stuck_fault`` only.
    #
    # ``None`` means NOT MEASURED — either the detector is not
    # ``stuck_fault`` at all, or the resolution arm that produced this
    # episode's span had no count to give (see ``_stuck_fault_span``).
    # Never substitute 0: that would assert that a freeze froze zero
    # samples, which no real freeze does.
    frozen_sample_count: int | None = None

    # Slots the channel SHOULD have produced over the same span at its
    # pinned cadence (``expected_slot_count``), not a tally of rows.
    # ``raw_slot_count - frozen_sample_count > 0`` therefore means readings
    # are MISSING inside the freeze.  ``stuck_fault`` only, ``None`` under
    # the same conditions and with the same "not measured" meaning as
    # ``frozen_sample_count``.
    raw_slot_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON response."""
        return {
            "channel": self.channel,
            "detector": self.detector,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_seconds": self.duration_seconds,
            "peak_score": self.peak_score,
            "sample_count": self.sample_count,
            "severity": self.severity,
            "is_recovery": self.is_recovery,
            "is_ongoing": self.is_ongoing,
            "onset_ts": self.onset_ts,
            "detected_ts": self.detected_ts,
            "frozen_value": self.frozen_value,
            "frozen_sample_count": self.frozen_sample_count,
            "raw_slot_count": self.raw_slot_count,
        }


def _severity_rank(sev: str | None) -> int:
    """Numeric rank for severity comparison; no detector persists "normal"."""
    ranks = {"unclassified": 0, "warning": 1, "critical": 2, "urgent": 3}
    return ranks.get(sev or "unclassified", 0)


def group_alerts_into_episodes(
    high_anomalies: list[Any],
    *,
    completed_faults: list[Any] | None = None,
    open_freezes: dict[str, Any] | None = None,
    gap_seconds: int = EPISODE_GAP_SECONDS,
) -> list[AnomalyEpisode]:
    """Group high_anomalies into contiguous per-(channel, detector) episodes.

    Two alerts belong to the same episode when they share a channel AND
    a detector and their timestamps are within ``gap_seconds``.  Alerts
    from different detectors are always separate episodes — a
    ``stuck_fault`` alert is never absorbed into a ``statistical``
    episode.

    Parameters
    ----------
    high_anomalies:
        ``UploadHighAnomalyDetail`` list from ``finalize_scores``.
    completed_faults:
        ``stuck_carry.completed_faults`` — all completed FaultWindows,
        used to compute final stuck_fault episode durations.
    open_freezes:
        ``stuck_carry.channels`` — maps channel → ChannelFreezeState for
        freezes still open at end of input.  Optional: omitting it does not
        change any other episode, it only degrades a still-open stuck fault
        to the ``frozen_hours_at_detection`` lower bound (module docstring
        rule 6c instead of 6b).
    gap_seconds:
        Max gap before splitting into a new episode.
    """
    completed_faults = completed_faults or []

    # Index completed faults by (channel, onset_ts) for O(1) lookup.  Open
    # states are already keyed by channel, so they need no index.
    sources = _StuckFaultSources(
        fault_index=_build_fault_index(completed_faults),
        open_freezes=open_freezes or {},
    )

    # Build sortable records: (group_key, dt, alert)
    records: list[tuple[str, datetime, Any]] = []
    for alert in high_anomalies:
        key = _channel_key(alert)
        try:
            dt = _parse_iso(alert.timestamp)
        except (ValueError, AttributeError):
            continue
        records.append((key, dt, alert))

    records.sort(key=lambda r: (r[0], r[1]))

    episodes: list[AnomalyEpisode] = []

    current_key: str | None = None
    current_group: list[tuple[datetime, Any]] = []

    for key, dt, alert in records:
        if key != current_key:
            _flush_group(current_group, episodes, sources)
            current_key = key
            current_group = [(dt, alert)]
        else:
            last_dt = current_group[-1][0]
            gap = (dt - last_dt).total_seconds()
            if gap > gap_seconds:
                _flush_group(current_group, episodes, sources)
                current_group = [(dt, alert)]
            else:
                current_group.append((dt, alert))

    _flush_group(current_group, episodes, sources)
    return episodes


def _channel_key(alert: Any) -> str:
    """Grouping key: ``channel:detector``.

    Alerts are only merged within the same (channel, detector) pair.
    """
    col = getattr(alert, "column", None)
    det = getattr(alert, "detector", None) or "lstm"
    if col:
        return f"{col}:{det}"
    return f"__lstm__:{det}"


def _build_fault_index(
    completed_faults: list[Any],
) -> dict[tuple[str, str], Any]:
    """Index completed FaultWindows by (channel, onset_ts)."""
    index: dict[tuple[str, str], Any] = {}
    for fw in completed_faults:
        key = (fw.channel, fw.onset_ts)
        index[key] = fw
    return index


@dataclass(frozen=True)
class _StuckFaultSources:
    """Everything a stuck-fault episode's frozen span can be resolved from.

    Bundled rather than passed as two parameters so that adding a third
    source later does not widen every signature between
    ``group_alerts_into_episodes`` and ``_stuck_fault_span`` again.

    ``fault_index``
        Released freezes, ``(channel, onset_ts) -> FaultWindow``.
    ``open_freezes``
        Freezes still open at end of input, ``channel ->
        ChannelFreezeState``, exactly as held in ``StuckCarry.channels``.
        Empty when the caller did not supply any, which is what makes the
        open-state branch inert by default.

    Both are keyed on the onset, which is also the stuck alert's timestamp.
    """

    fault_index: dict[tuple[str, str], Any]
    open_freezes: dict[str, Any]


@dataclass(frozen=True)
class _StuckFaultSpan:
    """One stuck-fault episode's frozen span: boundary, duration, counts.

    Every field leaves ``_stuck_fault_span`` together, derived from a
    single resolution of the freeze.  That is what makes ``end_dt -
    onset_dt == duration_seconds`` structurally true rather than a
    convention two separate code paths have to remember, and it is equally
    what keeps the two counts describing the SAME span as that pair — a
    count resolved from one arm beside a boundary resolved from another
    would be the same class of contradiction invariant 5 exists to
    prevent.  Never widen this to let a caller supply one field and compute
    another.

    ``frozen_sample_count`` / ``raw_slot_count`` are ``None`` when the arm
    that won carried no count (arms 3 and 4 never carry one).  ``None`` is
    "not measured"; 0 would be a measurement, and a false one.
    """

    end_dt: datetime
    duration_seconds: float
    is_ongoing: bool
    frozen_sample_count: int | None = None
    raw_slot_count: int | None = None


def _open_freeze_end(state: Any, onset_dt: datetime) -> datetime | None:
    """Last frozen sample of *state*, if *state* is the freeze at *onset_dt*.

    Returns ``None`` — meaning "no better information here, fall through" —
    when the channel's open freeze is a DIFFERENT one (a channel can have
    released the freeze this episode describes and opened a new one since),
    when either timestamp is missing or unparseable, or when the span would
    run backwards.  Never raises: a malformed carry must degrade to the
    lower-bound branch, not fail the whole upload response.

    Onsets are compared as instants rather than as strings:
    ``ChannelFreezeState.onset_ts`` and the alert's ``timestamp`` are two
    ISO spellings of the same instant and need not be byte-identical (see
    ``_canonical_onset_key`` in ``stuck_fault_detector.py``).
    """
    try:
        if _parse_iso(state.onset_ts) != onset_dt:
            return None
        end_dt = _parse_iso(state.last_ts)
    except (AttributeError, TypeError, ValueError):
        return None
    if end_dt < onset_dt:
        return None
    return end_dt


def _open_freeze_sample_count(state: Any) -> int | None:
    """Frozen samples tallied on the still-open freeze *state*.

    ``ChannelFreezeState.sample_count`` is the detector's own running tally
    of finite samples seen at the frozen value, maintained as rows are
    consumed, so it is surfaced as-is.  Returns ``None`` for a state that
    carries no integer tally (a hand-built or legacy-deserialised state),
    which reads as "not measured" rather than as zero frozen samples.
    """
    count = getattr(state, "sample_count", None)
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def _open_freeze_slot_count(
    state: Any,
    onset_dt: datetime,
    end_dt: datetime,
) -> int | None:
    """Expected slots of still-open freeze *state* over ``[onset, end]``.

    ``ChannelFreezeState`` deliberately holds no slot counter — its
    docstring records why: the tally it used to keep counted rows that
    survived ``CSVAdapter``, which measured the adapter and not the sensor.
    So the figure for a freeze that never released has exactly one
    canonical source, ``expected_slot_count`` over the span's two endpoints
    at the freeze's pinned cadence, and that function's contract requires
    callers in this position to use it rather than re-derive the count.
    It is also the call ``anomaly_episode_writer`` makes for the very same
    open state, which is what makes this episode and the persisted
    ``anomaly_episodes`` row carry the same number instead of two
    independently derived ones.

    Returns ``None`` when the state carries no usable cadence, so an
    unpinnable freeze reports "not measured" instead of a count scaled by
    a placeholder.  Never raises: ``expected_slot_count`` rejects a
    non-positive cadence and a backwards span, and a malformed carry must
    degrade this one field rather than fail the whole upload response.
    """
    try:
        cadence_seconds = float(state.cadence_seconds)
    except (AttributeError, TypeError, ValueError):
        return None
    if cadence_seconds <= 0:
        return None
    try:
        return expected_slot_count(onset_dt, end_dt, cadence_seconds)
    except ValueError:
        return None


def _stuck_fault_span(
    onset: tuple[datetime, Any],
    actual_channel: str,
    sources: _StuckFaultSources,
) -> _StuckFaultSpan:
    """Resolve span, duration, ongoing flag and counts for a stuck_fault.

    *onset* is the group's first ``(dt, alert)`` entry — the freeze's onset,
    since a freeze emits exactly one alert and it is stamped at ``onset_ts``.
    Taking the pair rather than the two values separately keeps the parsed
    datetime and the alert it came from from drifting apart.

    Resolution order (module docstring rule 6), first hit wins:

    1. A completed ``FaultWindow`` matching (channel, onset_ts): the frozen
       span is known exactly and ends at ``last_frozen_ts``, the last sample
       still at the frozen value.  is_ongoing=False.  BOTH counts available —
       surfaced from ``FaultWindow.frozen_sample_count`` and
       ``FaultWindow.raw_slot_count``, which the detector resolved at release
       (``None`` on a hand-built window, passed through as such).
    2. An open ``ChannelFreezeState`` matching the same onset: the freeze had
       not ended at end of input, so the span runs to ``last_ts``, the last
       frozen sample actually observed.  is_ongoing=True.  BOTH counts
       available, but asymmetrically: ``frozen_sample_count`` is the state's
       own ``sample_count`` tally, while the state holds NO slot counter by
       design, so ``raw_slot_count`` comes from ``expected_slot_count`` over
       the same two endpoints at the freeze's pinned cadence — the single
       canonical derivation that function's contract mandates for an open
       freeze, and the one ``anomaly_episode_writer`` already uses for the
       same state.  ``None`` when the state carries no usable cadence.
    3. No state supplied for this channel: the alert's own
       ``frozen_hours_at_detection`` (hours frozen at the GATE CROSSING) laid
       off from the onset.  is_ongoing=True.  NEITHER count available — the
       alert carries no sample tally and no cadence.  Both stay ``None``.
    4. Nothing recoverable: a zero-length span at the onset.  NEITHER count
       available; both stay ``None``.

    A count is never estimated, substituted or carried over from a losing
    arm: ``None`` means the winning arm did not know, and 0 would claim it
    did.

    Step 3 is a last resort, NOT the normal path for a still-open freeze.
    The alert is stamped once, at the gate crossing, and never revised, so
    it is short by however long the freeze ran on afterwards — 35 minutes on
    the 80-row ongoing fixture, unbounded in general.  Step 2 exists to
    avoid it and needs no new timestamp source beyond the carry the caller
    already holds.  Whatever the step, the end boundary and the duration
    come out of the SAME arithmetic, which is what keeps module docstring
    rule 5 true by construction: never derive one of them here without the
    other, and never reach for ``detected_ts`` as a third opinion.

    The sole caller invokes this only under ``detector == "stuck_fault"``,
    so the detector discrimination is structurally guaranteed — do not add
    a redundant guard here, and do not move the call out of that branch.

    Prose in ``src/dashboard/services/diagnosis_enricher.py`` and under
    ``tests/`` still cites this function's former name,
    ``_stuck_fault_duration``, from when it returned the duration alone.
    """
    onset_dt, first_alert = onset
    onset_ts = first_alert.timestamp
    fw_key = (actual_channel, onset_ts)
    fw = sources.fault_index.get(fw_key)

    if fw is not None:
        # Use the last frozen sample, not the release (first non-frozen)
        # sample.  last_frozen_dt is the correct boundary for the frozen span
        # and is what ``anomaly_episodes.end_ts`` holds; see FaultWindow's
        # docstring in stuck_fault_detector.py for the two-marker split.
        boundary_dt = fw.last_frozen_dt
        # Both counts come off the window that supplied the boundary, so all
        # four values describe one span.  They are surfaced, never recomputed:
        # frozen_sample_count is the detector's tally and raw_slot_count was
        # already resolved through expected_slot_count at release.  Either can
        # be None on a hand-built window; that None is passed through, because
        # "this window tracked no counts" is the truth in that case.
        return _StuckFaultSpan(
            end_dt=boundary_dt,
            duration_seconds=(boundary_dt - fw.onset_dt).total_seconds(),
            is_ongoing=False,
            frozen_sample_count=fw.frozen_sample_count,
            raw_slot_count=fw.raw_slot_count,
        )

    open_state = sources.open_freezes.get(actual_channel)
    if open_state is not None:
        end_dt = _open_freeze_end(open_state, onset_dt)
        if end_dt is not None:
            # Observed-so-far span of a freeze that never released: the real
            # last frozen sample, so this agrees with the anomaly_episodes
            # row the writer builds from the same carry state.
            return _StuckFaultSpan(
                end_dt=end_dt,
                duration_seconds=(end_dt - onset_dt).total_seconds(),
                is_ongoing=True,
                frozen_sample_count=_open_freeze_sample_count(open_state),
                raw_slot_count=_open_freeze_slot_count(
                    open_state, onset_dt, end_dt,
                ),
            )

    # Last resort (step 3) — no open state reached us for this channel, so
    # fall back to elapsed-at-the-gate as a lower bound.  Stuck hits carry
    # that in frozen_hours_at_detection; rows persisted before that field
    # existed still carry it in z_score, and there is no backfill migration.
    # The legacy read is safe only because the caller has already established
    # detector == "stuck_fault": a statistical row also has a numeric
    # z_score, so discriminating on z_score's presence instead of on the
    # detector would misread it as a duration.
    duration_hours = getattr(first_alert, "frozen_hours_at_detection", None)
    if duration_hours is None:
        duration_hours = getattr(first_alert, "z_score", None)
    if duration_hours is not None and duration_hours > 0:
        duration = float(duration_hours) * _SECONDS_PER_HOUR
        # No counts here, by nature of the source: the alert carries elapsed
        # hours at the gate crossing and nothing else — no sample tally, and
        # no cadence to turn the span into expected slots.  Both counts stay
        # None.  Deriving a slot count from this span and a default cadence
        # would be the fabricated-gap bug ChannelFreezeState's docstring
        # describes, dressed up as a measurement.
        return _StuckFaultSpan(
            end_dt=onset_dt + timedelta(seconds=duration),
            duration_seconds=duration,
            is_ongoing=True,
        )

    # Last resort: no duration is recoverable from the alert, so the span
    # collapses onto the onset — the one case where end_ts == start_ts is
    # the truthful answer, because duration_seconds is 0.0 too.  The counts
    # stay None rather than 0: a zero-length span is still a freeze that
    # froze samples, we simply have no record of how many.
    return _StuckFaultSpan(end_dt=onset_dt, duration_seconds=0.0, is_ongoing=True)


def _flush_group(
    group: list[tuple[datetime, Any]],
    episodes: list[AnomalyEpisode],
    sources: _StuckFaultSources,
) -> None:
    """Convert a contiguous group of alerts into an AnomalyEpisode."""
    if not group:
        return

    first_alert = group[0][1]
    detector = getattr(first_alert, "detector", "lstm") or "lstm"

    # Derive the actual channel name from the first alert (not the
    # composite group key which includes the detector suffix).
    actual_channel = getattr(first_alert, "column", None) or "__lstm__"

    start_dt = group[0][0]

    scores = [float(a.score) for _, a in group]
    peak = max(scores)
    severities = [getattr(a, "severity", None) for _, a in group]
    best_sev = max(severities, key=_severity_rank)

    # Resolve the episode's end boundary and duration together.  For a
    # stuck fault the alert rows do not describe the span (one alert per
    # freeze, stamped at onset), so both come from the frozen span; for
    # every other detector the alert rows ARE the span.
    is_ongoing = False
    frozen_sample_count: int | None = None
    raw_slot_count: int | None = None
    if detector == "stuck_fault":
        span = _stuck_fault_span(group[0], actual_channel, sources)
        end_dt = span.end_dt
        duration = span.duration_seconds
        is_ongoing = span.is_ongoing
        # The counts ride along with the boundary and duration that were
        # resolved from the same freeze; they are never filled in elsewhere.
        frozen_sample_count = span.frozen_sample_count
        raw_slot_count = span.raw_slot_count
    else:
        end_dt = group[-1][0]
        duration = (end_dt - start_dt).total_seconds()

    # Stuck-fault specific fields.
    onset_ts: str | None = None
    detected_ts: str | None = None
    frozen_value: float | None = None
    if detector == "stuck_fault":
        onset_ts = first_alert.timestamp
        detected_ts = getattr(first_alert, "detected_ts", None)
        frozen_value = getattr(first_alert, "value", None)

    episodes.append(AnomalyEpisode(
        channel=actual_channel,
        detector=detector,
        start_ts=start_dt.isoformat(),
        end_ts=end_dt.isoformat(),
        duration_seconds=duration,
        peak_score=peak,
        sample_count=len(group),
        severity=best_sev or "unclassified",
        # is_recovery omitted -- always its default (False) on this path,
        # see the field's docstring above for why.
        is_ongoing=is_ongoing,
        onset_ts=onset_ts,
        detected_ts=detected_ts,
        frozen_value=frozen_value,
        frozen_sample_count=frozen_sample_count,
        raw_slot_count=raw_slot_count,
    ))
