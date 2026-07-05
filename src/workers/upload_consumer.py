"""Worker-side handlers for portioned CSV uploads.

handle_upload_portion: score + persist one portion (idempotent on redelivery).
handle_upload_finalize: calibrate the file-level threshold across all portions
and attach scores/metadata to persisted rows — race-free via try_transition.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.sensor_data import StatisticalAnomalyDetail
from src.api.services.cold_run_upload import (
    build_anomaly_metadata_by_timestamp,
    build_anomaly_scores_by_timestamp,
)
from src.api.services.csv_persist import apply_anomaly_scores, persist_readings_chunk
from src.api.services.upload_portions import (
    PortionScores,
    StatCarry,
    finalize_scores,
    score_portion,
)
from src.db.jobs import (
    get_job,
    increment_progress,
    load_portions_in_order,
    prune_portion_results,
    record_portion_result,
    set_job_result,
    try_transition,
)
from src.db.models import JobPortion
from src.mq.publisher import publish_json
from src.mq.schemas import UploadFinalizeMessage, UploadPortionMessage
from src.mq.topology import UPLOAD_FINALIZE_QUEUE

logger = logging.getLogger(__name__)


async def _portion_already_recorded(
    db: AsyncSession, job_id: str, portion_idx: int
) -> bool:
    result = await db.execute(
        select(func.count(JobPortion.id)).where(
            JobPortion.job_id == job_id,
            JobPortion.portion_idx == portion_idx,
        )
    )
    return bool(result.scalar())


def _portion_result_payload(
    portion: PortionScores, sensor_rows: int, raw_rows: int
) -> dict[str, Any]:
    return {
        "scores": portion.scores,
        "sequence_timestamps": portion.sequence_timestamps,
        "stat_hits": [hit.model_dump() for hit in portion.stat_hits],
        "rows_processed": portion.rows_processed,
        "stat_columns": None,  # filled below when known
        "sensor_rows": sensor_rows,
        "raw_rows": raw_rows,
    }


async def handle_upload_portion(
    msg: UploadPortionMessage,
    db: AsyncSession,
    *,
    attempt: int = 0,
) -> None:
    """Score one portion, persist its rows, record progress.

    Idempotent: a redelivered portion is acked without reprocessing, and row
    persistence is ON CONFLICT-upserted.
    """
    if await _portion_already_recorded(db, msg.job_id, msg.portion_idx):
        logger.info(
            "Portion %s/%s already recorded — redelivery ignored",
            msg.job_id,
            msg.portion_idx,
        )
        return

    await try_transition(db, msg.job_id, "pending", "running")

    # Local import keeps torch out of module import time for non-ML consumers.
    from src.api.routes.inference import get_cached_detector
    from src.models.artifact_store import resolve_checkpoint

    checkpoint_path = await resolve_checkpoint(db, msg.checkpoint_path)
    stat_carry = StatCarry(rows=list(msg.stat_carry_rows))

    def _score() -> PortionScores:
        # Torch model load + inference are CPU-bound for minutes on large
        # portions. Run OFF the event loop: a blocked loop cannot answer AMQP
        # heartbeats, and the broker then resets the connection mid-handling
        # (observed in production as 'Connection reset by peer' + requeue loop).
        detector = get_cached_detector(checkpoint_path)
        normalized_df = pd.DataFrame(msg.rows) if msg.rows else None
        overlap_df = pd.DataFrame(msg.overlap_rows) if msg.overlap_rows else pd.DataFrame()
        result, _ = score_portion(
            normalized_df=normalized_df,
            prev_overlap_df=overlap_df,
            raw_rows=msg.raw_rows,
            stat_carry=stat_carry,
            adaptive_ranges=[tuple(r) for r in msg.adaptive_ranges],
            detector=detector,
            seq_len=msg.seq_len,
            include_embeddings=False,
        )
        return result

    portion = await asyncio.to_thread(_score)

    sensor_rows = raw_rows = 0
    if msg.persist_data:
        persist_df = None
        if msg.rows:
            persist_df = pd.DataFrame(msg.rows)
            persist_df["timestamp"] = pd.to_datetime(persist_df["timestamp"])
        raw_persist_df = None
        if msg.raw_rows:
            raw_persist_df = pd.DataFrame(msg.raw_rows)
            raw_persist_df["timestamp"] = pd.to_datetime(raw_persist_df["timestamp"])
            if "source" not in raw_persist_df.columns:
                raw_persist_df["source"] = None
        s_ins, s_upd, r_ins, r_upd = await persist_readings_chunk(
            db=db,
            tenant_id=msg.tenant_id,
            greenhouse_id=msg.greenhouse_id,
            df=persist_df,
            raw_df=raw_persist_df,
        )
        await db.commit()
        sensor_rows = s_ins + s_upd
        raw_rows = r_ins + r_upd

    payload = _portion_result_payload(portion, sensor_rows, raw_rows)
    payload["stat_columns"] = stat_carry.columns
    recorded = await record_portion_result(
        db,
        job_id=msg.job_id,
        portion_idx=msg.portion_idx,
        status="completed",
        result=payload,
        attempt=attempt,
    )
    if not recorded:
        return  # concurrent duplicate delivery won the insert

    completed, failed, total = await increment_progress(db, msg.job_id)
    if total is not None and completed + failed >= total:
        await publish_json(UPLOAD_FINALIZE_QUEUE, UploadFinalizeMessage(job_id=msg.job_id))


async def record_portion_failure(
    db: AsyncSession,
    *,
    job_id: str,
    portion_idx: int,
    error: str,
    attempt: int,
) -> None:
    """Terminal failure path (after MQ_MAX_RETRIES): count it and maybe finalize."""
    recorded = await record_portion_result(
        db,
        job_id=job_id,
        portion_idx=portion_idx,
        status="failed",
        result={"error": error},
        attempt=attempt,
    )
    if not recorded:
        return
    completed, failed, total = await increment_progress(db, job_id, failed=True)
    if total is not None and completed + failed >= total:
        await publish_json(UPLOAD_FINALIZE_QUEUE, UploadFinalizeMessage(job_id=job_id))


async def handle_upload_finalize(msg: UploadFinalizeMessage, db: AsyncSession) -> None:
    """Assemble portions in order, calibrate the threshold, persist scores."""
    claimed = await try_transition(db, msg.job_id, "running", "finalizing")
    if not claimed:
        # Producer may finalize before any portion transitioned the status.
        claimed = await try_transition(db, msg.job_id, "pending", "finalizing")
    if not claimed:
        logger.info("Finalize for %s already claimed — skipping", msg.job_id)
        return

    job = await get_job(db, msg.job_id)
    if job is None:
        logger.error("Finalize for unknown job %s", msg.job_id)
        return

    portion_rows = await load_portions_in_order(db, msg.job_id)
    portions: list[PortionScores] = []
    stat_carry = StatCarry()
    sensor_rows_total = 0
    raw_rows_total = 0
    seen_hits: set[tuple[str, str]] = set()

    for row in portion_rows:
        if row.status != "completed" or not row.result:
            continue
        stored = row.result
        hits: list[StatisticalAnomalyDetail] = []
        for hit_data in stored.get("stat_hits", []):
            hit = StatisticalAnomalyDetail(**hit_data)
            key = (hit.timestamp, hit.column)
            if key in seen_hits:
                continue
            seen_hits.add(key)
            hits.append(hit)
        portions.append(
            PortionScores(
                scores=stored.get("scores", []),
                sequence_timestamps=stored.get("sequence_timestamps", []),
                stat_hits=hits,
                rows_processed=stored.get("rows_processed", 0),
            )
        )
        stat_carry.total_rows += stored.get("rows_processed", 0)
        if stat_carry.columns is None and stored.get("stat_columns"):
            stat_carry.columns = stored["stat_columns"]
        sensor_rows_total += stored.get("sensor_rows", 0)
        raw_rows_total += stored.get("raw_rows", 0)

    payload = job.payload or {}
    response, _ = finalize_scores(
        portions,
        tenant_id=job.tenant_id,
        greenhouse_id=job.greenhouse_id or "default",
        imputed_channels=payload.get("imputed_channels", []),
        stat_carry=stat_carry,
        include_embeddings=False,
    )

    if payload.get("persist_data"):
        await apply_anomaly_scores(
            db=db,
            tenant_id=job.tenant_id,
            greenhouse_id=job.greenhouse_id or "default",
            anomaly_scores_by_timestamp=build_anomaly_scores_by_timestamp(
                response.high_anomalies
            ),
            anomaly_metadata_by_timestamp=build_anomaly_metadata_by_timestamp(
                response.high_anomalies
            ),
        )
        response = response.model_copy(
            update={
                "data_persisted": True,
                "rows_uploaded": sensor_rows_total or raw_rows_total,
                "sensor_rows_uploaded": sensor_rows_total,
                "raw_rows_uploaded": raw_rows_total,
            }
        )

    final_status = "partially_failed" if job.failed_portions else "completed"
    await set_job_result(
        db,
        msg.job_id,
        status=final_status,
        result=response.model_dump(mode="json", exclude={"embeddings", "uncertainties"}),
    )
    await prune_portion_results(db, msg.job_id)