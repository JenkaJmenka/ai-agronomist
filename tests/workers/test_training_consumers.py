"""Tests for WS8: artifact store, keyset pagination, online + retrain consumers."""

import datetime
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.jobs import create_job, get_job
from src.db.models import Base, JobPortion, LearningLog, SensorReading
from src.models.artifact_store import put_checkpoint, resolve_checkpoint
from src.mq.schemas import OnlineTrainingPortionMessage, RetrainJobMessage

TENANT = "tenant_training"
BASE_TS = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


async def _seed_readings(session, count: int):
    for i in range(count):
        session.add(
            SensorReading(
                tenant_id=TENANT,
                greenhouse_id="default",
                timestamp=BASE_TS + datetime.timedelta(minutes=i),
                temperature=20.0 + (i % 10) * 0.5,
                humidity=50.0 + (i % 5),
                co2=400.0 + (i % 7),
                light=1000.0 + (i % 11),
            )
        )
    await session.commit()


class TestArtifactStore:
    async def test_put_resolve_round_trip(self, session, tmp_path):
        src = tmp_path / "ckpt.pt"
        src.write_bytes(b"fake-checkpoint-bytes")
        db_path = await put_checkpoint(session, "retrained/tenant_x", str(src))
        assert db_path == "db://model_artifacts/retrained/tenant_x"

        with patch(
            "src.models.artifact_store.LOCAL_CACHE_DIR", str(tmp_path / "cache")
        ):
            local = await resolve_checkpoint(session, db_path)
        assert local is not None and os.path.exists(local)
        assert open(local, "rb").read() == b"fake-checkpoint-bytes"

    async def test_plain_paths_pass_through(self, session):
        assert await resolve_checkpoint(session, "models/baseline.pt") == "models/baseline.pt"
        assert await resolve_checkpoint(session, None) is None

    async def test_missing_artifact_raises(self, session):
        with pytest.raises(FileNotFoundError):
            await resolve_checkpoint(session, "db://model_artifacts/nope")

    async def test_put_overwrites_same_key(self, session, tmp_path):
        src = tmp_path / "ckpt.pt"
        src.write_bytes(b"v1")
        await put_checkpoint(session, "retrained/tenant_x", str(src))
        src.write_bytes(b"v2-longer-content")
        await put_checkpoint(session, "retrained/tenant_x", str(src))
        with patch(
            "src.models.artifact_store.LOCAL_CACHE_DIR", str(tmp_path / "cache")
        ):
            local = await resolve_checkpoint(session, "db://model_artifacts/retrained/tenant_x")
        assert open(local, "rb").read() == b"v2-longer-content"


class TestIterCustomerData:
    async def test_keyset_pagination_covers_all_rows_in_order(self, session):
        from src.models.training_pipeline import iter_customer_data

        await _seed_readings(session, 25)
        portions = [p async for p in iter_customer_data(session, TENANT, portion_rows=10)]
        assert [len(p) for p in portions] == [10, 10, 5]
        stitched = np.concatenate(portions)
        assert len(stitched) == 25
        # Ordered by timestamp: temperature follows the seeded (i % 10) pattern.
        assert stitched[0][0] == pytest.approx(20.0)
        assert stitched[11][0] == pytest.approx(20.5)

    async def test_empty_tenant_yields_nothing(self, session):
        from src.models.training_pipeline import iter_customer_data

        portions = [p async for p in iter_customer_data(session, "ghost", portion_rows=10)]
        assert portions == []


def _online_message(job_id: str, idx: int, count: int, n_seqs: int = 3):
    rng = np.random.default_rng(idx)
    return OnlineTrainingPortionMessage(
        job_id=job_id,
        tenant_id=TENANT,
        greenhouse_id="default",
        portion_idx=idx,
        portion_count=count,
        sequences=rng.random((n_seqs, 20, 4)).tolist(),
    )


class TestOnlineConsumer:
    @pytest_asyncio.fixture
    async def job(self, session):
        return await create_job(
            session, tenant_id=TENANT, job_type="training_online", total_portions=2
        )

    @pytest.fixture(autouse=True)
    def patched_manager(self):
        import src.workers.training_consumer as tc

        manager = MagicMock()
        learner = MagicMock()
        learner.update_count = 0
        manager.learners = {}

        def observe(tenant_id, seq):
            manager.learners[tenant_id] = learner
            learner.update_count += 1
            return 0.25

        manager.observe.side_effect = observe
        with patch.object(tc, "_manager", manager), patch.object(
            tc, "_save_learner_state", AsyncMock(return_value="db://model_artifacts/online_learners/t")
        ):
            yield manager

    async def test_online_portions_complete_job(self, session, job):
        from src.workers.training_consumer import handle_online_portion

        await handle_online_portion(_online_message(job.id, 0, 2), session)
        await handle_online_portion(_online_message(job.id, 1, 2), session)

        fetched = await get_job(session, job.id)
        assert fetched.status == "completed"
        assert fetched.result["learner_state"].startswith("db://")

        logs = await session.execute(
            select(func.count(LearningLog.id)).where(LearningLog.tenant_id == TENANT)
        )
        assert logs.scalar() >= 2  # one observe_batch event per portion

    async def test_online_redelivery_is_ignored(self, session, job, patched_manager):
        from src.workers.training_consumer import handle_online_portion

        msg = _online_message(job.id, 0, 2)
        await handle_online_portion(msg, session)
        calls = patched_manager.observe.call_count
        await handle_online_portion(msg, session)
        assert patched_manager.observe.call_count == calls

        fetched = await get_job(session, job.id)
        assert fetched.completed_portions == 1


class TestRetrainConsumer:
    async def test_retrain_registers_when_improved(self, session, tmp_path):
        from src.workers import retrain_consumer

        await _seed_readings(session, 50)
        job = await create_job(session, tenant_id=TENANT, job_type="training_retrain")

        ckpt = tmp_path / "new.pt"
        ckpt.write_bytes(b"trained")

        learner = MagicMock()
        learner.checkpoint_path = "models/baseline.pt"
        learner.fine_tune_portions = AsyncMock(return_value=str(ckpt))

        evaluator = MagicMock()
        evaluator.evaluate = AsyncMock(return_value={"loss": 1.0})
        evaluator.compare.return_value = {"improvement_percent": 25.0}

        registry = MagicMock()
        registry.get_active_model = AsyncMock(return_value=None)
        registry.register_model = AsyncMock(return_value="model-123")

        with patch.object(retrain_consumer, "TransferLearner", return_value=learner), patch.object(
            retrain_consumer, "ModelEvaluator", return_value=evaluator
        ), patch.object(retrain_consumer, "ModelRegistry", return_value=registry), patch.object(
            retrain_consumer.LSTMVAE, "load_checkpoint", lambda self, p: None
        ):
            await retrain_consumer.handle_retrain_job(
                RetrainJobMessage(job_id=job.id, tenant_id=TENANT), session
            )

        fetched = await get_job(session, job.id)
        assert fetched.status == "completed"
        assert fetched.result["registered"] is True
        assert fetched.result["model_id"] == "model-123"
        registry.register_model.assert_awaited_once()
        # Checkpoint went through the artifact store as a db:// path.
        assert registry.register_model.call_args.kwargs["checkpoint_path"].startswith("db://")

    async def test_retrain_redelivery_skipped_when_claimed(self, session):
        from src.workers import retrain_consumer

        job = await create_job(session, tenant_id=TENANT, job_type="training_retrain")
        # Simulate an in-flight run holding the claim.
        from src.db.jobs import try_transition

        await try_transition(session, job.id, "pending", "running")

        with patch.object(retrain_consumer, "TransferLearner") as learner_cls:
            await retrain_consumer.handle_retrain_job(
                RetrainJobMessage(job_id=job.id, tenant_id=TENANT), session
            )
            learner_cls.assert_not_called()
