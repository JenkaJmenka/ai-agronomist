"""Message contracts for the job queues (Pydantic v2).

Rows travel inside messages — Railway services share no filesystem, so a
worker can never read a temp file written by the API container.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# Schema version for UploadPortionMessage and the per-portion result payload.
# Consumers that receive an unrecognised version MUST fail the job with a clear
# retryable error (name the received and supported versions, say "drain/redeploy").
# Finalize must not silently degrade to a partial result on an unsupported version.
#
# Version history:
#   v1 (legacy): no stuck_carry_state, no stat_carry_prior_rows semantics,
#                no 290-row stat carry, no psychro_context in result.
#                Messages/results produced before this field existed parse as v1
#                because the field defaults to 1.
#   v2 (current): stuck_carry_state (producer-embedded wall-clock carry),
#                 stat_carry_prior_rows (global row-index for stat detection),
#                 290-row stat carry window, psychro_context in portion result
#                 (enables async pair suppression identical to sync path).
UPLOAD_PORTION_SCHEMA_VERSION: int = 2


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

    ``schema_version`` identifies the message contract version (see
    ``UPLOAD_PORTION_SCHEMA_VERSION``).  Defaults to 1 so legacy messages
    published before this field existed are distinguishable from v2 messages.
    Consumers that do not recognise the version MUST fail the job immediately
    with a retryable error naming the versions; they must NOT silently process
    the message as if it were a supported version.
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
    # Message schema version (see UPLOAD_PORTION_SCHEMA_VERSION).
    # Defaults to 1 so pre-v2 messages are detected as legacy rather than
    # silently parsed as the current version.
    schema_version: int = Field(default=1)


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
