"""Message contracts for the job queues (Pydantic v2).

Rows travel inside messages — Railway services share no filesystem, so a
worker can never read a temp file written by the API container.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class UploadPortionMessage(BaseModel):
    """One scored+persisted portion of an uploaded CSV.

    `rows` are adapter-normalized sensor records (ISO timestamp + canonical
    sensor columns); `raw_rows` are the NaN-stripped original records used for
    statistical detection and RawReading persistence — the worker never needs
    the CSV file or adapter.

    ``stuck_carry_state`` is the serialised ``StuckCarry`` state BEFORE this
    portion, computed by the producer during its sequential CSV pass.  Workers
    deserialise it, run ``detect_chunk``, and store the updated state alongside
    ``stuck_hits`` in the portion result so ``_finalize`` can call ``flush``
    with the carry from the last completed portion.

    Portions are published before the total is known and may be consumed out of
    order / concurrently; the producer-embedded carry is the only safe way to
    thread wall-clock stuck-fault state across portion boundaries.
    """

    job_id: str
    tenant_id: str
    greenhouse_id: str
    portion_idx: int = Field(..., ge=0)
    # 0 = unknown at publish time; jobs.total_portions is authoritative.
    portion_count: int = Field(default=0, ge=0)
    rows: list[dict[str, Any]]
    raw_rows: list[dict[str, Any]] = Field(default_factory=list)
    overlap_rows: list[dict[str, Any]] = Field(default_factory=list)
    stat_carry_rows: list[dict[str, Any]] = Field(default_factory=list)
    # Total raw rows seen by the producer *before* this portion (global row
    # index of stat_carry_rows[0]).  Workers use this to compute the
    # global-index mask for incremental statistical detection.  Defaults to
    # 0 so messages published before this field existed still parse.
    stat_carry_prior_rows: int = Field(default=0, ge=0)
    adaptive_ranges: list[list[float]]
    checkpoint_path: Optional[str] = None
    seq_len: int = 20
    persist_data: bool = False
    # Wall-clock stuck-fault carry state (producer-computed, portion-specific).
    stuck_carry_state: Optional[dict[str, Any]] = None


class UploadPrepareMessage(BaseModel):
    """Instruction to prepare an uploaded CSV on the worker: cold-start train
    (when needed), then publish scored portions. The CSV itself lives in the
    artifact store; only its db:// reference travels here."""

    job_id: str
    tenant_id: str
    greenhouse_id: str = "default"
    blob_path: str                 # db://model_artifacts/uploads/<job_id>
    filename: Optional[str] = None
    persist_data: bool = False
    train_model: bool = False
    seq_len: int = 20              # SEQ_LEN default; carried for forward-compat


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
