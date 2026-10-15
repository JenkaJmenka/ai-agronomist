"""Harness shared by the stuck-fault episode view-equality pins.

Builds ONE persisted upload of ``tests/fixtures/native_dec_stuck_fault.csv``
and exposes it from both sides: the detection-time episodes the upload
produced, and the query-time episodes ``GET /api/anomalies/episodes``
recomputes from the same database.

Setup goes through the real sync upload path with a live session, because
``persist_stuck_fault_episodes`` only runs under ``persist_db is not None``
(``src/api/services/cold_run_upload.py``).  No ``anomaly_episodes`` row is
hand-inserted anywhere here — a hand-built row would pin the fixture
instead of the pipeline.

The detection-time dataclasses are captured by spying on the real
``group_alerts_into_episodes`` call inside that path: the upload response
schema drops two of the dataclass's fields (see the D3 note in
``test_episode_view_parity.py``), and building a ``StuckCarry`` in the test
to call the grouper directly would risk the imputed-channel phantom-freeze
trap described in ``test_anomaly_episode_persistence.py``.

Consumed by ``tests/api/test_episode_view_parity.py`` and
``tests/api/test_recovery_tag_fixture_a.py``.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.api.services import upload_portions
from src.api.services.cold_run_upload import run_cold_run_upload
from src.config import LATENT_DIM
from src.db.models import AnomalyEpisodeRecord, Base
from src.db.session import get_db
from src.vendors.csv_adapter import CSVAdapter

FIXTURE_A = Path(__file__).resolve().parents[1] / "fixtures" / "native_dec_stuck_fault.csv"
TENANT = "episode_view_parity"
GH_ID = "gh_view_parity"
CHUNK_ROWS = 500
STUCK_DETECTOR = "stuck_fault"
QUERY_THRESHOLD = 0.5

# Wide enough to contain every reading in fixture A (2026-12-14 .. 2026-12-25)
# without clamping either end — these readings are future-dated, so the end of
# a window is never "now" (project_future_dated_readings).
WINDOW_START = datetime(2026, 12, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2027, 1, 15, tzinfo=timezone.utc)


def mock_detector() -> MagicMock:
    """Constant sub-threshold LSTM score: stuck detection is model-independent."""
    detector = MagicMock()
    detector.predict_batch_enriched.side_effect = lambda seqs: (
        np.full(len(seqs), 0.05, dtype=np.float32),
        np.zeros((len(seqs), LATENT_DIM), dtype=np.float32),
        np.zeros(len(seqs), dtype=np.float32),
    )
    return detector


def stuck_only(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stuck-fault episodes only, oldest start first."""
    return sorted(
        (ep for ep in episodes if ep.get("detector") == STUCK_DETECTOR),
        key=lambda ep: ep["start_ts"],
    )


def instant(timestamp: str) -> datetime:
    """Parse an episode timestamp to a UTC instant.

    The pins compare instants, not strings, so a formatting difference
    between the two views can never read as a span disagreement.
    """
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@contextlib.contextmanager
def _capture_detection_episodes() -> Iterator[list[list[Any]]]:
    """Record what the REAL grouper returned inside the real upload path.

    The spy delegates to the production function with the production
    arguments and keeps only its result, so nothing about detection-time
    grouping is simulated — it is the call ``finalize_scores`` makes,
    observed.
    """
    captured: list[list[Any]] = []
    real_grouper = upload_portions.group_alerts_into_episodes

    def _spy(*args: Any, **kwargs: Any) -> list[Any]:
        episodes = real_grouper(*args, **kwargs)
        captured.append(episodes)
        return episodes

    with patch.object(upload_portions, "group_alerts_into_episodes", _spy):
        yield captured


class BothViews:
    """One persisted upload of fixture A, seen from the detection side."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        response_episodes: list[dict[str, Any]],
        detection_episodes: list[dict[str, Any]],
    ) -> None:
        self.session = session
        # Detection-time, as the upload RESPONSE exposes it.
        self.response = stuck_only(response_episodes)
        # Detection-time, as the grouper's dataclasses carry it — a superset
        # of the response's fields while D3 stands.
        self.detection = stuck_only(detection_episodes)
        # Every detector, unfiltered: the Task 5 tagging pins compare the
        # STATISTICAL episodes around a fault across the two views.
        self.all_response = sorted(response_episodes, key=lambda ep: ep["start_ts"])


async def _upload_fixture_a(session: AsyncSession) -> BothViews:
    """Run the real sync upload with persistence, keeping both detection views."""
    adapter = CSVAdapter({"filepath": str(FIXTURE_A)})
    adapter.connect()
    with _capture_detection_episodes() as captured:
        response, _ = await run_cold_run_upload(
            tmp_path=str(FIXTURE_A),
            adapter=adapter,
            detector=mock_detector(),
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            chunk_size=CHUNK_ROWS,
            persist_db=session,
        )
    await session.commit()
    assert len(captured) == 1, f"expected one grouping call, saw {len(captured)}"
    return BothViews(
        session=session,
        response_episodes=[ep.model_dump(mode="json") for ep in response.episodes],
        detection_episodes=[ep.to_dict() for ep in captured[0]],
    )


@contextlib.asynccontextmanager
async def _upload_into_fresh_db() -> AsyncIterator[BothViews]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            yield await _upload_fixture_a(session)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def both_views() -> AsyncIterator[BothViews]:
    """Fixture A uploaded with persistence: readings AND episode rows written."""
    async with _upload_into_fresh_db() as views:
        yield views


@pytest_asyncio.fixture
async def views_without_episode_records() -> AsyncIterator[BothViews]:
    """The same upload, then every ``anomaly_episodes`` row removed.

    Reproduces data uploaded before migration 026 existed, and the
    ``persist_db is None`` preview path: the flagged readings are there, the
    detector's durable record is not.  Rows are DELETED from a real
    persisted upload rather than a partial state being hand-built.
    """
    async with _upload_into_fresh_db() as views:
        await views.session.execute(delete(AnomalyEpisodeRecord))
        await views.session.commit()
        yield views


def _fetch_episodes(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(
        "/api/anomalies/episodes",
        params={
            "tenant_id": TENANT,
            "greenhouse_id": GH_ID,
            "start": WINDOW_START.isoformat(),
            "end": WINDOW_END.isoformat(),
            "threshold": QUERY_THRESHOLD,
        },
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["episodes"]


@contextlib.contextmanager
def _endpoint_view(views: BothViews) -> Iterator[list[dict[str, Any]]]:
    """EVERY query-time episode from the real endpoint, reading *views*' db."""
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield views.session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield _fetch_episodes(client)
    finally:
        app.dependency_overrides.clear()


@contextlib.contextmanager
def _query_view(views: BothViews) -> Iterator[list[dict[str, Any]]]:
    """Stuck episodes only — the span pins compare like with like."""
    with _endpoint_view(views) as episodes:
        yield stuck_only(episodes)


@pytest.fixture
def query_episodes(both_views: BothViews) -> Iterator[list[dict[str, Any]]]:
    """Query-time stuck episodes for an upload whose episode rows exist."""
    with _query_view(both_views) as episodes:
        yield episodes


@pytest.fixture
def all_query_episodes(both_views: BothViews) -> Iterator[list[dict[str, Any]]]:
    """Query-time episodes of EVERY detector, not just the stuck faults.

    The span pins want stuck faults alone; the Task 5 tagging pins
    (``test_recovery_tag_fixture_a.py``) are about the statistical
    episodes AROUND a fault, so they need the unfiltered list.
    """
    with _endpoint_view(both_views) as episodes:
        yield episodes


@pytest.fixture
def query_episodes_without_records(
    views_without_episode_records: BothViews,
) -> Iterator[list[dict[str, Any]]]:
    """Query-time stuck episodes for an upload with no episode rows left."""
    with _query_view(views_without_episode_records) as episodes:
        yield episodes
