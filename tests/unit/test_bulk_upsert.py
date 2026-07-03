"""Tests for dialect-aware bulk upsert and chunk-equivalent upload scoring."""

import asyncio
import datetime
import os
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base, SensorReading
from src.db.upsert import bulk_upsert_rows, dedupe_rows_by_key

BASE_TS = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _sensor_row(minute: int, temperature: float = 20.0, anomaly_score=None) -> dict:
    return {
        "tenant_id": "tenant_upsert",
        "greenhouse_id": "default",
        "timestamp": BASE_TS + datetime.timedelta(minutes=minute),
        "temperature": temperature,
        "humidity": 50.0,
        "co2": 400.0,
        "light": 1000.0,
        "anomaly_score": anomaly_score,
    }


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


async def _count(session, tenant_id="tenant_upsert"):
    result = await session.execute(
        select(func.count(SensorReading.id)).where(SensorReading.tenant_id == tenant_id)
    )
    return result.scalar()


class TestBulkUpsert:
    async def test_insert_then_conflict_updates_in_place(self, session):
        await bulk_upsert_rows(
            session,
            SensorReading,
            [_sensor_row(0), _sensor_row(1)],
            update_cols=("temperature", "humidity", "co2", "light", "anomaly_score"),
            preserve_when_null=("anomaly_score",),
        )
        await session.commit()
        assert await _count(session) == 2

        await bulk_upsert_rows(
            session,
            SensorReading,
            [_sensor_row(0, temperature=99.0)],
            update_cols=("temperature", "humidity", "co2", "light", "anomaly_score"),
            preserve_when_null=("anomaly_score",),
        )
        await session.commit()
        assert await _count(session) == 2
        result = await session.execute(
            select(SensorReading.temperature).where(SensorReading.timestamp == BASE_TS)
        )
        assert result.scalar() == pytest.approx(99.0)

    async def test_null_score_preserves_existing_value(self, session):
        await bulk_upsert_rows(
            session,
            SensorReading,
            [_sensor_row(0, anomaly_score=0.9)],
            update_cols=("temperature", "anomaly_score"),
            preserve_when_null=("anomaly_score",),
        )
        await session.commit()
        # Re-upload without a score must not wipe the stored one.
        await bulk_upsert_rows(
            session,
            SensorReading,
            [_sensor_row(0, anomaly_score=None)],
            update_cols=("temperature", "anomaly_score"),
            preserve_when_null=("anomaly_score",),
        )
        await session.commit()
        result = await session.execute(
            select(SensorReading.anomaly_score).where(SensorReading.timestamp == BASE_TS)
        )
        assert result.scalar() == pytest.approx(0.9)

    def test_dedupe_rows_by_key_keeps_last(self):
        rows = [_sensor_row(0, temperature=1.0), _sensor_row(0, temperature=2.0)]
        deduped = dedupe_rows_by_key(rows)
        assert len(deduped) == 1
        assert deduped[0]["temperature"] == pytest.approx(2.0)


class TestChunkEquivalence:
    """Scoring must be identical whether the file fits in one chunk or many."""

    @staticmethod
    def _score_csv(csv_content: str, chunk_size: int):
        from src.api.services.cold_run_upload import run_cold_run_upload
        from src.vendors.csv_adapter import CSVAdapter

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
            tmp.write(csv_content)
            tmp_name = tmp.name
        try:
            adapter = CSVAdapter({"filepath": tmp_name})
            adapter.connect()
            detector = MagicMock()
            # Deterministic score derived from sequence content so boundary
            # bugs (lost/duplicated sequences) change the output.
            detector.predict_batch_enriched.side_effect = lambda seqs: (
                np.asarray([float(np.mean(s)) for s in seqs], dtype=np.float32),
                np.zeros((len(seqs), 8), dtype=np.float32),
                np.zeros(len(seqs), dtype=np.float32),
            )
            response, seq_ts = asyncio.run(
                run_cold_run_upload(
                    tmp_path=tmp_name,
                    adapter=adapter,
                    detector=detector,
                    tenant_id="tenant_eq",
                    greenhouse_id="default",
                    seq_len=20,
                    chunk_size=chunk_size,
                )
            )
            return response, seq_ts
        finally:
            os.unlink(tmp_name)

    def test_single_vs_multi_chunk_identical(self):
        rows = 130
        csv_content = "timestamp,temperature,humidity,co2,light\n" + "\n".join(
            f"2026-06-09T{11 + i // 60:02d}:{i % 60:02d}:00Z,"
            f"{20 + (i % 7) * 0.5},{50 + (i % 5)},{400 + i % 11},{1000 + i % 13}"
            for i in range(rows)
        )
        single, single_ts = self._score_csv(csv_content, chunk_size=1000)
        multi, multi_ts = self._score_csv(csv_content, chunk_size=37)

        # rows - seq_len + 1 sliding sequences, regardless of chunking
        assert single.sequences_processed == rows - 20 + 1
        assert multi.sequences_processed == single.sequences_processed
        assert multi_ts == single_ts
        assert multi.anomaly_scores == pytest.approx(single.anomaly_scores)
        assert multi.threshold_used == pytest.approx(single.threshold_used)
        assert [h.timestamp for h in multi.high_anomalies] == [
            h.timestamp for h in single.high_anomalies
        ]
