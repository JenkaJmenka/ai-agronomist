"""Queue topology: main queues with TTL-backed retry queues and DLQs.

Retry pattern (no broker plugins needed): a consumer that wants a retry
publishes the message to ``<queue>.retry``; the retry queue has a message TTL
and dead-letters back to the main queue on expiry. After MQ_MAX_RETRIES
attempts (tracked in the ``x-attempt`` header) consumers publish to
``<queue>.dlq`` instead and ack.
"""

from __future__ import annotations

import aio_pika

from src.config import MQ_RETRY_DELAY_MS

JOBS_EXCHANGE = "agronomist.jobs"

UPLOAD_PREPARE_QUEUE = "upload.prepare"
UPLOAD_PORTIONS_QUEUE = "upload.portions"
UPLOAD_FINALIZE_QUEUE = "upload.finalize"
TRAINING_ONLINE_QUEUE = "training.online"
TRAINING_RETRAIN_QUEUE = "training.retrain"

MAIN_QUEUES = (
    UPLOAD_PREPARE_QUEUE,      # NEW — declared first
    UPLOAD_PORTIONS_QUEUE,
    UPLOAD_FINALIZE_QUEUE,
    TRAINING_ONLINE_QUEUE,
    TRAINING_RETRAIN_QUEUE,
)

ATTEMPT_HEADER = "x-attempt"


def retry_queue(queue_name: str) -> str:
    return f"{queue_name}.retry"


def dlq_queue(queue_name: str) -> str:
    return f"{queue_name}.dlq"


async def declare_topology(
    channel: aio_pika.abc.AbstractChannel,
) -> aio_pika.abc.AbstractExchange:
    """Idempotent declaration; called by both publisher and worker startup."""
    exchange = await channel.declare_exchange(
        JOBS_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )

    for name in MAIN_QUEUES:
        main = await channel.declare_queue(name, durable=True)
        await main.bind(exchange, routing_key=name)

        retry = await channel.declare_queue(
            retry_queue(name),
            durable=True,
            arguments={
                "x-message-ttl": MQ_RETRY_DELAY_MS,
                "x-dead-letter-exchange": JOBS_EXCHANGE,
                "x-dead-letter-routing-key": name,
            },
        )
        await retry.bind(exchange, routing_key=retry_queue(name))

        dlq = await channel.declare_queue(dlq_queue(name), durable=True)
        await dlq.bind(exchange, routing_key=dlq_queue(name))

    return exchange
