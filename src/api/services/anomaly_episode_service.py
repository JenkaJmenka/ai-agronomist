"""Query-time anomaly episode merging — DB-facing entry point.

The upload path already merges contiguous alerts into episodes at detection
time (``src/models/episode_grouper.py``, inside ``finalize_scores``), but
that result is returned once in the upload response and never persisted or
queried again.  This module adds the same episode concept for historical
data, queried on demand by ``GET /api/anomalies/episodes``.

Grouping key: ``(channel, detector, direction)`` — mirrors the upload path's
``(channel, detector)`` key (a ``stuck_fault`` must never merge into a
``statistical`` episode on the same channel, see
``docs/architecture/episode_grouping.md``) plus a ``direction`` split (a
humidity drop and a humidity spike 20 minutes apart are different events).
``direction`` is query-side-only and is meaningless for a stuck fault (a
freeze has no direction, see ``_RowInfo.direction`` derivation) — this is
documented, not reconciled with the upload path, which has no direction
concept at all.
Contiguity uses the same gap rule as the upload path
(``ANOMALY_EPISODE_GAP_SECONDS``, defaulting to ``EPISODE_GAP_SECONDS``).

THE ONE CONSTRAINT THAT MUST NOT BE VIOLATED: episodes are built from EVERY
row with ``anomaly_score >= threshold`` in the window
(``_fetch_minimal_flagged_rows``), never from a top-N-by-score subset.
Grouping a top-N list manufactures gaps that do not exist in the data —
ranking and capping happen only *after* grouping.

The pure grouping core (no I/O, no pandas) lives in
``anomaly_episode_grouping.py`` and is re-exported here.

Not to be confused with the ``anomaly_episodes`` DB table (migration 026,
``src/db/models.py``'s ``AnomalyEpisodeRecord``): that table holds rows a
detector writes *at detection time*, one per fault window, and persists
them. This module computes its episodes fresh on every request from
already-flagged ``sensor_readings`` and persists nothing — same name on the
table, deliberately kept, opposite mechanism.

Stuck-fault exception: a freeze emits exactly ONE flagged ``sensor_readings``
row (the alert at ``onset_ts`` — see ``WallclockStuckDetector._check_gate``),
so the row-derived span for a stuck-fault episode is always a single-row
group: ``duration_seconds`` would always read 0.0 and the frozen/raw counts
would always be unknown. ``_augment_stuck_fault_episodes`` below replaces
those fields, for stuck-fault episodes only, by looking up the matching
``anomaly_episodes`` row and sourcing the real span from it. Every other
detector keeps the pure row-derived behaviour in
``anomaly_episode_grouping.py``.

Cross-channel recovery tagging (Task 5): ``is_recovery`` marks a
non-stuck-fault episode whose start falls no earlier than
``EPISODE_GAP_SECONDS`` before, and within ``RECOVERY_KNOCKON_WINDOW_HOURS``
after, a RELEASE of a stuck fault on a DIFFERENT channel -- a controller
response to the fault clearing (e.g. a heating setpoint excursion after a
humidity freeze releases), not an independent event. ``EPISODE_GAP_SECONDS``
rather than a dedicated grace constant: ``release_ts`` lags the true change
by up to one sample/gap, so an episode starting fractionally before the
release is within this pipeline's own measurement resolution, not a
separately-tuned fudge factor -- see ``docs/handoff/task5_spec.md`` for the
rejected overlap-based alternative and why. Tagging is informational only:
it never suppresses the hit or changes its score or rank. This lives here,
not in ``src/models/episode_grouper.py``, because the tagging needs
cross-upload reach -- ``stuck_carry.recovery_channels`` is in-memory per
upload and cannot see a fault released by an earlier one, while this
module already reads the persisted ``anomaly_episodes`` table for the
stuck-fault-augmentation purpose above. See
``_apply_cross_channel_recovery_tags`` and
``docs/handoff/task5_spec.md``. A hit on the SAME channel as its own fault,
within the post-release suppression window, is handled independently and
earlier -- at upload time, by never persisting/flagging that row at all
(``COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS`` in
``src/api/services/upload_portions.py``) -- so it never reaches this module
to be tagged either way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.anomaly_episode_grouping import (  # noqa: F401
    build_episodes as build_episodes,
)
from src.api.services.anomaly_group_queries import _fetch_minimal_flagged_rows
from src.api.services.sample_enrichment import (
    _build_sample,
    _build_sorted_incidents,
    _enrich_samples_fast,
    _iso,
    _row_dict_from_minimal,
)
from src.config import (
    ANOMALY_EPISODE_GAP_SECONDS,
    ANOMALY_EPISODE_MAX,
    ANOMALY_TREND_FLAT_DEADBAND,
    EPISODE_GAP_SECONDS,
    RECOVERY_KNOCKON_WINDOW_HOURS,
)
from src.db.models import AnomalyEpisodeRecord
from src.models.episode_grouper import _parse_iso
from src.models.fault_classifier import FaultType

# Single source of truth for the detector name is FaultType — see
# fault_classification.py and anomaly_episode_writer.py, which both key off
# the same value rather than a second hardcoded literal.
_STUCK_FAULT_DETECTOR: str = FaultType.STUCK_FAULT.value


async def fetch_anomaly_episodes(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    threshold: float,
    sample_limit: int,
    incidents: list[dict[str, Any]],
    gap_seconds: int = ANOMALY_EPISODE_GAP_SECONDS,
    deadband: float = ANOMALY_TREND_FLAT_DEADBAND,
    episode_max: int = ANOMALY_EPISODE_MAX,
) -> list[dict[str, Any]]:
    """Fetch every flagged row in the window, group, rank, cap, and enrich.

    Uses ``_fetch_minimal_flagged_rows`` — EVERY row with
    ``anomaly_score >= threshold``, never a top-N-by-score subset — so
    episodes reflect the true contiguity of the underlying data.  Never
    clamps the window to "now": readings can be future-dated
    (project_future_dated_readings).
    """
    minimal_rows = await _fetch_minimal_flagged_rows(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id,
        start=start, end=end, threshold=threshold,
    )
    if not minimal_rows:
        return []

    row_dicts = [
        _row_dict_from_minimal(
            r.timestamp, r.anomaly_score, r.temperature, r.humidity, r.co2, r.light,
            r.anomaly_type,
            trigger_channel=getattr(r, "trigger_channel", None),
            trigger_detector=getattr(r, "trigger_detector", None),
            detected_severity=getattr(r, "detected_severity", None),
            trigger_value=getattr(r, "trigger_value", None),
        )
        for r in minimal_rows
    ]

    episodes = build_episodes(
        row_dicts, gap_seconds=gap_seconds, deadband=deadband, sample_limit=sample_limit,
        window_start=start, window_end=end,
    )
    if not episodes:
        return []

    await _augment_stuck_fault_episodes(
        db, episodes, tenant_id=tenant_id, greenhouse_id=greenhouse_id,
        start=start, end=end,
    )

    # Rank by peak_score and cap AFTER grouping — capping first would
    # fracture contiguous events that only look separate under a score
    # cutoff (the constraint this module must not violate).
    episodes.sort(key=lambda ep: ep["peak_score"], reverse=True)
    episodes = episodes[:episode_max]

    # Task 5: tag cross-channel recovery hits. Runs on the capped list --
    # tagging is informational only (never touches peak_score or the
    # ranking/cap above it just ran), so tagging episodes the cap already
    # dropped would be wasted work.
    await _apply_cross_channel_recovery_tags(
        db, episodes, tenant_id=tenant_id, greenhouse_id=greenhouse_id,
        start=start, end=end,
    )

    _enrich_episode_samples(episodes, incidents)
    return episodes


async def _fetch_stuck_fault_episode_records(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    channels: set[str],
    start: datetime,
    end: datetime,
) -> dict[tuple[str, datetime], AnomalyEpisodeRecord]:
    """Load persisted stuck-fault rows for this window, keyed by (channel, onset_ts).

    Restricted to ``channels`` actually present among this request's
    stuck-fault episodes. The window filter is an index-friendly narrowing,
    not a correctness requirement: the alert row that produced each episode
    is itself inside ``[start, end]``, and its ``onset_ts`` is that same
    timestamp (see module docstring), so a matching record's ``onset_ts`` is
    always inside the window too.
    """
    stmt = select(AnomalyEpisodeRecord).where(
        AnomalyEpisodeRecord.tenant_id == tenant_id,
        AnomalyEpisodeRecord.greenhouse_id == greenhouse_id,
        AnomalyEpisodeRecord.detector == _STUCK_FAULT_DETECTOR,
        AnomalyEpisodeRecord.channel.in_(channels),
        AnomalyEpisodeRecord.onset_ts >= start,
        AnomalyEpisodeRecord.onset_ts <= end,
    )
    result = await db.execute(stmt)
    return {
        (record.channel, _as_utc(record.onset_ts)): record
        for record in result.scalars().all()
    }


def _as_utc(value: datetime) -> datetime:
    """Normalize a possibly tz-naive datetime to tz-aware UTC.

    SQLite (every dev/test backend) returns a naive ``datetime`` from a
    ``DateTime(timezone=True)`` column; asyncpg (production Postgres)
    returns an aware one. Without this, the dict key built here from
    ``record.onset_ts`` only ever matched the tz-aware key built from
    ``_parse_iso(episode["start_ts"])`` on Postgres — every stuck fault
    fell through to the absent-record branch under SQLite, silently, since
    both branches return a valid-looking response. Mirrors ``_parse_iso``'s
    own naive-defaults-to-UTC assumption (``src/models/episode_grouper.py``)
    rather than inventing a second rule.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _augment_stuck_fault_episodes(
    db: AsyncSession,
    episodes: list[dict[str, Any]],
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
) -> None:
    """Source stuck-fault episodes from ``anomaly_episodes``, in place.

    No-ops (no query issued) when no episode in this batch is a stuck
    fault. Every other detector's episode is untouched.
    """
    stuck_channels = {
        episode["channel"] for episode in episodes
        if episode["detector"] == _STUCK_FAULT_DETECTOR
    }
    if not stuck_channels:
        return

    records_by_key = await _fetch_stuck_fault_episode_records(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id,
        channels=stuck_channels, start=start, end=end,
    )
    for episode in episodes:
        if episode["detector"] != _STUCK_FAULT_DETECTOR:
            continue
        key = (episode["channel"], _parse_iso(episode["start_ts"]))
        _augment_stuck_fault_episode(episode, records_by_key.get(key))


def _augment_stuck_fault_episode(
    episode: dict[str, Any],
    record: AnomalyEpisodeRecord | None,
) -> None:
    """Replace the row-derived span with the detector's persisted one, in place.

    The query-time grouper sees exactly one flagged row per freeze, so
    without ``record`` the episode's ``duration_seconds`` reads 0.0 — a
    frozen channel misreported as having recovered instantly. This is THE
    BUG described in the module docstring.

    ``record`` is ``None`` for a stuck fault whose alert row exists in
    ``sensor_readings`` but whose ``anomaly_episodes`` row does not — e.g.
    uploaded before migration 026, or persisted on the no-DB-write preview
    path (``persist_db is None``, see ``anomaly_episode_writer.py``). In
    that case the real span is simply not recoverable from what this
    request has: report ``duration_seconds`` (and the two sample counts) as
    ``None`` — unknown — rather than silently repeating the false 0.0, and
    without fabricating a row to fill the gap.

    ``sample_count`` is set unconditionally to 1 per the field contract: for
    a stuck-fault episode it is the alert count (always one alert fired at
    onset), never the flagged-row count — see
    ``AnomalyEpisodeSummary.sample_count``.
    """
    episode["sample_count"] = 1
    if record is None:
        episode["duration_seconds"] = None
        episode["frozen_sample_count"] = None
        episode["raw_slot_count"] = None
        return

    # end_ts and duration_seconds leave together, from the same record, so
    # the boundary and the duration can never describe different spans
    # (episode_grouper.py design decision 5 -- end_ts - start_ts ==
    # duration_seconds holds for every episode, every detector).
    episode["end_ts"] = _iso(_as_utc(record.end_ts))
    episode["duration_seconds"] = (
        float(record.duration_seconds) if record.duration_seconds is not None else None
    )
    episode["severity"] = record.severity
    episode["frozen_sample_count"] = record.frozen_sample_count
    episode["raw_slot_count"] = record.raw_slot_count


async def _fetch_recovery_release_events(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    window_hours: float,
    gap_seconds: int,
) -> list[tuple[str, datetime]]:
    """Load released stuck-fault ``(channel, release_ts)`` pairs for tagging.

    An episode starting at ``start`` can only be tagged by a release at or
    after ``start - window_hours`` (the forward side of the tagging window,
    read backward); an episode starting at ``end`` can only be tagged by a
    release at or before ``end + gap_seconds`` (``gap_seconds`` -- reused
    from ``EPISODE_GAP_SECONDS`` -- lets a release arrive slightly AFTER an
    episode already underway still tag it; see ``_is_cross_channel_recovery_hit``
    for why this is a resolution bound, not a grace fudge factor). Both ends
    narrow the scan to every release that could matter for some episode in
    ``[start, end]`` without excluding one that could.

    Deliberately NOT restricted to channels already present in this batch
    (contrast ``_fetch_stuck_fault_episode_records`` above, which is): a
    release can tag a hit on a channel whose OWN stuck-fault alert row falls
    outside this window, or in a different upload entirely -- that
    cross-upload reach is the entire reason this lives at query time (see
    ``_apply_cross_channel_recovery_tags``).

    Filtered on tenant_id/greenhouse_id/detector, the leading columns shared
    with the existing ``anomaly_episodes`` indexes; ``release_ts`` itself
    carries no dedicated index -- see docs/handoff/task5_spec.md for the
    trade-off this accepts rather than adding one.
    """
    lower_bound = start - timedelta(hours=window_hours)
    upper_bound = end + timedelta(seconds=gap_seconds)
    stmt = select(
        AnomalyEpisodeRecord.channel, AnomalyEpisodeRecord.release_ts,
    ).where(
        AnomalyEpisodeRecord.tenant_id == tenant_id,
        AnomalyEpisodeRecord.greenhouse_id == greenhouse_id,
        AnomalyEpisodeRecord.detector == _STUCK_FAULT_DETECTOR,
        AnomalyEpisodeRecord.release_ts.is_not(None),
        AnomalyEpisodeRecord.release_ts >= lower_bound,
        AnomalyEpisodeRecord.release_ts <= upper_bound,
    )
    result = await db.execute(stmt)
    return [(channel, _as_utc(release_ts)) for channel, release_ts in result.all()]


def _is_cross_channel_recovery_hit(
    episode: dict[str, Any],
    releases: list[tuple[str, datetime]],
    window_hours: float,
    gap_seconds: int,
) -> bool:
    """True iff *episode* starts in [release - gap, release + window] on another channel.

    Never true for a stuck-fault episode: it IS the faulted channel, not a
    downstream hit on one (see module docstring and
    ``AnomalyEpisodeSummary.is_recovery`` for the full contract).

    The lower bound is ``EPISODE_GAP_SECONDS`` (``gap_seconds``), not a
    dedicated tuned constant: ``release_ts`` is "the first non-frozen
    sample after the freeze ends" (``AnomalyEpisodeRecord`` docstring,
    ``src/db/models.py``), so it can lag the true end of the fault by up to
    one sample/gap -- the same resolution the pipeline already uses to
    decide what counts as "contiguous" everywhere else. An episode starting
    fractionally before ``release_ts`` is within that resolution, not
    actually before the fault cleared. The upper bound
    (``RECOVERY_KNOCKON_WINDOW_HOURS``) is unrelated and unchanged.

    Overlap-based tagging (checking whether the episode's span overlaps
    the fault's span, rather than anchoring on the episode's start) was
    considered and rejected: it would tag a long pre-existing excursion on
    another channel -- one that started well before the fault and simply
    happens to still be running when it releases -- as a recovery response,
    which it is not.
    """
    if episode["detector"] == _STUCK_FAULT_DETECTOR:
        return False
    channel = episode["channel"]
    start_dt = _parse_iso(episode["start_ts"])
    gap = timedelta(seconds=gap_seconds)
    return any(
        release_channel != channel
        and -gap <= (start_dt - release_dt) <= timedelta(hours=window_hours)
        for release_channel, release_dt in releases
    )


async def _apply_cross_channel_recovery_tags(
    db: AsyncSession,
    episodes: list[dict[str, Any]],
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    window_hours: float = RECOVERY_KNOCKON_WINDOW_HOURS,
    # Deliberately EPISODE_GAP_SECONDS, NOT ANOMALY_EPISODE_GAP_SECONDS --
    # this module's OWN query-time grouping default, on `fetch_anomaly_episodes`'s
    # `gap_seconds: int = ANOMALY_EPISODE_GAP_SECONDS` parameter above. The
    # two are intentionally separate knobs (see ANOMALY_EPISODE_GAP_SECONDS's
    # docstring in src/config.py) and this is not a copy-paste slip between
    # them: the grace here compensates for lag in `release_ts`, which the
    # stuck-fault detector writes at DETECTION time, so its resolution is
    # the detection-time cadence (EPISODE_GAP_SECONDS), not the query-side
    # regrouping knob that produced this episode's `start_ts`. Both
    # currently default to 600, so nothing misbehaves today either way --
    # the distinction only bites if someone sets ANOMALY_EPISODE_GAP_SECONDS
    # to something else, at which point using that constant here instead
    # would silently regroup episodes at one gap while granting recovery
    # grace at another.
    gap_seconds: int = EPISODE_GAP_SECONDS,
) -> None:
    """Set ``is_recovery`` on every episode in place (Task 5).

    Cross-upload by construction: reads every released stuck-fault
    ``anomaly_episodes`` row in range regardless of which upload wrote it --
    the whole reason this tagging lives here and not in
    ``src/models/episode_grouper.py``, whose ``stuck_carry.recovery_channels``
    is per-upload and cannot see a fault released by an earlier one.

    Purely additive: never reorders ``episodes`` or touches ``peak_score``,
    so it is safe to run after the rank-then-cap step in
    ``fetch_anomaly_episodes``.

    No-ops (no query issued) when every episode in this batch is itself a
    stuck fault -- there is no non-stuck-fault episode left to tag, and a
    stuck-fault episode is always ``is_recovery=False``.
    """
    if all(ep["detector"] == _STUCK_FAULT_DETECTOR for ep in episodes):
        for ep in episodes:
            ep["is_recovery"] = False
        return

    releases = await _fetch_recovery_release_events(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id,
        start=start, end=end, window_hours=window_hours,
        gap_seconds=gap_seconds,
    )
    for episode in episodes:
        episode["is_recovery"] = _is_cross_channel_recovery_hit(
            episode, releases, window_hours, gap_seconds,
        )


def _enrich_episode_samples(
    episodes: list[dict[str, Any]], incidents: list[dict[str, Any]],
) -> None:
    """Batch-enrich each episode's top-N sample rows in place.

    Enriches ONLY the collected sample rows (small N <= N_episodes x
    sample_limit), not every row in every episode — mirrors
    anomaly_group_service._collect_and_enrich_group_samples.
    """
    sorted_times, sorted_incidents = _build_sorted_incidents(incidents)

    sample_raw: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        for row in episode["sample_row_dicts"]:
            ts = str(row.get("timestamp") or "")
            if ts and ts not in sample_raw:
                sample_raw[ts] = row

    enriched_list = (
        _enrich_samples_fast(list(sample_raw.values()), sorted_times, sorted_incidents)
        if sample_raw
        else []
    )
    enriched_by_ts = {str(e.get("timestamp") or ""): e for e in enriched_list}

    for episode in episodes:
        samples: list[dict[str, Any]] = []
        for row in episode.pop("sample_row_dicts"):
            ts = str(row.get("timestamp") or "")
            enriched = enriched_by_ts.get(
                ts, {"timestamp": ts, "anomaly_score": row.get("anomaly_score")},
            )
            samples.append(_build_sample(enriched))
        episode["samples"] = samples
