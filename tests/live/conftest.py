"""Shared fixtures for live monitoring test suite.

Provides: fakeredis hub factory, fake detector objects, tiny-interval configs
via monkeypatch, and common test helpers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator, List
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from src.live.events import LiveReadingEvent, SourceReading, SourceStatusEvent
from src.live.hub import LiveHub
from src.live.sources.base import SourceConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT = "t1"
GREENHOUSE = "gh_A"
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fakeredis fixtures
# ---------------------------------------------------------------------------


async def _safe_redis_close(client: FakeRedis) -> None:
    """Close a redis/FakeRedis client in a version-safe way.

    redis-py 5.x introduced ``aclose()``; the 4.x series (pinned in
    requirements.txt) only has ``close()``.  Prefer ``aclose()`` when present
    so the helper works on both versions without deprecation warnings.
    """
    close_fn = getattr(client, "aclose", None) or client.close
    await close_fn()


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[FakeRedis, None]:
    """Shared fakeredis client for a single test."""
    client = FakeRedis()
    yield client
    await _safe_redis_close(client)


@pytest_asyncio.fixture
async def hub(redis_client: FakeRedis) -> LiveHub:
    """LiveHub wired to the shared fakeredis client with short block."""
    return LiveHub(redis_client=redis_client, block_ms=100)


def make_hub_factory() -> LiveHub:
    """Create a LiveHub with a per-subscriber factory (for fan-out isolation tests)."""
    def factory() -> FakeRedis:
        return FakeRedis()
    return LiveHub(redis_factory=factory, block_ms=100)


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def make_reading(
    channels: dict[str, float] | None = None,
    tenant_id: str = TENANT,
    greenhouse_id: str = GREENHOUSE,
    anomaly_score: float | None = None,
    seq: str = "",
    timestamp: datetime | None = None,
) -> LiveReadingEvent:
    """Build a minimal LiveReadingEvent."""
    ch = channels or {"temperature": 22.5, "humidity": 65.0}
    return LiveReadingEvent(
        seq=seq,
        timestamp=timestamp or NOW,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        channels=ch,
        sources={k: f"src_{k}" for k in ch},
        anomaly_score=anomaly_score,
    )


def make_source_reading(
    source_id: str = "climate",
    channels: dict[str, float] | None = None,
    timestamp: datetime | None = None,
    tenant_id: str = TENANT,
    greenhouse_id: str = GREENHOUSE,
) -> SourceReading:
    """Build a SourceReading."""
    ch = channels or {"temperature": 22.5}
    return SourceReading(
        source_id=source_id,
        vendor="mock",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        timestamp=timestamp or NOW,
        channels=ch,
    )


# ---------------------------------------------------------------------------
# Source config helpers
# ---------------------------------------------------------------------------


def make_source_config(
    source_id: str = "climate",
    channels: list[str] | None = None,
    priority: int = 10,
    poll_interval: float = 0.01,
    vendor: str = "mock",
) -> SourceConfig:
    """Build a SourceConfig for testing."""
    return SourceConfig(
        source_id=source_id,
        vendor=vendor,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        channels=channels or ["temperature", "humidity"],
        priority=priority,
        poll_interval_seconds=poll_interval,
    )


# ---------------------------------------------------------------------------
# Fake detector for scoring tests (avoids torch import)
# ---------------------------------------------------------------------------


def make_fake_detector():
    """Build a fake AnomalyDetector that returns predictable values.

    Returns a MagicMock that mimics the detector.model(tensor) interface.
    """
    import numpy as np

    detector = MagicMock()
    # Make model callable: returns (reconstructed, mu, logvar, z)
    # reconstructed matches input shape -> zero error -> low score
    def model_call(tensor):
        # Return near-identical reconstruction -> near-zero error
        return tensor.clone(), None, None, None

    detector.model = MagicMock(side_effect=model_call)
    detector.model.eval = MagicMock()
    return detector


def make_high_score_detector():
    """Build a fake detector that produces high reconstruction errors."""
    import numpy as np

    detector = MagicMock()

    def model_call(tensor):
        import torch
        # Return zeros -> high error against non-zero input
        return torch.zeros_like(tensor), None, None, None

    detector.model = MagicMock(side_effect=model_call)
    detector.model.eval = MagicMock()
    return detector


# ---------------------------------------------------------------------------
# Collector helpers
# ---------------------------------------------------------------------------


async def collect_replay(
    hub: LiveHub,
    tenant_id: str = TENANT,
    greenhouse_id: str = GREENHOUSE,
    last_event_id: str | None = None,
) -> list[tuple[str, str, str]]:
    """Subscribe and collect entries until the first idle tick."""
    items: list[tuple[str, str, str]] = []
    async for entry_id, event_type, data_json in hub.subscribe(
        tenant_id, greenhouse_id, last_event_id=last_event_id,
    ):
        if event_type == "idle":
            break
        items.append((entry_id, event_type, data_json))
    return items
