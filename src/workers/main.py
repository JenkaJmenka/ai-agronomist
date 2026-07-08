"""Long-lived queue worker: consumes upload/training job messages.

Per-message flow: decode (gzip-aware) → parse schema → handler with a fresh DB
session → ack. On handler error the message is republished to the queue's TTL
retry queue with an incremented attempt header; after MQ_MAX_RETRIES it goes
to the DLQ and the terminal-failure hook records the failure in the jobs
store. SIGTERM/SIGINT stop consumption and drain in-flight work.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aio_pika
from pydantic import BaseModel

import time

from src.config import (
    MQ_HANDLER_TIMEOUT_SECONDS,
    MQ_PREFETCH_COUNT,
    MQ_PREPARE_HANDLER_TIMEOUT_SECONDS,
    MQ_RETRAIN_HANDLER_TIMEOUT_SECONDS,
    MQ_WATCHDOG_INTERVAL_SECONDS,
)
from src.db.session import AsyncSessionLocal
from src.mq.connection import close_mq, get_mq_connection
from src.mq.publisher import decode_message_body, publish_json, publish_raw
from src.mq.topology import (
    ATTEMPT_HEADER,
    TRAINING_ONLINE_QUEUE,
    TRAINING_RETRAIN_QUEUE,
    UPLOAD_FINALIZE_QUEUE,
    UPLOAD_PORTIONS_QUEUE,
    UPLOAD_PREPARE_QUEUE,
    declare_topology,
    dlq_queue,
    retry_queue,
)
from src.mq.schemas import (
    OnlineTrainingPortionMessage,
    RetrainJobMessage,
    UploadFinalizeMessage,
    UploadPortionMessage,
    UploadPrepareMessage,
)

logger = logging.getLogger("worker.main")

Handler = Callable[..., Awaitable[None]]
TerminalHook = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class QueueBinding:
    queue_name: str
    schema: type[BaseModel]
    handler: Handler
    # Called once retries are exhausted; receives (db, message_model, error, attempt).
    on_terminal_failure: Optional[TerminalHook] = None
    prefetch: int = MQ_PREFETCH_COUNT
    # Hard deadline per message: a hung handler becomes a retry, not a jam.
    handler_timeout: float = MQ_HANDLER_TIMEOUT_SECONDS


async def _terminal_upload_portion(db, msg: UploadPortionMessage, error: str, attempt: int) -> None:
    from src.workers.upload_consumer import record_portion_failure

    await record_portion_failure(
        db, job_id=msg.job_id, portion_idx=msg.portion_idx, error=error, attempt=attempt
    )


async def _terminal_job_failure(db, msg, error: str, attempt: int) -> None:
    from src.db.jobs import set_job_result

    await set_job_result(db, msg.job_id, status="failed", error=error)


def build_bindings() -> list[QueueBinding]:
    from src.workers.upload_consumer import handle_upload_finalize, handle_upload_portion

    bindings = [
        QueueBinding(
            queue_name=UPLOAD_PORTIONS_QUEUE,
            schema=UploadPortionMessage,
            handler=handle_upload_portion,
            on_terminal_failure=_terminal_upload_portion,
        ),
        QueueBinding(
            queue_name=UPLOAD_FINALIZE_QUEUE,
            schema=UploadFinalizeMessage,
            handler=handle_upload_finalize,
            on_terminal_failure=_terminal_job_failure,
        ),
    ]
    try:
        from src.workers.training_consumer import handle_online_portion

        bindings.append(
            QueueBinding(
                queue_name=TRAINING_ONLINE_QUEUE,
                schema=OnlineTrainingPortionMessage,
                handler=handle_online_portion,
                on_terminal_failure=_terminal_job_failure,
            )
        )
    except ImportError:
        logger.warning("training_consumer not available — %s not consumed", TRAINING_ONLINE_QUEUE)
    try:
        from src.workers.retrain_consumer import handle_retrain_job

        bindings.append(
            QueueBinding(
                queue_name=TRAINING_RETRAIN_QUEUE,
                schema=RetrainJobMessage,
                handler=handle_retrain_job,
                on_terminal_failure=_terminal_job_failure,
                # Retraining is long-running; never hold extra messages hostage.
                prefetch=1,
                handler_timeout=MQ_RETRAIN_HANDLER_TIMEOUT_SECONDS,
            )
        )
    except ImportError:
        logger.warning("retrain_consumer not available — %s not consumed", TRAINING_RETRAIN_QUEUE)
    try:
        from src.workers.upload_prepare_consumer import (
            _terminal_upload_prepare,
            handle_upload_prepare,
        )

        bindings.append(
            QueueBinding(
                queue_name=UPLOAD_PREPARE_QUEUE,
                schema=UploadPrepareMessage,
                handler=handle_upload_prepare,
                # Set the job failed + delete the blob once retries are exhausted.
                on_terminal_failure=_terminal_upload_prepare,
                # Cold-start train + portion production is long and CPU-heavy;
                # like retrain, never hold other prepare messages hostage.
                prefetch=1,
                handler_timeout=MQ_PREPARE_HANDLER_TIMEOUT_SECONDS,
            )
        )
    except ImportError:
        logger.warning(
            "upload_prepare_consumer not available — %s not consumed", UPLOAD_PREPARE_QUEUE
        )
    return bindings


async def _handle_message(binding: QueueBinding, message: aio_pika.abc.AbstractIncomingMessage) -> None:
    """Process one message. Every exit path acks or rejects.

    A message left neither acked nor rejected stays 'unacked' forever and —
    with prefetch=N — N such messages silently starve the whole consumer
    channel. Observed in production: a broker restart mid-job left two
    poison messages that froze the worker at 2/22 portions across restarts.
    """
    attempt = int((message.headers or {}).get(ATTEMPT_HEADER, 0))

    try:
        body = decode_message_body(message.body, message.content_encoding)
        parsed = binding.schema.model_validate_json(body)
    except Exception:
        logger.exception(
            "Unparseable message on %s — moving raw body to DLQ", binding.queue_name
        )
        try:
            await publish_raw(
                dlq_queue(binding.queue_name),
                message.body,
                content_encoding=message.content_encoding,
                headers=dict(message.headers or {}),
            )
        except Exception:
            logger.exception("Could not preserve unparseable message in DLQ — dropping")
        await message.ack()
        return

    portion = getattr(parsed, "portion_idx", None)
    label = f"{binding.queue_name}" + (f"[portion {portion}]" if portion is not None else "")
    started = time.monotonic()
    logger.info("Handling %s (attempt %s)", label, attempt)
    try:
        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(
                binding.handler(parsed, db), timeout=binding.handler_timeout
            )
        await message.ack()
        logger.info("Finished %s in %.1fs", label, time.monotonic() - started)
        return
    except asyncio.TimeoutError:
        logger.error(
            "Handler TIMED OUT on %s after %.0fs (attempt %s) — routing to retry/DLQ",
            label,
            binding.handler_timeout,
            attempt,
        )
        error_text = f"handler timeout after {binding.handler_timeout:.0f}s"
    except Exception as exc:
        logger.exception("Handler failed on %s (attempt %s)", label, attempt)
        error_text = str(exc)

    # Retry/DLQ routing — must never leave the message unacked: if the
    # republish itself fails (broker hiccup), give the message back to the
    # queue instead of starving the prefetch window.
    from src.config import MQ_MAX_RETRIES

    try:
        if attempt + 1 < MQ_MAX_RETRIES:
            await publish_json(retry_queue(binding.queue_name), parsed, attempt=attempt + 1)
        else:
            await publish_json(dlq_queue(binding.queue_name), parsed, attempt=attempt + 1)
            if binding.on_terminal_failure is not None:
                try:
                    async with AsyncSessionLocal() as db:
                        await binding.on_terminal_failure(db, parsed, error_text, attempt + 1)
                except Exception:
                    logger.exception("Terminal-failure hook failed for %s", binding.queue_name)
    except Exception:
        logger.exception(
            "Retry/DLQ publish failed on %s — requeueing original message",
            binding.queue_name,
        )
        await message.reject(requeue=True)
        return
    await message.ack()


def _transport_dead(
    connection: aio_pika.abc.AbstractConnection,
    channels: list[aio_pika.abc.AbstractChannel],
) -> bool:
    """True when the MQ connection or any consumer channel is no longer live."""
    return connection.is_closed or any(ch.is_closed for ch in channels)


async def run_worker() -> None:
    logging.basicConfig(level=logging.INFO)

    # Pin the CPU torch thread pool before any inference/training runs — the
    # worker is where cold-start training and portion scoring happen.
    from src.models.torch_runtime import configure_torch_threads

    configure_torch_threads()

    connection = await get_mq_connection()

    stop_event = asyncio.Event()
    unhealthy = False
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    channels: list[aio_pika.abc.AbstractChannel] = []
    for binding in build_bindings():
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=binding.prefetch)
        await declare_topology(channel)
        queue = await channel.get_queue(binding.queue_name)

        async def consume(message, binding=binding):
            await _handle_message(binding, message)

        await queue.consume(consume)
        channels.append(channel)
        logger.info("Consuming %s (prefetch=%s)", binding.queue_name, binding.prefetch)

    async def _watchdog() -> None:
        # aio-pika channel restoration has failed in production after a broker
        # restart ("No active transport in channel"), leaving a process that
        # looks alive but consumes nothing. Exiting non-zero lets the platform
        # restart the container into a clean connection.
        nonlocal unhealthy
        while not stop_event.is_set():
            await asyncio.sleep(MQ_WATCHDOG_INTERVAL_SECONDS)
            if _transport_dead(connection, channels):
                logger.error("MQ connection/channel dead — exiting for a clean restart")
                unhealthy = True
                stop_event.set()
                return

    watchdog = asyncio.create_task(_watchdog())

    logger.info("Worker up — waiting for messages")
    await stop_event.wait()
    watchdog.cancel()

    logger.info("Shutdown signal received — draining")
    for channel in channels:
        try:
            if not channel.is_closed:
                await channel.close()
        except Exception:
            logger.exception("Channel close failed during drain")
    try:
        await close_mq()
    except Exception:
        logger.exception("Connection close failed during drain")
    if unhealthy:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run_worker())
