"""Pydantic v2 event DTOs for the live monitoring SSE stream.

Each model maps to one SSE ``event:`` type. All ``channels`` dicts and score
fields enforce finite-float validation to prevent the known SQLite/Postgres
NaN gap (SQLite JSON silently accepts NaN; Postgres 500s).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, field_validator

from src.config import SENSOR_NAMES

# ---------------------------------------------------------------------------
# Canonical channel bounds for live monitoring.
#
# NOTE: ``src/vendors/base.py`` defines similar bounds inline inside
# ``validate_canonical_schema`` (a local variable in a static method).
# Those bounds are private to the validator — we define them here as the
# shared, importable source for the live-monitoring pipeline.  Vendors
# are intentionally left untouched.
# ---------------------------------------------------------------------------
CHANNEL_BOUNDS: Dict[str, Tuple[float, float]] = {
    "temperature": (0.0, 50.0),
    "humidity": (0.0, 100.0),
    "co2": (0.0, 1000.0),
    "light": (0.0, 2000.0),
    # Fertigation / environmental channels (not used by ML scorer).
    "soil_moisture": (0.0, 100.0),       # %
    "root_zone_temp": (0.0, 45.0),       # °C
    "vpd": (0.0, 4.0),                   # kPa
}

# The four canonical channels consumed by the ML scorer (LSTM-VAE).
# Shared source of truth for the live pipeline (mock episode targeting,
# tests) — see also ``CHANNEL_ORDER`` in ``src/live/attribution.py`` for
# the *ordered* model-input layout.
ML_CHANNELS: Set[str] = set(SENSOR_NAMES)


def sanitize_channels(channels: Dict[str, float]) -> Dict[str, float]:
    """Strip non-finite values from a channels dict.

    Returns a **new** dict containing only entries whose value passes
    ``math.isfinite``.  Use this to guard dicts before they reach a JSON
    serialiser that would produce ``NaN`` / ``Infinity`` literals.
    """
    return {k: v for k, v in channels.items() if math.isfinite(v)}


def _validate_finite(value: float, field_name: str) -> float:
    """Raise ``ValueError`` if *value* is not finite."""
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    return value


def _validate_channels_finite(channels: Dict[str, float]) -> Dict[str, float]:
    """Raise ``ValueError`` on any non-finite channel value."""
    for key, val in channels.items():
        if not math.isfinite(val):
            raise ValueError(
                f"Channel '{key}' must be finite, got {val!r}"
            )
    return channels


# ---------------------------------------------------------------------------
# Internal pre-merge partial reading (not an SSE event itself).
# ---------------------------------------------------------------------------
class SourceReading(BaseModel):
    """Internal pre-merge partial reading from a single data source.

    Each source may supply a subset of the canonical four channels.
    """

    source_id: str
    vendor: str
    tenant_id: str
    greenhouse_id: str
    timestamp: datetime
    channels: Dict[str, float]

    @field_validator("channels")
    @classmethod
    def _channels_finite(cls, v: Dict[str, float]) -> Dict[str, float]:
        return _validate_channels_finite(v)


# ---------------------------------------------------------------------------
# SSE event: ``reading``
# ---------------------------------------------------------------------------
class LiveReadingEvent(BaseModel):
    """Merged reading emitted as an SSE ``reading`` event.

    ``sources`` maps each channel name to the ``source_id`` that provided
    the value (provenance tracking).
    """

    seq: str
    timestamp: datetime
    tenant_id: str
    greenhouse_id: str
    channels: Dict[str, float]
    sources: Dict[str, str]
    anomaly_score: Optional[float] = None

    @field_validator("channels")
    @classmethod
    def _channels_finite(cls, v: Dict[str, float]) -> Dict[str, float]:
        return _validate_channels_finite(v)

    @field_validator("anomaly_score")
    @classmethod
    def _score_finite(cls, v: Optional[float]) -> Optional[float]:
        if v is not None:
            _validate_finite(v, "anomaly_score")
        return v


# ---------------------------------------------------------------------------
# SSE event: ``anomaly``
# ---------------------------------------------------------------------------
class LiveAnomalyEvent(BaseModel):
    """Anomaly alert emitted as an SSE ``anomaly`` event."""

    seq: str
    timestamp: datetime
    tenant_id: str
    greenhouse_id: str
    score: float
    threshold: float
    affected_channels: List[str]
    reasoning: str
    window_start: datetime
    window_end: datetime

    @field_validator("score", "threshold")
    @classmethod
    def _finite_score(cls, v: float, info: object) -> float:
        return _validate_finite(v, info.field_name if hasattr(info, "field_name") else "score")


# ---------------------------------------------------------------------------
# SSE event: ``anomaly-detail``
# ---------------------------------------------------------------------------
class AnomalyDetailEvent(BaseModel):
    """Enriched anomaly detail emitted as an SSE ``anomaly-detail`` event."""

    anomaly_seq: str
    tenant_id: str
    greenhouse_id: str
    reasoning: str
    fault_type: Optional[str] = None
    root_cause: Optional[str] = None
    incident_id: Optional[str] = None


# ---------------------------------------------------------------------------
# SSE event: ``source-status``
# ---------------------------------------------------------------------------
class SourceStatusEvent(BaseModel):
    """Data-source health status emitted as an SSE ``source-status`` event."""

    source_id: str
    vendor: str
    status: Literal["online", "degraded", "offline"]
    last_seen: Optional[datetime] = None
