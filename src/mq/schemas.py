"""Message contracts for the job queues (Pydantic v2).

Rows travel inside messages — Railway services share no filesystem, so a
worker can never read a temp file written by the API container.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class UploadPortionMessage(BaseModel):
    """One scored+persisted portion of an uploaded CSV."""

    job_id: str
    tenant_id: str
    greenhouse_id: str
    portion_idx: int = Field(..., ge=0)
    portion_count: int = Field(..., ge=1)
    rows: list[dict[str, Any]]
    overlap_rows: list[dict[str, Any]] = Field(default_factory=list)
    stat_carry_rows: list[dict[str, Any]] = Field(default_factory=list)
    csv_props: dict[str, Any] = Field(default_factory=dict)
    adaptive_ranges: list[list[float]]
    checkpoint_path: Optional[str] = None
    seq_len: int = 20
    persist_data: bool = False


class UploadFinalizeMessage(BaseModel):
    """Fired once all portions of an upload job are recorded."""

    job_id: str


class OnlineTrainingPortionMessage(BaseModel):
    """A portion of sequences for the online-learning loop."""

    job_id: str
    tenant_id: str
    greenhouse_id: Optional[str] = None
    portion_idx: int = Field(..., ge=0)
    portion_count: int = Field(..., ge=1)
    sequences: list[list[list[float]]]


class RetrainJobMessage(BaseModel):
    """Full LSTM-VAE retraining job; the worker streams data from the DB."""

    job_id: str
    tenant_id: str
    baseline_checkpoint: Optional[str] = None
    epochs: int = Field(default=5, ge=1)
    portion_rows: int = Field(default=10000, ge=1)
