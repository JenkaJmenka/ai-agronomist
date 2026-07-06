"""Tests for cold-start training helpers and model registry lifecycle."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.cold_start_training import should_train_cold_start
from src.db.models import Base, ModelArtifact
from src.db.session import AsyncSessionLocal, engine
from src.models.artifact_store import put_checkpoint
from src.models.model_registry import ModelRegistry


@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def test_db() -> AsyncSession:
    async with AsyncSessionLocal() as db:
        yield db


def test_should_train_cold_start_option_a():
    assert should_train_cold_start(force_train=False, has_active_model=False) is True
    assert should_train_cold_start(force_train=False, has_active_model=True) is False
    assert should_train_cold_start(force_train=True, has_active_model=True) is True


@pytest.mark.asyncio
async def test_delete_model_removes_artifact(test_db: AsyncSession, tmp_path):
    registry = ModelRegistry(test_db)
    ckpt = tmp_path / "model.pt"
    ckpt.write_bytes(b"fake-checkpoint-bytes")
    artifact_path = await put_checkpoint(test_db, "test/delete_me", str(ckpt))
    model_id = await registry.register_model(
        "tenant_x", "cold_start", artifact_path, metrics={"mse": 0.1}
    )

    assert await registry.delete_model("tenant_x", model_id, force=True) is True
    assert await registry.get_active_model("tenant_x") is None

    result = await test_db.execute(
        select(ModelArtifact).where(ModelArtifact.key == "test/delete_me")
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_active_switches_to_previous(test_db: AsyncSession):
    registry = ModelRegistry(test_db)
    m1 = await registry.register_model("tenant_y", "cold_start", "path/v1.pt")
    m2 = await registry.register_model("tenant_y", "cold_start", "path/v2.pt")

    assert (await registry.get_active_model("tenant_y"))["model_id"] == m2
    assert await registry.delete_model("tenant_y", m2) is True
    active = await registry.get_active_model("tenant_y")
    assert active is not None
    assert active["model_id"] == m1


@pytest.mark.asyncio
async def test_clear_models_deletes_all_versions(test_db: AsyncSession, tmp_path):
    registry = ModelRegistry(test_db)
    ckpt = tmp_path / "model.pt"
    ckpt.write_bytes(b"bytes")
    artifact_path = await put_checkpoint(test_db, "test/clear_all", str(ckpt))
    await registry.register_model("tenant_z", "cold_start", artifact_path)
    await registry.register_model("tenant_z", "transfer_learned", "path/local.pt")

    count = await registry.clear_models("tenant_z")
    assert count == 2
    assert await registry.list_models("tenant_z") == []
