"""Tests for the worker message loop: bindings, dispatch, retry/DLQ routing."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import MQ_MAX_RETRIES, MQ_PREFETCH_COUNT
from src.mq.schemas import UploadFinalizeMessage
from src.mq.topology import (
    ATTEMPT_HEADER,
    TRAINING_ONLINE_QUEUE,
    TRAINING_RETRAIN_QUEUE,
    UPLOAD_FINALIZE_QUEUE,
    UPLOAD_PORTIONS_QUEUE,
    dlq_queue,
    retry_queue,
)
from src.workers.main import (
    QueueBinding,
    _handle_message,
    _terminal_job_failure,
    _terminal_upload_portion,
    build_bindings,
)


def _incoming(payload: str, attempt: int | None = None) -> MagicMock:
    message = MagicMock()
    message.body = payload.encode()
    message.content_encoding = None
    message.headers = {ATTEMPT_HEADER: attempt} if attempt is not None else {}
    message.ack = AsyncMock()
    return message


def _binding(handler, on_terminal_failure=None) -> QueueBinding:
    return QueueBinding(
        queue_name=UPLOAD_FINALIZE_QUEUE,
        schema=UploadFinalizeMessage,
        handler=handler,
        on_terminal_failure=on_terminal_failure,
    )


class TestBuildBindings:
    def test_all_four_queues_bound(self):
        bindings = build_bindings()
        names = [b.queue_name for b in bindings]
        assert names == [
            UPLOAD_PORTIONS_QUEUE,
            UPLOAD_FINALIZE_QUEUE,
            TRAINING_ONLINE_QUEUE,
            TRAINING_RETRAIN_QUEUE,
        ]

    def test_retrain_uses_prefetch_one(self):
        by_name = {b.queue_name: b for b in build_bindings()}
        assert by_name[TRAINING_RETRAIN_QUEUE].prefetch == 1
        assert by_name[UPLOAD_PORTIONS_QUEUE].prefetch == MQ_PREFETCH_COUNT


class TestHandleMessage:
    async def test_success_path_acks(self):
        handler = AsyncMock()
        message = _incoming(json.dumps({"job_id": "j1"}))
        with patch("src.workers.main.AsyncSessionLocal") as session_factory, patch(
            "src.workers.main.publish_json", new=AsyncMock()
        ) as publish:
            session_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await _handle_message(_binding(handler), message)
        handler.assert_awaited_once()
        message.ack.assert_awaited_once()
        publish.assert_not_awaited()

    async def test_failure_republishes_to_retry_with_incremented_attempt(self):
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        message = _incoming(json.dumps({"job_id": "j1"}), attempt=0)
        with patch("src.workers.main.AsyncSessionLocal") as session_factory, patch(
            "src.workers.main.publish_json", new=AsyncMock()
        ) as publish:
            session_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await _handle_message(_binding(handler), message)
        publish.assert_awaited_once()
        routing_key = publish.await_args.args[0]
        assert routing_key == retry_queue(UPLOAD_FINALIZE_QUEUE)
        assert publish.await_args.kwargs["attempt"] == 1
        message.ack.assert_awaited_once()  # original message always acked

    async def test_exhausted_retries_route_to_dlq_and_fire_terminal_hook(self):
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        terminal = AsyncMock()
        message = _incoming(json.dumps({"job_id": "j1"}), attempt=MQ_MAX_RETRIES - 1)
        with patch("src.workers.main.AsyncSessionLocal") as session_factory, patch(
            "src.workers.main.publish_json", new=AsyncMock()
        ) as publish:
            session_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await _handle_message(_binding(handler, terminal), message)
        assert publish.await_args.args[0] == dlq_queue(UPLOAD_FINALIZE_QUEUE)
        terminal.assert_awaited_once()
        # (db, parsed, error, attempt)
        assert terminal.await_args.args[2] == "boom"
        assert terminal.await_args.args[3] == MQ_MAX_RETRIES
        message.ack.assert_awaited_once()

    async def test_terminal_hook_failure_still_acks(self):
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        terminal = AsyncMock(side_effect=RuntimeError("hook down"))
        message = _incoming(json.dumps({"job_id": "j1"}), attempt=MQ_MAX_RETRIES - 1)
        with patch("src.workers.main.AsyncSessionLocal") as session_factory, patch(
            "src.workers.main.publish_json", new=AsyncMock()
        ):
            session_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await _handle_message(_binding(handler, terminal), message)
        message.ack.assert_awaited_once()

    async def test_invalid_schema_raises_before_ack(self):
        handler = AsyncMock()
        message = _incoming(json.dumps({"not_the_schema": True}))
        with pytest.raises(Exception):
            await _handle_message(_binding(handler), message)
        message.ack.assert_not_awaited()


class TestTerminalHooks:
    async def test_upload_portion_hook_records_failure(self):
        msg = MagicMock(job_id="j1", portion_idx=4)
        with patch(
            "src.workers.upload_consumer.record_portion_failure", new=AsyncMock()
        ) as record:
            await _terminal_upload_portion("db", msg, "err", 3)
        record.assert_awaited_once_with(
            "db", job_id="j1", portion_idx=4, error="err", attempt=3
        )

    async def test_job_failure_hook_sets_failed_status(self):
        msg = MagicMock(job_id="j9")
        with patch("src.db.jobs.set_job_result", new=AsyncMock()) as set_result:
            await _terminal_job_failure("db", msg, "err", 3)
        set_result.assert_awaited_once_with("db", "j9", status="failed", error="err")
