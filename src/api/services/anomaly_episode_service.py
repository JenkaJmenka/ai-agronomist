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
Contiguity uses the same gap rule as the upload path
(``ANOMALY_EPISODE_GAP_SECONDS``, defaulting to ``EPISODE_GAP_SECONDS``).

THE ONE CONSTRAINT THAT MUST NOT BE VIOLATED: episodes are built from EVERY
row with ``anomaly_score >= threshold`` in the window
(``_fetch_minimal_flagged_rows``), never from a top-N-by-score subset.
Grouping a top-N list manufactures gaps that do not exist in the data —
ranking and capping happen only *after* grouping.

The pure grouping core (no I/O, no pandas) lives in
``anomaly_episode_grouping.py`` and is re-exported here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.anomaly_episode_grouping import (  # noqa: F401
    build_episodes as build_episodes,
)
from src.api.services.anomaly_group_queries import _fetch_minimal_flagged_rows
from src.api.services.sample_enrichment import (
    _build_sample,
    _build_sorted_incidents,
    _enrich_samples_fast,
    _row_dict_from_minimal,
)
from src.config import (
    ANOMALY_EPISODE_GAP_SECONDS,
    ANOMALY_EPISODE_MAX,
    ANOMALY_TREND_FLAT_DEADBAND,
)


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

    # Rank by peak_score and cap AFTER grouping — capping first would
    # fracture contiguous events that only look separate under a score
    # cutoff (the constraint this module must not violate).
    episodes.sort(key=lambda ep: ep["peak_score"], reverse=True)
    episodes = episodes[:episode_max]

    _enrich_episode_samples(episodes, incidents)
    return episodes


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
