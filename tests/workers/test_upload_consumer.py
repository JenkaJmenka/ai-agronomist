"""Tests for portioned upload: producer → portion handlers → finalize.

No broker involved: publish_json is captured, and handlers are called
directly — exactly what the worker loop does per message.
"""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.jobs import get_job, try_transition
from src.db.models import Base, JobPortion, RawReading, SensorReading
from src.mq.schemas import UploadFinalizeMessage, UploadPortionMessage
from src.workers.upload_consumer import (
    handle_upload_finalize,
    handle_upload_portion,
    record_portion_failure,
)

TENANT = "tenant_portions"
ROWS = 130


# Accelerating ramp: the rolling baseline absorbs each anomalous row, so only
# values that outrun the inflating std keep z >= COLD_RUN_STAT_Z_MIN for the
# COLD_RUN_STAT_MIN_CONSECUTIVE rows the detector requires.
#
# IMPORTANT: all values must be <= 50.0 °C — the upper bound enforced by
# CSVAdapter.validate_canonical_schema (src/vendors/base.py).  Exceeding it
# causes the adapter to reject the entire portion chunk, leaving those rows
# absent from sensor_readings so apply_anomaly_scores cannot set trigger_channel.
# Normal baseline mean ≈ 21.5 °C, std ≈ 1.0 °C; z-threshold = 5.5 → need ≥ 27 °C.
# These values produce z-scores of ≈ 8.5, 13.5, 18.5, 23.5, 27.5 — all well
# above the 5.5 threshold — while staying within the adapter's 50 °C cap.
_FAULT_RAMP = (30.0, 35.0, 40.0, 45.0, 49.0)


def _csv_content(rows: int = ROWS, fault_at: int | None = None) -> str:
    def _temp(i: int) -> float:
        if fault_at is not None and fault_at <= i < fault_at + len(_FAULT_RAMP):
            return _FAULT_RAMP[i - fault_at]
        return 20 + (i % 7) * 0.5

    return "timestamp,temperature,humidity,co2,light\n" + "\n".join(
        f"2026-06-09T{11 + i // 60:02d}:{i % 60:02d}:00Z,"
        f"{_temp(i)},{50 + (i % 5)},{400 + i % 11},{1000 + i % 13}"
        for i in range(rows)
    )


def _mock_detector() -> MagicMock:
    detector = MagicMock()
    detector.predict_batch_enriched.side_effect = lambda seqs: (
        np.asarray([float(np.mean(s)) for s in seqs], dtype=np.float32),
        np.zeros((len(seqs), 8), dtype=np.float32),
        np.zeros(len(seqs), dtype=np.float32),
    )
    return detector


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


async def _produce_messages(session, csv_content: str, portion_rows: int, persist=True):
    """Run the producer with publish captured; returns (job_id, messages)."""
    from src.api.services.upload_producer import produce_upload_portions
    from src.vendors.csv_adapter import CSVAdapter

    published: list[tuple[str, object]] = []

    async def capture(routing_key, payload, **kwargs):
        published.append((routing_key, payload))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write(csv_content)
        tmp_name = tmp.name
    try:
        adapter = CSVAdapter({"filepath": tmp_name})
        adapter.connect()
        with patch(
            "src.api.services.upload_producer.publish_json", side_effect=capture
        ):
            job_id, portion_count = await produce_upload_portions(
                tmp_path=tmp_name,
                adapter=adapter,
                db=session,
                tenant_id=TENANT,
                greenhouse_id="default",
                checkpoint_path=None,
                persist_data=persist,
                filename="data.csv",
                portion_rows=portion_rows,
            )
    finally:
        os.unlink(tmp_name)
    portion_msgs = [m for key, m in published if key == "upload.portions"]
    assert portion_count == len(portion_msgs)
    return job_id, portion_msgs


async def _consume_all(session, messages, detector):
    """Feed portion messages to the handler; returns finalize messages fired."""
    finalize_msgs: list[UploadFinalizeMessage] = []

    async def capture(routing_key, payload, **kwargs):
        finalize_msgs.append(payload)

    with patch(
        "src.api.routes.inference.get_cached_detector", return_value=detector
    ), patch("src.workers.upload_consumer.publish_json", side_effect=capture):
        for msg in messages:
            await handle_upload_portion(msg, session)
    return finalize_msgs


async def test_portion_processing_persists_and_finalizes(session):
    job_id, messages = await _produce_messages(session, _csv_content(), portion_rows=50)
    assert len(messages) == 3

    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    assert len(finalize_msgs) == 1  # only the last portion fires finalize

    sensor_count = await session.execute(
        select(func.count(SensorReading.id)).where(SensorReading.tenant_id == TENANT)
    )
    assert sensor_count.scalar() == ROWS
    raw_count = await session.execute(
        select(func.count(RawReading.id)).where(RawReading.tenant_id == TENANT)
    )
    assert raw_count.scalar() == ROWS

    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)
    assert job.status == "completed"
    assert job.result["sequences_processed"] == ROWS - 20 + 1
    assert job.result["data_persisted"] is True
    assert job.result["rows_uploaded"] == ROWS


async def test_duplicate_portion_delivery_is_ignored(session):
    job_id, messages = await _produce_messages(session, _csv_content(60), portion_rows=60)
    detector = _mock_detector()
    await _consume_all(session, messages, detector)
    calls_after_first = detector.predict_batch_enriched.call_count

    # Redeliver the same portion — no new inference, no double rows/progress.
    await _consume_all(session, messages, detector)
    assert detector.predict_batch_enriched.call_count == calls_after_first

    sensor_count = await session.execute(
        select(func.count(SensorReading.id)).where(SensorReading.tenant_id == TENANT)
    )
    assert sensor_count.scalar() == 60
    job = await get_job(session, job_id)
    assert job.completed_portions == 1


async def test_sync_vs_portioned_equivalence(session):
    """The portioned pipeline must produce the same threshold and anomaly set."""
    from src.api.services.cold_run_upload import run_cold_run_upload
    from src.vendors.csv_adapter import CSVAdapter

    csv_content = _csv_content()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write(csv_content)
        tmp_name = tmp.name
    try:
        adapter = CSVAdapter({"filepath": tmp_name})
        adapter.connect()
        sync_response, _ = await run_cold_run_upload(
            tmp_path=tmp_name,
            adapter=adapter,
            detector=_mock_detector(),
            tenant_id=TENANT,
            greenhouse_id="default",
            chunk_size=50,
        )
    finally:
        os.unlink(tmp_name)

    job_id, messages = await _produce_messages(
        session, csv_content, portion_rows=50, persist=False
    )
    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)

    assert job.result["sequences_processed"] == sync_response.sequences_processed
    assert job.result["threshold_used"] == pytest.approx(sync_response.threshold_used)
    assert job.result["anomaly_scores"] == pytest.approx(sync_response.anomaly_scores)
    assert [h["timestamp"] for h in job.result["high_anomalies"]] == [
        h.timestamp for h in sync_response.high_anomalies
    ]


async def test_statistical_anomalies_survive_portioning(session):
    """Injected spikes must show up in the portioned job result's stat summary."""
    from src.api.services.cold_run_upload import run_cold_run_upload
    from src.vendors.csv_adapter import CSVAdapter

    csv_content = _csv_content(fault_at=70)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write(csv_content)
        tmp_name = tmp.name
    try:
        adapter = CSVAdapter({"filepath": tmp_name})
        adapter.connect()
        sync_response, _ = await run_cold_run_upload(
            tmp_path=tmp_name,
            adapter=adapter,
            detector=_mock_detector(),
            tenant_id=TENANT,
            greenhouse_id="default",
            chunk_size=50,
        )
    finally:
        os.unlink(tmp_name)

    sync_stat = sync_response.statistical_summary
    assert sync_stat is not None and len(sync_stat.anomalies) > 0

    job_id, messages = await _produce_messages(
        session, csv_content, portion_rows=50, persist=False
    )
    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)

    portioned_hits = job.result["statistical_summary"]["anomalies"]
    sync_hits = [a.model_dump() for a in sync_stat.anomalies]
    assert sorted(
        (h["timestamp"], h["column"]) for h in portioned_hits
    ) == sorted((h["timestamp"], h["column"]) for h in sync_hits)


async def test_failed_portion_marks_job_partially_failed(session):
    job_id, messages = await _produce_messages(session, _csv_content(), portion_rows=50)
    detector = _mock_detector()

    finalize_msgs: list[UploadFinalizeMessage] = []

    async def capture(routing_key, payload, **kwargs):
        finalize_msgs.append(payload)

    with patch(
        "src.api.routes.inference.get_cached_detector", return_value=detector
    ), patch("src.workers.upload_consumer.publish_json", side_effect=capture):
        await handle_upload_portion(messages[0], session)
        await handle_upload_portion(messages[1], session)
        await record_portion_failure(
            session,
            job_id=job_id,
            portion_idx=messages[2].portion_idx,
            error="boom",
            attempt=3,
        )

    assert len(finalize_msgs) == 1
    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)
    assert job.status == "partially_failed"
    assert job.failed_portions == 1


async def test_async_finalize_persists_trigger_channel(session):
    """B4: the async finalize path must populate trigger_channel on anomalous rows.

    The sync path passes trigger_data_by_timestamp to apply_anomaly_scores; the
    async path had a bug where it did not — rows came back with NULL
    trigger_channel / trigger_detector / detected_severity even when a
    statistical anomaly was the triggering event.  This test ensures parity.
    """
    # Use a fault-at-70 CSV so the statistical detector fires on temperature.
    csv_content = _csv_content(fault_at=70)
    job_id, messages = await _produce_messages(session, csv_content, portion_rows=50)

    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    assert len(finalize_msgs) == 1

    await handle_upload_finalize(finalize_msgs[0], session)

    job = await get_job(session, job_id)
    assert job.status == "completed", f"Expected completed, got {job.status}"

    # At least one row must have a non-NULL trigger_channel — the statistical
    # anomaly on temperature must survive the async path without being dropped.
    result = await session.execute(
        select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.trigger_channel.isnot(None),
        )
    )
    rows_with_trigger = result.scalars().all()
    assert len(rows_with_trigger) > 0, (
        "No rows have trigger_channel set after async finalize — "
        "the B4 divergence (trigger_data not passed to apply_anomaly_scores) "
        "is still present."
    )
    # Every row with a trigger must also have trigger_detector set.
    for row in rows_with_trigger:
        assert row.trigger_detector is not None, (
            f"Row at {row.timestamp} has trigger_channel={row.trigger_channel!r} "
            "but trigger_detector is NULL"
        )


async def test_finalize_retries_after_a_failed_write(session):
    """A finalize that raises mid-write must stay retryable, not wedge the job.

    _finalize claims the job by moving it to 'finalizing' before it writes.
    When the write raised — in production, apply_anomaly_scores hitting a
    column migration 024 had never created — the redelivered message could no
    longer match 'running'/'pending', logged "already claimed", and acked.  The
    DLQ hook never fired, so the job sat in 'finalizing' forever showing no
    error while its anomaly scores were silently never written.
    """
    job_id, messages = await _produce_messages(
        session, _csv_content(fault_at=70), portion_rows=50
    )
    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    assert len(finalize_msgs) == 1

    write_failure = RuntimeError('column "trigger_channel" does not exist')
    with patch(
        "src.workers.upload_consumer.apply_anomaly_scores", side_effect=write_failure
    ):
        with pytest.raises(RuntimeError):
            await handle_upload_finalize(finalize_msgs[0], session)

    job = await get_job(session, job_id)
    assert job.status == "finalizing", "the claim happened before the failing write"

    # Redelivery must re-claim and finish rather than short-circuit.
    await handle_upload_finalize(finalize_msgs[0], session)

    job = await get_job(session, job_id)
    assert job.status == "completed"
    assert job.result["anomalies_detected"] > 0
    scored = await session.execute(
        select(func.count(SensorReading.id)).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.anomaly_score.isnot(None),
        )
    )
    assert scored.scalar() > 0, "the retry must write the scores the first attempt lost"


async def test_finalize_does_not_overwrite_a_pruned_result(session):
    """A duplicate delivery racing a finished finalize must not blank the result.

    prune_portion_results nulls the per-portion blobs, so recomputing after a
    successful finalize would rebuild an empty response and overwrite a good
    result with zeros.
    """
    job_id, messages = await _produce_messages(session, _csv_content(), portion_rows=50)
    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    await handle_upload_finalize(finalize_msgs[0], session)

    job = await get_job(session, job_id)
    completed_result = job.result
    assert job.status == "completed"

    # Force the status back to simulate a duplicate delivery arriving while the
    # winning attempt was still between its claim and its terminal write.
    await try_transition(session, job_id, "completed", "finalizing")
    await handle_upload_finalize(finalize_msgs[0], session)

    job = await get_job(session, job_id)
    assert job.result == completed_result, "a redundant finalize must not rewrite the result"
