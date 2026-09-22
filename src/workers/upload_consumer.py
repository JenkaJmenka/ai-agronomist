"""Worker-side handlers for portioned CSV uploads.

handle_upload_portion: score + persist one portion (idempotent on redelivery).
handle_upload_finalize: calibrate the file-level threshold across all portions
and attach scores/metadata to persisted rows — race-free via try_transition.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.sensor_data import StatisticalAnomalyDetail, UploadHighAnomalyDetail
from src.api.services.cold_run_upload import (
    build_anomaly_metadata_by_timestamp,
    build_anomaly_scores_by_timestamp,
    build_trigger_data_by_timestamp,
    build_upload_score_dicts,
)
from src.api.services.csv_persist import apply_anomaly_scores, persist_readings_chunk
from src.api.services.upload_portions import (
    PortionScores,
    StatCarry,
    finalize_scores,
    score_portion,
)
from src.models.stuck_fault_detector import StuckCarry
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
from src.mq.schemas import (
    UPLOAD_PORTION_SCHEMA_VERSION,
    UploadFinalizeMessage,
    UploadPortionMessage,
)
from src.mq.topology import UPLOAD_FINALIZE_QUEUE
from src.utils.profiling import atimed, profile_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema-version gating
# ---------------------------------------------------------------------------
# Versions of UploadPortionMessage that this consumer can process correctly.
# A message whose schema_version is NOT in this set is failed immediately with
# a retryable error (the message is nacked into the retry queue and eventually
# the DLQ + terminal-failure hook, which records the portion as failed).
# Never add a version here without verifying that the handler produces correct
# results for it — "crash-free-but-wrong" is a failure.
_SUPPORTED_PORTION_MESSAGE_SCHEMA_VERSIONS: frozenset[int] = frozenset(
    {UPLOAD_PORTION_SCHEMA_VERSION}
)

# Versions of the per-portion RESULT payload (stored by record_portion_result)
# that _finalize can assemble correctly.  A result payload whose schema_version
# is missing or not in this set causes _finalize to raise (triggering retries
# and eventually the DLQ + terminal-failure hook for the finalize message).
_SUPPORTED_PORTION_RESULT_SCHEMA_VERSIONS: frozenset[int] = frozenset(
    {UPLOAD_PORTION_SCHEMA_VERSION}
)


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


def _merge_psychro_contexts(
    portions_in_order: list[tuple[int, dict[str, Any]]],
    job_id: str = "",
) -> dict[str, tuple[float, float, float]]:
    """Merge per-portion psychro_context dicts with lowest-portion-idx priority.

    Merge rule (approved 2026-09): union all per-portion dicts iterating
    portions in ascending ``portion_idx`` order.  On a duplicate timestamp
    the value from the LOWEST ``portion_idx`` wins (first-seen wins).  If
    the same timestamp appears in two portions with DIFFERING values a
    WARNING is logged — the same timestamp in two portions is expected only
    from overlap rows and should be identical; differing values indicate a
    data inconsistency.

    The result does NOT depend on the order in which portions COMPLETED —
    only on ``portion_idx``, which is fixed at publish time and immutable.

    This function is pure (no DB I/O, no shared state) and unit-testable.

    Args:
        portions_in_order: List of ``(portion_idx, raw_context)`` tuples
            where ``raw_context`` maps ISO timestamp to
            ``[T_air, RH, measured_hd]`` (as stored in the result payload —
            JSON lists, not tuples).  MUST be sorted by ``portion_idx``
            ascending before calling (``load_portions_in_order`` guarantees
            this for the DB-loaded path).
        job_id: Job identifier for log messages (empty string in unit tests).

    Returns:
        ``{timestamp: (T_air, RH, measured_hd)}`` with tuples restored.
    """
    merged: dict[str, tuple[float, float, float]] = {}
    seen_idx: dict[str, int] = {}  # timestamp -> winning portion_idx

    for portion_idx, raw_context in portions_in_order:
        for ts, values in raw_context.items():
            try:
                t_air = float(values[0])
                rh = float(values[1])
                hd = float(values[2])
            except (IndexError, TypeError, ValueError):
                logger.warning(
                    "psychro_context job=%s portion=%d ts=%s: "
                    "malformed value %r — skipped",
                    job_id,
                    portion_idx,
                    ts,
                    values,
                )
                continue
            new_val: tuple[float, float, float] = (t_air, rh, hd)
            if ts not in merged:
                merged[ts] = new_val
                seen_idx[ts] = portion_idx
            elif merged[ts] != new_val:
                logger.warning(
                    "psychro_context duplicate timestamp %s "
                    "in portions %d (value=%s) and %d (value=%s) "
                    "with DIFFERING values — job=%s, keeping portion %d value",
                    ts,
                    seen_idx[ts],
                    merged[ts],
                    portion_idx,
                    new_val,
                    job_id,
                    seen_idx[ts],
                )
            # else: identical values — keep existing (lowest portion_idx wins)

    return merged


def _portion_result_payload(
    portion: PortionScores,
    sensor_rows: int,
    raw_rows: int,
    stuck_carry: StuckCarry | None = None,
    stat_carry: StatCarry | None = None,
) -> dict[str, Any]:
    """Build the result payload stored by ``record_portion_result``.

    Includes ``schema_version`` so ``_finalize`` can detect and refuse
    results produced by an incompatible code version.

    ``psychro_context`` is serialised as ``{timestamp: [T_air, RH, hd]}``
    (JSON-safe lists; only finite floats).  ``_finalize`` restores the
    values as tuples and merges contexts from all portions in ascending
    ``portion_idx`` order so pair suppression in ``finalize_scores`` works
    identically on the async path to the synchronous path.
    """
    # Serialise psychro_context: {ts: (t, rh, hd)} → {ts: [t, rh, hd]}.
    # Only include entries with all-finite floats (guard against NaN/Inf
    # that could survive from malformed rows or carry initialisation).
    psychro_payload: dict[str, list[float]] = {}
    if stat_carry is not None:
        for ts, (t_air, rh, hd) in stat_carry.psychro_context.items():
            if math.isfinite(t_air) and math.isfinite(rh) and math.isfinite(hd):
                psychro_payload[ts] = [t_air, rh, hd]

    payload: dict[str, Any] = {
        "schema_version": UPLOAD_PORTION_SCHEMA_VERSION,
        "scores": portion.scores,
        "sequence_timestamps": portion.sequence_timestamps,
        "stat_hits": [hit.model_dump() for hit in portion.stat_hits],
        "stuck_hits": [hit.model_dump() for hit in portion.stuck_hits],
        "rows_processed": portion.rows_processed,
        "stat_columns": None,  # filled below when known
        "sensor_rows": sensor_rows,
        "raw_rows": raw_rows,
        "psychro_context": psychro_payload,
    }
    if stuck_carry is not None:
        payload["stuck_carry_state"] = stuck_carry.to_dict()
    return payload


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

    # ------------------------------------------------------------------
    # Schema-version gate (Part B).
    # Raise immediately so the existing retry/DLQ machinery handles it:
    # up to MQ_MAX_RETRIES retries, then on_terminal_failure →
    # record_portion_failure → increment_progress(failed=True) → finalize
    # fires with a partially_failed job.  The error names the versions and
    # instructs operators to drain and redeploy.
    # ------------------------------------------------------------------
    if msg.schema_version not in _SUPPORTED_PORTION_MESSAGE_SCHEMA_VERSIONS:
        raise ValueError(
            f"UploadPortionMessage schema_version={msg.schema_version} is not "
            f"supported by this worker "
            f"(supported: {sorted(_SUPPORTED_PORTION_MESSAGE_SCHEMA_VERSIONS)}). "
            "Drain the upload-portions queue and redeploy before reprocessing. "
            f"job_id={msg.job_id} portion_idx={msg.portion_idx}"
        )

    await try_transition(db, msg.job_id, "pending", "running")

    # Local import keeps torch out of module import time for non-ML consumers.
    from src.api.routes.inference import get_cached_detector
    from src.models.artifact_store import resolve_checkpoint

    try:
        checkpoint_path = await resolve_checkpoint(db, msg.checkpoint_path)
    except FileNotFoundError as exc:
        logger.warning(
            "Portion %s/%s: model deleted (%s). Failing immediately without retries.", 
            msg.job_id, msg.portion_idx, exc
        )
        await record_portion_failure(
            db,
            job_id=msg.job_id,
            portion_idx=msg.portion_idx,
            error=f"Upload failed: {exc}",
            attempt=attempt,
        )
        return

    stat_carry = StatCarry(
        rows=list(msg.stat_carry_rows),
        total_rows=msg.stat_carry_prior_rows,
    )

    # Deserialise the wall-clock stuck-fault carry state embedded by the producer.
    # The producer ran detect_chunk sequentially for each portion before publishing,
    # so each message carries the exact carry state that precedes this portion —
    # safe to use even when portions arrive out of order or are retried.
    stuck_carry: StuckCarry | None = (
        StuckCarry.from_dict(msg.stuck_carry_state)
        if msg.stuck_carry_state
        else None
    )

    def _score() -> tuple[PortionScores, StuckCarry | None]:
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
            stuck_carry=stuck_carry,
        )
        return result, stuck_carry

    async with atimed(
        logger,
        "upload.portion.score",
        job=msg.job_id,
        portion=msg.portion_idx,
        rows=len(msg.rows or []),
    ):
        portion, updated_stuck_carry = await asyncio.to_thread(_score)

    sensor_rows = raw_rows = 0
    if msg.persist_data:
        async with atimed(
            logger, "upload.portion.persist", job=msg.job_id, portion=msg.portion_idx
        ):
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
                # Worker re-sums the split into a total; skip the two
                # existing-key pre-SELECTs (2 fewer round-trips per portion).
                count_split=False,
            )
            await db.commit()
            sensor_rows = s_ins + s_upd
            raw_rows = r_ins + r_upd

    payload = _portion_result_payload(
        portion, sensor_rows, raw_rows, updated_stuck_carry, stat_carry
    )
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
        profile_event(logger, "upload.portions.complete", job=msg.job_id, portions=total)
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
    async with atimed(logger, "upload.finalize", job=msg.job_id):
        await _finalize(msg, db)


async def _finalize(msg: UploadFinalizeMessage, db: AsyncSession) -> None:
    claimed = await try_transition(db, msg.job_id, "running", "finalizing")
    if not claimed:
        # Producer may finalize before any portion transitioned the status.
        claimed = await try_transition(db, msg.job_id, "pending", "finalizing")
    if not claimed:
        # Still 'finalizing' means the claiming attempt died before writing a
        # result — set_job_result always moves the status to a terminal one.
        # Without this re-claim the retry short-circuits here and acks, so the
        # DLQ hook never fires and the job hangs in 'finalizing' forever with
        # no error surfaced (observed in production when a missing column made
        # apply_anomaly_scores raise).
        claimed = await try_transition(db, msg.job_id, "finalizing", "finalizing")
    if not claimed:
        logger.info("Finalize for %s already terminal — skipping", msg.job_id)
        return

    job = await get_job(db, msg.job_id)
    if job is None:
        logger.error("Finalize for unknown job %s", msg.job_id)
        return

    portion_rows = await load_portions_in_order(db, msg.job_id)
    if job.completed_portions and not any(row.result for row in portion_rows):
        # prune_portion_results ran, so a prior attempt finalized successfully.
        # Only a concurrent duplicate delivery reaches this. Recomputing would
        # overwrite a good result with an empty one.
        logger.info(
            "Finalize for %s: portion results already pruned — skipping", msg.job_id
        )
        return
    portions: list[PortionScores] = []
    stat_carry = StatCarry()
    sensor_rows_total = 0
    raw_rows_total = 0
    seen_hits: set[tuple[str, str]] = set()
    last_stuck_carry_state: dict[str, Any] | None = None
    # (portion_idx, psychro_context_dict) pairs for post-loop merge.
    # load_portions_in_order guarantees ascending portion_idx order.
    psychro_portions: list[tuple[int, dict[str, Any]]] = []

    for row in portion_rows:
        if row.status != "completed" or not row.result:
            continue
        stored = row.result

        # ------------------------------------------------------------------
        # Result schema-version gate (2a).
        # Missing or unsupported schema_version → raise immediately so the
        # existing MQ retry/DLQ machinery handles it (same mechanism as the
        # message-level gate in handle_upload_portion).
        # ------------------------------------------------------------------
        version = stored.get("schema_version")
        if version not in _SUPPORTED_PORTION_RESULT_SCHEMA_VERSIONS:
            raise ValueError(
                f"Portion result schema_version={version!r} is not supported "
                f"by this worker "
                f"(supported: {sorted(_SUPPORTED_PORTION_RESULT_SCHEMA_VERSIONS)}). "
                "Drain the upload-portions queue and redeploy before "
                "reprocessing. "
                f"job_id={msg.job_id} portion_idx={row.portion_idx}"
            )

        psychro_portions.append(
            (row.portion_idx, stored.get("psychro_context", {}))
        )
        hits: list[StatisticalAnomalyDetail] = []
        for hit_data in stored.get("stat_hits", []):
            hit = StatisticalAnomalyDetail(**hit_data)
            key = (hit.timestamp, hit.column)
            if key in seen_hits:
                continue
            seen_hits.add(key)
            hits.append(hit)

        # Collect per-portion stuck_fault alerts (detected during portion scoring).
        stuck_hits: list[UploadHighAnomalyDetail] = [
            UploadHighAnomalyDetail(**h) for h in stored.get("stuck_hits", [])
        ]

        portions.append(
            PortionScores(
                scores=stored.get("scores", []),
                sequence_timestamps=stored.get("sequence_timestamps", []),
                stat_hits=hits,
                stuck_hits=stuck_hits,
                rows_processed=stored.get("rows_processed", 0),
            )
        )
        stat_carry.total_rows += stored.get("rows_processed", 0)
        if stat_carry.columns is None and stored.get("stat_columns"):
            stat_carry.columns = stored["stat_columns"]
        sensor_rows_total += stored.get("sensor_rows", 0)
        raw_rows_total += stored.get("raw_rows", 0)

        # Track the last stored stuck_carry_state (portions are loaded in order
        # by portion_idx, so the last completed portion's state is the right one
        # for the flush call in finalize_scores).
        if stored.get("stuck_carry_state"):
            last_stuck_carry_state = stored["stuck_carry_state"]

    # Reconstruct the final stuck_carry for flush (still-open freezes at EOF).
    # Falls back to a fresh carry (with imputed_channels set) when no portion
    # stored a carry state — e.g. older job rows or all portions failed.
    payload = job.payload or {}
    if last_stuck_carry_state is not None:
        finalize_stuck_carry: StuckCarry | None = StuckCarry.from_dict(
            last_stuck_carry_state
        )
    elif payload.get("imputed_channels"):
        # No portions stored a carry state (legacy job or all portions failed),
        # but we still need to pass imputed_channels to avoid false stuck alerts.
        finalize_stuck_carry = StuckCarry(
            skip_channels=frozenset(payload["imputed_channels"])
        )
    else:
        finalize_stuck_carry = None

    # Restore and merge per-portion psychrometric contexts (2b).
    # psychro_portions is already in ascending portion_idx order (guaranteed
    # by load_portions_in_order ORDER BY portion_idx).  Lowest portion_idx
    # wins on duplicate timestamps — matches the sync path's first-seen rule.
    stat_carry.psychro_context = _merge_psychro_contexts(
        psychro_portions, job_id=msg.job_id
    )

    response, _ = finalize_scores(
        portions,
        tenant_id=job.tenant_id,
        greenhouse_id=job.greenhouse_id or "default",
        imputed_channels=payload.get("imputed_channels", []),
        stat_carry=stat_carry,
        include_embeddings=False,
        stuck_carry=finalize_stuck_carry,
    )

    if payload.get("persist_data"):
        # ADR-005: canonical scores + producer-native scores keyed by timestamp.
        _canonical_by_ts, _raw_by_ts = build_upload_score_dicts(
            response.high_anomalies,
        )
        await apply_anomaly_scores(
            db=db,
            tenant_id=job.tenant_id,
            greenhouse_id=job.greenhouse_id or "default",
            anomaly_scores_by_timestamp=_canonical_by_ts,
            anomaly_metadata_by_timestamp=build_anomaly_metadata_by_timestamp(
                response.high_anomalies
            ),
            anomaly_score_raw_by_timestamp=_raw_by_ts,
            score_scale="upload",
            trigger_data_by_timestamp=build_trigger_data_by_timestamp(
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
    result_payload = response.model_dump(mode="json", exclude={"embeddings", "uncertainties"})
    result_payload["model_trained"] = payload.get("model_trained", False)
    result_payload["model_id"] = payload.get("model_id")
    result_payload["training_skipped_reason"] = payload.get("training_skipped_reason")

    finalize_error = None
    if (
        payload.get("persist_data")
        and stat_carry.total_rows > 0
        and sensor_rows_total == 0
        and raw_rows_total == 0
    ):
        final_status = "partially_failed"
        finalize_error = (
            "Persistence was requested but 0 rows were written to "
            f"sensor_readings/raw_readings (processed {stat_carry.total_rows} data rows). "
            "Check the worker service logs."
        )
        logger.error(
            "Job %s: persist_data requested but 0 rows persisted (processed %s rows)",
            msg.job_id,
            stat_carry.total_rows,
        )

    await set_job_result(
        db,
        msg.job_id,
        status=final_status,
        result=result_payload,
        error=finalize_error,
    )
    await prune_portion_results(db, msg.job_id)
