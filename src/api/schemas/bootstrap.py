"""Pydantic v2 schemas for the two-pass cold-start bootstrap API.

These DTOs represent the JSON contract for POST /api/v1/bootstrap/run.
Field names mirror the engine BootstrapRowResult dataclass exactly so
dataclasses.asdict() can be unpacked directly into the Pydantic model.
"""

from pydantic import BaseModel, Field


class BootstrapRowResult(BaseModel):
    """Per-row classification result from the two-pass bootstrap pipeline."""

    timestamp: str
    is_anomaly: bool
    reconstruction_loss: float
    pass1_filtered: bool
    root_cause_tags: list[str]
    contributing_sensors: list[str]
    physics_validation_status: str


class BootstrapRunResponse(BaseModel):
    """Top-level response from POST /api/v1/bootstrap/run.

    ``pass1_warnings`` (BACKLOG-14 Phase 2) surfaces Pass 1's operator-facing
    discard/MAD warnings to the caller — a tenant who just uploaded a year of
    data deserves to be told part of it was thrown away, not just have it
    logged server-side. Empty on a clean run; non-empty does not mean the
    pipeline failed (a "warn" discard status still returns 200 — only "fail"
    raises).
    """

    rows_ingested: int
    pass1_clean_rows: int
    pass1_anomaly_rows: int
    results: list[BootstrapRowResult]
    pass1_warnings: list[str] = Field(default_factory=list)
