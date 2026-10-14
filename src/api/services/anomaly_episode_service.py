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
"""

from __future__ import annotations

from datetime import datetime, timezone
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
