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
    direction: Optional[Literal["drop", "spike"]] = None
    start_ts: str
    end_ts: str
    duration_seconds: float
    sample_count: int
    peak_score: float
    peak_ts: str
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


class AnomalyEpisodesResponse(BaseModel):
    """Response body for GET /api/anomalies/episodes."""

    episodes: list[AnomalyEpisodeSummary]
    episode_count: int
    anomaly_count: int
    threshold: float
    score_scale_version: int
