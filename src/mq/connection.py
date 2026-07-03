"""Lazy, per-process RabbitMQ connection management.

The API must boot (and serve non-MQ endpoints) even when the broker is
unreachable — the connection is only opened on first use.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import aio_pika

from src.config import RABBITMQ_URL

_connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
_channel: Optional[aio_pika.abc.AbstractChannel] = None
_lock = asyncio.Lock()


async def get_mq_connection() -> aio_pika.abc.AbstractRobustConnection:
    global _connection
    async with _lock:
        if _connection is None or _connection.is_closed:
            _connection = await aio_pika.connect_robust(RABBITMQ_URL)
    return _connection


async def get_mq_channel() -> aio_pika.abc.AbstractChannel:
    global _channel
    connection = await get_mq_connection()
    async with _lock:
        if _channel is None or _channel.is_closed:
            _channel = await connection.channel()
    return _channel


async def close_mq() -> None:
    """FastAPI/worker shutdown hook; safe when never connected."""
    global _connection, _channel
    async with _lock:
        if _channel is not None and not _channel.is_closed:
            await _channel.close()
        _channel = None
        if _connection is not None and not _connection.is_closed:
            await _connection.close()
        _connection = None
