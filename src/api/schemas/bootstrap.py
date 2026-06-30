"""Pydantic v2 schemas for the two-pass cold-start bootstrap API.

These DTOs represent the JSON contract for POST /api/v1/bootstrap/run.
Field names mirror the engine BootstrapRowResult dataclass exactly so
dataclasses.asdict() can be unpacked directly into the Pydantic model.
"""

from pydantic import BaseModel


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
    """Top-level response from POST /api/v1/bootstrap/run."""

    rows_ingested: int
    pass1_clean_rows: int
    pass1_anomaly_rows: int
    results: list[BootstrapRowResult]
