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


class BootstrapPass1Warning(BaseModel):
    """One Pass-1 warning, mirroring ``src.models.pass1_warning.Pass1Warning``.

    ``code`` is a member of the closed set in that module
    (``PASS1_WARNING_CODES``); ``message`` is tenant-safe prose with no
    internal env var or config names in it (see that module's docstring).
    """

    code: str
    message: str


class BootstrapRunResponse(BaseModel):
    """Top-level response from POST /api/v1/bootstrap/run.

    ``pass1_warnings`` (BACKLOG-14 Phase 2) surfaces Pass 1's operator-facing
    discard/MAD warnings to the caller — a tenant who just uploaded a year of
    data deserves to be told part of it was thrown away, not just have it
    logged server-side. Empty on a clean run; non-empty does not mean the
    pipeline failed (a "warn" discard status still returns 200 — only "fail"
    raises).

    The field is a list of structured objects, not strings: Pass 1 now
    returns ``tuple[Pass1Warning, ...]`` (a `(code, message)` pair each, see
    ``src/models/pass1_warning.py``), and this schema mirrors that shape
    rather than collapsing it to prose the caller can't branch on.
    """

    rows_ingested: int
    pass1_clean_rows: int
    pass1_anomaly_rows: int
    results: list[BootstrapRowResult]
    pass1_warnings: list[BootstrapPass1Warning] = Field(default_factory=list)
