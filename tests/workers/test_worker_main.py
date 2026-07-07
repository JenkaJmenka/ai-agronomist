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
    UPLOAD_PREPARE_QUEUE,
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
    def test_all_queues_bound(self):
        bindings = build_bindings()
        names = [b.queue_name for b in bindings]
        assert names == [
            UPLOAD_PORTIONS_QUEUE,
            UPLOAD_FINALIZE_QUEUE,
            TRAINING_ONLINE_QUEUE,
            TRAINING_RETRAIN_QUEUE,
            UPLOAD_PREPARE_QUEUE,
        ]

    def test_retrain_uses_prefetch_one(self):
        by_name = {b.queue_name: b for b in build_bindings()}
        assert by_name[TRAINING_RETRAIN_QUEUE].prefetch == 1
        assert by_name[UPLOAD_PORTIONS_QUEUE].prefetch == MQ_PREFETCH_COUNT

    def test_prepare_binding_is_long_running_prefetch_one(self):
        from src.config import MQ_PREPARE_HANDLER_TIMEOUT_SECONDS

        by_name = {b.queue_name: b for b in build_bindings()}
        prepare = by_name[UPLOAD_PREPARE_QUEUE]
        assert prepare.prefetch == 1
        assert prepare.handler_timeout == MQ_PREPARE_HANDLER_TIMEOUT_SECONDS


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

    async def test_unparseable_message_moves_to_dlq_and_acks(self):
        """Poison messages must never stay unacked — with prefetch=N, N of
        them permanently starve the consumer channel."""
        handler = AsyncMock()
        message = _incoming(json.dumps({"not_the_schema": True}))
        with patch("src.workers.main.publish_raw", new=AsyncMock()) as publish_raw:
            await _handle_message(_binding(handler), message)
        handler.assert_not_awaited()
        publish_raw.assert_awaited_once()
        assert publish_raw.await_args.args[0] == dlq_queue(UPLOAD_FINALIZE_QUEUE)
        message.ack.assert_awaited_once()

    async def test_unparseable_message_acked_even_when_dlq_publish_fails(self):
        handler = AsyncMock()
        message = _incoming(b"\x00garbage".decode("latin1"))
        with patch(
            "src.workers.main.publish_raw", new=AsyncMock(side_effect=RuntimeError("down"))
        ):
            await _handle_message(_binding(handler), message)
        message.ack.assert_awaited_once()

    async def test_retry_publish_failure_requeues_instead_of_starving(self):
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        message = _incoming(json.dumps({"job_id": "j1"}), attempt=0)
        message.reject = AsyncMock()
        with patch("src.workers.main.AsyncSessionLocal") as session_factory, patch(
            "src.workers.main.publish_json",
            new=AsyncMock(side_effect=RuntimeError("broker down")),
        ):
            session_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await _handle_message(_binding(handler), message)
        message.reject.assert_awaited_once_with(requeue=True)
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


class TestHandlerTimeout:
    async def test_hung_handler_times_out_to_retry(self):
        """A wedged handler (e.g. a hung DB call) must become a retry, never a
        silent prefetch-window jam — observed in production as a worker that
        consumed nothing for 26 minutes."""
        import asyncio

        async def hang(parsed, db):
            await asyncio.sleep(3600)

        binding = QueueBinding(
            queue_name=UPLOAD_FINALIZE_QUEUE,
            schema=UploadFinalizeMessage,
            handler=hang,
            handler_timeout=0.05,
        )
        message = _incoming(json.dumps({"job_id": "j1"}), attempt=0)
        with patch("src.workers.main.AsyncSessionLocal") as session_factory, patch(
            "src.workers.main.publish_json", new=AsyncMock()
        ) as publish:
            session_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            await _handle_message(binding, message)
        assert publish.await_args.args[0] == retry_queue(UPLOAD_FINALIZE_QUEUE)
        message.ack.assert_awaited_once()

    def test_retrain_binding_gets_long_timeout(self):
        from src.config import MQ_RETRAIN_HANDLER_TIMEOUT_SECONDS

        by_name = {b.queue_name: b for b in build_bindings()}
        assert by_name[TRAINING_RETRAIN_QUEUE].handler_timeout == MQ_RETRAIN_HANDLER_TIMEOUT_SECONDS


class TestTransportWatchdog:
    def test_transport_dead_detection(self):
        from src.workers.main import _transport_dead

        conn = MagicMock(is_closed=False)
        live = MagicMock(is_closed=False)
        dead = MagicMock(is_closed=True)
        assert not _transport_dead(conn, [live, live])
        assert _transport_dead(conn, [live, dead])
        assert _transport_dead(MagicMock(is_closed=True), [live])
