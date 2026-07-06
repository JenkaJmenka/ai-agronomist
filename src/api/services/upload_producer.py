"""Producer side of portioned uploads: stream a CSV into portion messages.

The API parses the file once, publishes one UploadPortionMessage per
UPLOAD_PORTION_ROWS rows (with the normalized/raw record split, the seq_len-1
scoring overlap and the rolling statistical carry), then stamps the job's
total_portions. Workers process portions independently; the last one — or the
producer, when workers finished first — fires the finalize message.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.cold_run_upload import (
    _calibrate_ranges_from_head,
    _read_chunks,
    _strip_raw_records,
)
from src.config import COLD_RUN_STAT_ROLLING_WINDOW, UPLOAD_PORTION_ROWS
from src.db.jobs import create_job, get_job, set_total_portions, update_job
from src.mq.publisher import publish_json
from src.mq.schemas import UploadFinalizeMessage, UploadPortionMessage
from src.mq.topology import UPLOAD_FINALIZE_QUEUE, UPLOAD_PORTIONS_QUEUE
from src.vendors.csv_adapter import CSVAdapter, parse_raw_rows

SENSOR_COLS = ["temperature", "humidity", "co2", "light"]


def _normalized_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Normalized sensor rows with ISO timestamps (JSON-safe)."""
    out = df[["timestamp", *SENSOR_COLS]].copy()
    out["timestamp"] = out["timestamp"].map(
        lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)
    )
    return out.to_dict(orient="records")


def _raw_records(raw_df: pd.DataFrame) -> list[dict[str, Any]]:
    """NaN-stripped raw records; keeps `source` for RawReading persistence."""
    stripped = _strip_raw_records(raw_df)
    if "source" in raw_df.columns:
        for record, source in zip(stripped, raw_df["source"].tolist()):
            if source is not None and not (isinstance(source, float) and pd.isna(source)):
                record["source"] = source
    return stripped


async def produce_upload_portions(
    *,
    tmp_path: str,
    adapter: CSVAdapter,
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    checkpoint_path: str | None,
    persist_data: bool,
    filename: str | None = None,
    seq_len: int = 20,
    portion_rows: int = UPLOAD_PORTION_ROWS,
    model_trained: bool = False,
    model_id: str | None = None,
    training_skipped_reason: str | None = None,
    existing_job_id: str | None = None,
) -> tuple[str, int]:
    """Create the job, publish one message per portion, stamp total_portions.

    When *existing_job_id* is set the job was created earlier (async upload
    ack); payload and status are updated here before portions are published.

    Returns (job_id, portion_count).
    """
    csv_props = adapter._csv_props
    adaptive_ranges, imputed_channels = _calibrate_ranges_from_head(
        tmp_path, portion_rows, csv_props, adapter
    )

    payload = {
        "filename": filename,
        "persist_data": persist_data,
        "checkpoint_path": checkpoint_path,
        "imputed_channels": imputed_channels,
        "adaptive_ranges": [list(r) for r in adaptive_ranges],
        "model_trained": model_trained,
        "model_id": model_id,
        "training_skipped_reason": training_skipped_reason,
    }

    if existing_job_id:
        job = await get_job(db, existing_job_id)
        if job is None:
            raise ValueError(f"Unknown job {existing_job_id!r}")
        merged_payload = {**(job.payload or {}), **payload}
        await update_job(db, existing_job_id, payload=merged_payload, status="pending")
        job_id = existing_job_id
    else:
        job = await create_job(
            db,
            tenant_id=tenant_id,
            job_type="upload_csv",
            greenhouse_id=greenhouse_id,
            payload=payload,
        )
        job_id = job.id

    portion_idx = 0
    prev_normalized_tail: list[dict[str, Any]] = []
    prev_raw_tail: list[dict[str, Any]] = []

    for raw_chunk in _read_chunks(tmp_path, portion_rows, csv_props):
        if raw_chunk.empty:
            continue
        try:
            normalized_chunk = adapter.normalize(raw_chunk)
            adapter.validate(normalized_chunk)
            normalized_rows = _normalized_records(normalized_chunk)
        except Exception:
            normalized_rows = []

        raw_df, _ = parse_raw_rows(raw_chunk)
        raw_rows = _raw_records(raw_df) if not raw_df.empty else []

        message = UploadPortionMessage(
            job_id=job_id,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            portion_idx=portion_idx,
            rows=normalized_rows,
            raw_rows=raw_rows,
            overlap_rows=prev_normalized_tail,
            stat_carry_rows=prev_raw_tail,
            adaptive_ranges=[list(r) for r in adaptive_ranges],
            checkpoint_path=checkpoint_path,
            seq_len=seq_len,
            persist_data=persist_data,
        )
        await publish_json(UPLOAD_PORTIONS_QUEUE, message)
        portion_idx += 1

        if normalized_rows:
            prev_normalized_tail = normalized_rows[-(seq_len - 1):]
        if raw_rows:
            prev_raw_tail = (prev_raw_tail + raw_rows)[-COLD_RUN_STAT_ROLLING_WINDOW:]

    completed, failed = await set_total_portions(db, job_id, portion_idx)
    if portion_idx > 0 and completed + failed >= portion_idx:
        # Workers beat the producer to the finish line — fire finalize here.
        await publish_json(UPLOAD_FINALIZE_QUEUE, UploadFinalizeMessage(job_id=job_id))

    return job_id, portion_idx
