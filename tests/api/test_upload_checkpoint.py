"""Tests for upload checkpoint resolution (cold-start training fallback)."""

from __future__ import annotations

import pytest

from src.api.services import upload_checkpoint
from src.api.services.upload_checkpoint import (
    ColdStartTrainingError,
    resolve_upload_checkpoint,
)
from src.models.bootstrap import InsufficientDataError
from src.models.model_registry import ModelRegistry


@pytest.fixture(autouse=True)
def no_active_model(monkeypatch):
    async def _none(self, tenant_id):
        return None

    monkeypatch.setattr(ModelRegistry, "get_active_model", _none)


def _failing_trainer(exc: Exception):
    async def _train(db, tenant_id, csv_path):
        raise exc

    return _train


@pytest.mark.asyncio
async def test_auto_training_falls_back_to_baseline_on_insufficient_data(monkeypatch):
    monkeypatch.setattr(
        upload_checkpoint,
        "train_cold_start_from_csv",
        _failing_trainer(InsufficientDataError("not enough rows")),
    )
    checkpoint, trained, model_id, reason = await resolve_upload_checkpoint(
        None, "tenant_x", "/tmp/fake.csv", train_model=False
    )
    assert checkpoint is None
    assert trained is False
    assert model_id is None
    assert reason == "insufficient_data"


@pytest.mark.asyncio
async def test_auto_training_falls_back_to_baseline_on_unexpected_error(monkeypatch):
    monkeypatch.setattr(
        upload_checkpoint,
        "train_cold_start_from_csv",
        _failing_trainer(RuntimeError("torch exploded")),
    )
    checkpoint, trained, model_id, reason = await resolve_upload_checkpoint(
        None, "tenant_x", "/tmp/fake.csv", train_model=False
    )
    assert checkpoint is None
    assert trained is False
    assert reason == "training_error"


@pytest.mark.asyncio
async def test_forced_training_propagates_insufficient_data(monkeypatch):
    monkeypatch.setattr(
        upload_checkpoint,
        "train_cold_start_from_csv",
        _failing_trainer(InsufficientDataError("not enough rows")),
    )
    with pytest.raises(InsufficientDataError):
        await resolve_upload_checkpoint(
            None, "tenant_x", "/tmp/fake.csv", train_model=True
        )


@pytest.mark.asyncio
async def test_forced_training_wraps_unexpected_error(monkeypatch):
    monkeypatch.setattr(
        upload_checkpoint,
        "train_cold_start_from_csv",
        _failing_trainer(RuntimeError("torch exploded")),
    )
    with pytest.raises(ColdStartTrainingError):
        await resolve_upload_checkpoint(
            None, "tenant_x", "/tmp/fake.csv", train_model=True
        )
