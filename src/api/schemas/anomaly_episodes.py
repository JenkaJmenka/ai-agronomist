"""Pydantic v2 response models for GET /api/anomalies/episodes."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class AnomalyEpisodeSample(BaseModel):
    """One raw flagged row inside an episode's sample list."""

    timestamp: str
    anomaly_score: Optional[float] = None
    affected_sensors: list[str]
    reasoning: Optional[str] = None
    values: dict[str, float]
    detected_severity: Optional[str] = None


class AnomalyEpisodeSummary(BaseModel):
    """One contiguous anomaly event, merged from 5-minute flagged rows."""

    episode_id: str
    channel: str
    detector: str
    # Query-side-only, and meaningless for a stuck fault: a freeze has no
    # direction (it is a frozen value, not a drop or a spike), so this is
    # always None on a stuck-fault episode. Deliberately not reconciled
    # with the upload path (src/models/episode_grouper.py), which has no
    # direction concept at all — see anomaly_episode_service.py module doc.
    direction: Optional[Literal["drop", "spike"]] = None
    start_ts: str
    end_ts: str
    # Unknown (None), never a fabricated number, when a stuck fault has no
    # matching `anomaly_episodes` row to source it from (pre-migration-026
    # upload, or a no-DB-write preview) — see
    # anomaly_episode_service._augment_stuck_fault_episode. Every other
    # detector always has a float here (0.0 for a genuinely single-row
    # episode), since it is derived straight from the flagged rows' span.
    duration_seconds: Optional[float] = None
    # Alert count for a stuck-fault episode (always 1 — one alert fires at
    # onset, see WallclockStuckDetector._check_gate), NOT the flagged-row
    # count: the query-time grouper only ever flags that one row per
    # freeze. For every other detector this is the flagged-row count, as
    # before. Two different meanings under one field name — see
    # anomaly_episode_service._augment_stuck_fault_episode.
    sample_count: int
    peak_score: float
    peak_ts: str
    # "urgent"/"critical"/"warning" when a row in the episode carries a real
    # detected_severity; "unclassified" when none does (LSTM-only hits never
    # get one, and neither does the LSTM score-backfill/live-ingestion write
    # paths) — never "normal": no detector persists that value (see
    # episode_grouper._severity_rank). For a stuck-fault episode with a
    # matching `anomaly_episodes` row, this is that row's `severity` instead
    # of the row-derived value (same source value in practice, since the
    # table is written from the same alert).
    severity: str
    first_value: Optional[float] = None
    last_value: Optional[float] = None
    peak_value: Optional[float] = None
    # Band-relative: is this getting worse?  Only meaningful when `direction`
    # says which side of the band was breached, so "flat" whenever it is null.
    trend: Literal["worsening", "easing", "flat"]
    # Factual: which way the value moved.  Available for every channel,
    # including SCADA columns with no configured band.
    movement: Literal["rising", "falling", "steady"] = "steady"
    slope_per_hour: Optional[float] = None
    affected_sensors: list[str]
    headline: str
    is_truncated_start: bool
    is_truncated_end: bool
    samples: list[AnomalyEpisodeSample]
    # Stuck-fault-only, sourced from the `anomaly_episodes` table
    # (migration 026): finite samples the detector counted as frozen over
    # [onset_ts, end_ts]. None for every non-stuck-fault episode, and also
    # None for a stuck fault with no matching table row (see
    # duration_seconds above for why that case stays unknown rather than 0).
    frozen_sample_count: Optional[int] = None
    # Stuck-fault-only, sourced the same way: expected readings at the
    # channel's pinned cadence across the freeze — NOT a row tally (see
    # AnomalyEpisodeRecord.raw_slot_count's docstring in src/db/models.py).
    # None under the same two conditions as frozen_sample_count.
    raw_slot_count: Optional[int] = None


class AnomalyEpisodesResponse(BaseModel):
    """Response body for GET /api/anomalies/episodes."""

    episodes: list[AnomalyEpisodeSummary]
    episode_count: int
    anomaly_count: int
    threshold: float
    score_scale_version: int
