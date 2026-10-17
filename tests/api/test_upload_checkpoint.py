"""Tests for upload checkpoint resolution when cold-start training cannot run.

These two cases used to assert a "fall back to the baseline" contract:
``resolve_upload_checkpoint`` returned ``(None, False, None, "<reason>")`` and
the caller was expected to proceed. That contract was never honoured — the
``None`` flowed into ``get_cached_detector(None)``, which resolved
``models/baseline.pt``, a file excluded from the image by ``.dockerignore`` and
present in no environment, so the upload died with HTTP 503 "Failed to
initialize model". There is no global baseline to fall back to.

Now the function raises ``NoModelAvailableError`` (a ``ValueError``, so both
upload callers already map it to an actionable failure) and says what the
operator should do about it.
"""

from __future__ import annotations

import pytest

from src.api.services import upload_checkpoint
from src.api.services.upload_checkpoint import (
    ColdStartTrainingError,
    NoModelAvailableError,
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
async def test_insufficient_data_raises_instead_of_claiming_a_baseline(monkeypatch):
    monkeypatch.setattr(
        upload_checkpoint,
        "train_cold_start_from_csv",
        _failing_trainer(InsufficientDataError("not enough rows")),
    )
    with pytest.raises(NoModelAvailableError) as exc_info:
        await resolve_upload_checkpoint(
            None, "tenant_x", "/tmp/fake.csv", train_model=False
        )

    detail = str(exc_info.value)
    assert "tenant_x" in detail
    assert "not enough rows" in detail, "the underlying cause must survive"
    assert "baseline" not in detail.lower(), (
        "there is no baseline to mention — that claim was the bug"
    )
    # A ValueError subclass, so inference.py's existing handler returns 400 and
    # upload_prepare_consumer's marks the job failed, with no new handler.
    assert isinstance(exc_info.value, ValueError)


@pytest.mark.asyncio
async def test_unexpected_training_error_raises_instead_of_baseline(monkeypatch):
    monkeypatch.setattr(
        upload_checkpoint,
        "train_cold_start_from_csv",
        _failing_trainer(RuntimeError("torch exploded")),
    )
    with pytest.raises(NoModelAvailableError) as exc_info:
        await resolve_upload_checkpoint(
            None, "tenant_x", "/tmp/fake.csv", train_model=False
        )
    assert "tenant_x" in str(exc_info.value)
    assert isinstance(exc_info.value, ValueError)


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
