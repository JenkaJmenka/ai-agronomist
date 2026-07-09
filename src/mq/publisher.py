"""JSON publisher for job messages."""

from __future__ import annotations

import asyncio
import gzip

from aio_pika import DeliveryMode, Message
from pydantic import BaseModel

from src.mq.connection import get_mq_channel
from src.mq.topology import ATTEMPT_HEADER, declare_topology

# Bodies above this size are gzip-compressed (upload portions with wide CSVs).
GZIP_THRESHOLD_BYTES = 64 * 1024

_topology_declared = False


def encode_message_body(payload: BaseModel) -> tuple[bytes, str | None]:
    """Serialize a message model; returns (body, content_encoding)."""
    body = payload.model_dump_json().encode("utf-8")
    if len(body) > GZIP_THRESHOLD_BYTES:
        return gzip.compress(body), "gzip"
    return body, None


def decode_message_body(body: bytes, content_encoding: str | None) -> bytes:
    if content_encoding == "gzip":
        return gzip.decompress(body)
    return body


async def publish_raw(
    routing_key: str,
    body: bytes,
    *,
    content_encoding: str | None = None,
    headers: dict | None = None,
) -> None:
    """Publish opaque bytes (used to preserve unparseable messages in a DLQ)."""
    global _topology_declared
    channel = await get_mq_channel()
    if not _topology_declared:
        exchange = await declare_topology(channel)
        _topology_declared = True
    else:
        exchange = await channel.get_exchange("agronomist.jobs", ensure=False)
    await exchange.publish(
        Message(
            body,
            content_type="application/json",
            content_encoding=content_encoding,
            delivery_mode=DeliveryMode.PERSISTENT,
            headers=headers or {},
        ),
        routing_key=routing_key,
    )


async def publish_json(
    routing_key: str,
    payload: BaseModel,
    *,
    attempt: int = 0,
) -> None:
    """Publish a persistent JSON message to the jobs exchange."""
    global _topology_declared
    channel = await get_mq_channel()
    if not _topology_declared:
        exchange = await declare_topology(channel)
        _topology_declared = True
    else:
        exchange = await channel.get_exchange("agronomist.jobs", ensure=False)

    body, content_encoding = await asyncio.to_thread(encode_message_body, payload)
    message = Message(
        body,
        content_type="application/json",
        content_encoding=content_encoding,
        delivery_mode=DeliveryMode.PERSISTENT,
        headers={ATTEMPT_HEADER: attempt},
    )
    await exchange.publish(message, routing_key=routing_key)
