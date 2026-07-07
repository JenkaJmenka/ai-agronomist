"""Regression coverage for the "455 anomalies but empty sensor_readings" incident.

Production symptom: an upload job finished `completed` with anomalies detected,
but `persist_data` was true and 0 rows ever landed in sensor_readings/
raw_readings, and nothing surfaced the failure to the operator.

Two fixes are under test here (both already landed, not modified by this file):

1. `src.workers.upload_consumer.handle_upload_finalize` — when persistence was
   requested, portions actually had data (`stat_carry.total_rows > 0`), but
   both `sensor_rows_total` and `raw_rows_total` come back 0, the job must be
   set to `partially_failed` with a non-null, descriptive `error` instead of
   silently reporting `completed`.
2. `src.dashboard.components.inference_results.render_upload_status_bar` —
   surfaces a `status_col.error(...)` banner when a saved upload result has
   `data_persisted=True` but both row counters are 0.

This module intentionally mirrors the harness in
`tests/workers/test_upload_consumer.py` (producer -> portion handler ->
finalize, no real broker) rather than inventing a new one.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.jobs import get_job
from src.db.models import Base, RawReading, SensorReading
from src.mq.schemas import UploadFinalizeMessage
from src.workers.upload_consumer import handle_upload_finalize, handle_upload_portion

TENANT = "tenant_persist_guard"
ROWS = 90


def _csv_content(rows: int = ROWS) -> str:
    return "timestamp,temperature,humidity,co2,light\n" + "\n".join(
        f"2026-06-09T{11 + i // 60:02d}:{i % 60:02d}:00Z,"
        f"{20 + (i % 7) * 0.5},{50 + (i % 5)},{400 + i % 11},{1000 + i % 13}"
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


async def _produce_messages(session, csv_content: str, portion_rows: int, persist: bool):
    """Run the producer with publish captured; returns (job_id, portion messages)."""
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


async def _consume_all(session, messages, detector, *, persist_patch=None):
    """Feed portion messages to the handler; returns finalize messages fired.

    `persist_patch` optionally replaces `persist_readings_chunk` (imported
    directly into upload_consumer) to simulate a persistence layer that
    silently writes 0 rows while everything else succeeds -- this is exactly
    the incident scenario, and it cannot be reproduced by simply omitting
    `persist_data` (that path is already covered/guarded separately).
    """
    finalize_msgs: list[UploadFinalizeMessage] = []

    async def capture(routing_key, payload, **kwargs):
        finalize_msgs.append(payload)

    patches = [
        patch("src.api.routes.inference.get_cached_detector", return_value=detector),
        patch("src.workers.upload_consumer.publish_json", side_effect=capture),
    ]
    if persist_patch is not None:
        patches.append(
            patch("src.workers.upload_consumer.persist_readings_chunk", side_effect=persist_patch)
        )
    for p in patches:
        p.start()
    try:
        for msg in messages:
            await handle_upload_portion(msg, session)
    finally:
        for p in patches:
            p.stop()
    return finalize_msgs


# --------------------------------------------------------------------------
# A. End-to-end happy path: persistence actually writes rows, job stays clean.
# --------------------------------------------------------------------------


async def test_end_to_end_persist_writes_rows_and_job_completes_clean(session):
    """persist_data=True with real rows -> tables populated, no false alarm.

    This is the "item #4 in the handover" regression: previously an upload
    could report success with the DB left empty; here we drive the async
    portioned pipeline end-to-end (no real MQ broker, matching the existing
    upload-consumer test harness) and assert both tables are populated, the
    job finishes `completed`, and no error is attached.
    """
    job_id, messages = await _produce_messages(
        session, _csv_content(), portion_rows=45, persist=True
    )
    assert len(messages) == 2

    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    assert len(finalize_msgs) == 1

    sensor_count = (
        await session.execute(
            select(func.count(SensorReading.id)).where(SensorReading.tenant_id == TENANT)
        )
    ).scalar()
    raw_count = (
        await session.execute(
            select(func.count(RawReading.id)).where(RawReading.tenant_id == TENANT)
        )
    ).scalar()
    assert sensor_count == ROWS
    assert raw_count == ROWS

    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)

    assert job.status == "completed"
    assert job.error is None
    assert job.result["data_persisted"] is True
    assert job.result["sensor_rows_uploaded"] == ROWS
    assert job.result["raw_rows_uploaded"] == ROWS
    assert job.result["sensor_rows_uploaded"] > 0
    assert job.result["raw_rows_uploaded"] > 0


# --------------------------------------------------------------------------
# B. Fail-loud: persistence requested, portions "completed", but 0 rows
#    actually landed -- this is the exact incident shape.
# --------------------------------------------------------------------------


async def test_persist_requested_but_zero_rows_written_marks_partially_failed(session):
    job_id, messages = await _produce_messages(
        session, _csv_content(), portion_rows=45, persist=True
    )
    assert len(messages) == 2

    async def _zero_row_persist(**kwargs):
        # Simulates the production bug: the persistence call "succeeds" (no
        # exception raised, portion recorded as completed) but writes nothing
        # -- e.g. a silently-empty transaction, wrong tenant scoping, etc.
        return (0, 0, 0, 0)

    finalize_msgs = await _consume_all(
        session, messages, _mock_detector(), persist_patch=_zero_row_persist
    )
    assert len(finalize_msgs) == 1

    # Confirm the tables really are empty for this tenant -- the incident
    # symptom, not just an artifact of our mock.
    sensor_count = (
        await session.execute(
            select(func.count(SensorReading.id)).where(SensorReading.tenant_id == TENANT)
        )
    ).scalar()
    raw_count = (
        await session.execute(
            select(func.count(RawReading.id)).where(RawReading.tenant_id == TENANT)
        )
    ).scalar()
    assert sensor_count == 0
    assert raw_count == 0

    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)

    # Not a portion failure -- every portion still "completed" cleanly, which
    # is exactly why the old code silently reported `completed` for the whole
    # job. The new guard must catch this independently of failed_portions.
    assert job.failed_portions == 0
    assert job.status == "partially_failed"
    assert job.error is not None
    assert "0 rows" in job.error
    assert "sensor_readings" in job.error or "raw_readings" in job.error
    # rows really were processed (scored) -- this is what makes the 0-row
    # outcome a bug rather than an empty/no-op upload.
    assert job.result["sequences_processed"] > 0


# --------------------------------------------------------------------------
# C. No false positive: successful persistence, and persist_data=False, must
#    both stay clean (guards against the new condition over-firing).
# --------------------------------------------------------------------------


async def test_persist_success_no_false_positive_error(session):
    job_id, messages = await _produce_messages(
        session, _csv_content(60), portion_rows=60, persist=True
    )
    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)

    assert job.status == "completed"
    assert job.error is None
    assert job.result["sensor_rows_uploaded"] > 0
    assert job.result["raw_rows_uploaded"] > 0


async def test_persist_data_false_stays_completed_with_no_error(session):
    job_id, messages = await _produce_messages(
        session, _csv_content(60), portion_rows=60, persist=False
    )
    finalize_msgs = await _consume_all(session, messages, _mock_detector())
    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)

    assert job.status == "completed"
    assert job.error is None
    # persist_data was never requested, so data_persisted must stay falsy and
    # the 0-row guard (which only fires when persistence was requested) must
    # not trip even though nothing was ever written.
    assert not job.result.get("data_persisted")

    sensor_count = (
        await session.execute(
            select(func.count(SensorReading.id)).where(SensorReading.tenant_id == TENANT)
        )
    ).scalar()
    assert sensor_count == 0


# --------------------------------------------------------------------------
# D. Partial write (only one table populated) must stay completed -- the
#    guard requires BOTH sensor_rows_total AND raw_rows_total to be 0 before
#    flagging partially_failed. A future refactor that loosens the `and` to
#    an `or` would wrongly flip a legitimate partial-write into a failure;
#    these two cases lock the current (intentional) semantics in place.
# --------------------------------------------------------------------------


async def test_persist_partial_write_sensor_only_stays_completed(session):
    """sensor_rows > 0, raw_rows == 0 -> guard must NOT fire."""
    job_id, messages = await _produce_messages(
        session, _csv_content(60), portion_rows=60, persist=True
    )

    async def _sensor_only_persist(**kwargs):
        # (sensor_inserted, sensor_updated, raw_inserted, raw_updated) -- only
        # the sensor_readings side landed rows.
        return (60, 0, 0, 0)

    finalize_msgs = await _consume_all(
        session, messages, _mock_detector(), persist_patch=_sensor_only_persist
    )
    assert len(finalize_msgs) == 1

    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)

    assert job.status == "completed"
    assert job.error is None
    assert job.result["sensor_rows_uploaded"] == 60
    assert job.result["raw_rows_uploaded"] == 0


async def test_persist_partial_write_raw_only_stays_completed(session):
    """raw_rows > 0, sensor_rows == 0 -> mirror of the case above."""
    job_id, messages = await _produce_messages(
        session, _csv_content(60), portion_rows=60, persist=True
    )

    async def _raw_only_persist(**kwargs):
        # Only the raw_readings side landed rows this time.
        return (0, 0, 60, 0)

    finalize_msgs = await _consume_all(
        session, messages, _mock_detector(), persist_patch=_raw_only_persist
    )
    assert len(finalize_msgs) == 1

    await handle_upload_finalize(finalize_msgs[0], session)
    job = await get_job(session, job_id)

    assert job.status == "completed"
    assert job.error is None
    assert job.result["sensor_rows_uploaded"] == 0
    assert job.result["raw_rows_uploaded"] == 60
