"""Unit tests for the async LearningLogStore over in-memory SQLite (Gap 3)."""

from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db.learning_logs import (
    DEFAULT_HISTORY_LIMIT,
    DRIFT_CHECK_EVENT,
    LearningLogStore,
)
from src.db.models import Base, LearningLog

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def learning_session() -> AsyncSession:
    """Isolated in-memory async SQLite session with learning_logs table."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def store(learning_session: AsyncSession) -> LearningLogStore:
    return LearningLogStore(learning_session)


async def seed_event(
    store: LearningLogStore,
    *,
    tenant_id: str = "tenant_a",
    event_type: str = "observe_batch",
    created_at: datetime.datetime | None = None,
    **fields,
) -> str:
    """Log one event, optionally forcing a deterministic created_at."""
    event_id = await store.log_event(
        tenant_id=tenant_id, event_type=event_type, **fields
    )
    if created_at is not None:
        entry = await store._session.get(LearningLog, event_id)
        entry.created_at = created_at
        await store._session.commit()
    return event_id


def at_minute(minute: int) -> datetime.datetime:
    return datetime.datetime(2026, 6, 1, 12, minute, 0, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------


class TestLogEvent:
    async def test_returns_uuid_string(self, store: LearningLogStore) -> None:
        event_id = await store.log_event(
            tenant_id="tenant_a", event_type="observe_batch"
        )
        assert isinstance(event_id, str)
        assert len(event_id) == 36

    async def test_persists_row_with_defaults(
        self, store: LearningLogStore, learning_session: AsyncSession
    ) -> None:
        event_id = await store.log_event(
            tenant_id="tenant_a", event_type="observe_batch"
        )
        row = await learning_session.get(LearningLog, event_id)
        assert row is not None
        assert row.tenant_id == "tenant_a"
        assert row.greenhouse_id == "default"
        assert row.sequences_observed == 0
        assert row.updates_performed == 0
        assert row.created_at is not None

    async def test_persists_optional_fields(
        self, store: LearningLogStore, learning_session: AsyncSession
    ) -> None:
        event_id = await store.log_event(
            tenant_id="tenant_a",
            event_type=DRIFT_CHECK_EVENT,
            greenhouse_id="gh_7",
            sequences_observed=12,
            updates_performed=2,
            loss_before=0.5,
            loss_after=0.4,
            drift_score=0.42,
            drift_detected=True,
            details={"action": "retrain_model"},
            model_checkpoint_path="models/ckpt.pt",
        )
        row = await learning_session.get(LearningLog, event_id)
        assert row.greenhouse_id == "gh_7"
        assert row.sequences_observed == 12
        assert row.updates_performed == 2
        assert row.loss_before == pytest.approx(0.5)
        assert row.loss_after == pytest.approx(0.4)
        assert row.drift_score == pytest.approx(0.42)
        assert row.drift_detected is True
        assert row.details == {"action": "retrain_model"}
        assert row.model_checkpoint_path == "models/ckpt.pt"

    async def test_unknown_field_raises_value_error(
        self, store: LearningLogStore
    ) -> None:
        with pytest.raises(ValueError, match="Unknown learning-log fields"):
            await store.log_event(
                tenant_id="tenant_a",
                event_type="observe_batch",
                bogus_field=1,
            )


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


class TestGetHistory:
    async def test_tenant_isolation(self, store: LearningLogStore) -> None:
        await seed_event(store, tenant_id="tenant_a")
        await seed_event(store, tenant_id="tenant_b")
        history = await store.get_history(tenant_id="tenant_a")
        assert len(history) == 1
        assert all(entry.tenant_id == "tenant_a" for entry in history)

    async def test_newest_first_ordering(self, store: LearningLogStore) -> None:
        oldest = await seed_event(store, created_at=at_minute(0))
        newest = await seed_event(store, created_at=at_minute(30))
        middle = await seed_event(store, created_at=at_minute(15))
        history = await store.get_history(tenant_id="tenant_a")
        assert [entry.id for entry in history] == [newest, middle, oldest]

    async def test_event_type_filter(self, store: LearningLogStore) -> None:
        await seed_event(store, event_type="observe_batch")
        await seed_event(store, event_type=DRIFT_CHECK_EVENT)
        history = await store.get_history(
            tenant_id="tenant_a", event_type=DRIFT_CHECK_EVENT
        )
        assert len(history) == 1
        assert history[0].event_type == DRIFT_CHECK_EVENT

    async def test_greenhouse_filter(self, store: LearningLogStore) -> None:
        await seed_event(store, greenhouse_id="gh_1")
        await seed_event(store, greenhouse_id="gh_2")
        history = await store.get_history(tenant_id="tenant_a", greenhouse_id="gh_2")
        assert len(history) == 1
        assert history[0].greenhouse_id == "gh_2"

    async def test_limit_caps_results(self, store: LearningLogStore) -> None:
        for minute in range(5):
            await seed_event(store, created_at=at_minute(minute))
        history = await store.get_history(tenant_id="tenant_a", limit=2)
        assert len(history) == 2

    async def test_default_limit_constant(self) -> None:
        assert DEFAULT_HISTORY_LIMIT == 100

    async def test_empty_history_for_unknown_tenant(
        self, store: LearningLogStore
    ) -> None:
        assert await store.get_history(tenant_id="ghost") == []


# ---------------------------------------------------------------------------
# get_latest_drift
# ---------------------------------------------------------------------------


class TestGetLatestDrift:
    async def test_returns_none_without_drift_events(
        self, store: LearningLogStore
    ) -> None:
        await seed_event(store, event_type="observe_batch")
        assert await store.get_latest_drift(tenant_id="tenant_a") is None

    async def test_returns_most_recent_drift_check(
        self, store: LearningLogStore
    ) -> None:
        await seed_event(
            store,
            event_type=DRIFT_CHECK_EVENT,
            drift_score=0.1,
            created_at=at_minute(0),
        )
        newest = await seed_event(
            store,
            event_type=DRIFT_CHECK_EVENT,
            drift_score=0.9,
            created_at=at_minute(10),
        )
        latest = await store.get_latest_drift(tenant_id="tenant_a")
        assert latest is not None
        assert latest.id == newest
        assert latest.drift_score == pytest.approx(0.9)

    async def test_scoped_to_tenant(self, store: LearningLogStore) -> None:
        await seed_event(store, tenant_id="tenant_b", event_type=DRIFT_CHECK_EVENT)
        assert await store.get_latest_drift(tenant_id="tenant_a") is None

    async def test_greenhouse_filter(self, store: LearningLogStore) -> None:
        await seed_event(
            store, event_type=DRIFT_CHECK_EVENT, greenhouse_id="gh_1"
        )
        assert (
            await store.get_latest_drift(tenant_id="tenant_a", greenhouse_id="gh_2")
            is None
        )


# ---------------------------------------------------------------------------
# get_learning_summary
# ---------------------------------------------------------------------------


class TestGetLearningSummary:
    async def test_empty_summary_for_unknown_tenant(
        self, store: LearningLogStore
    ) -> None:
        summary = await store.get_learning_summary(tenant_id="ghost")
        assert summary == {
            "events_by_type": {},
            "total_events": 0,
            "total_updates_performed": 0,
            "last_event_at": None,
        }

    async def test_counts_per_event_type_and_updates_sum(
        self, store: LearningLogStore
    ) -> None:
        await seed_event(store, event_type="observe_batch", updates_performed=2)
        await seed_event(store, event_type="observe_batch", updates_performed=3)
        await seed_event(store, event_type=DRIFT_CHECK_EVENT)
        summary = await store.get_learning_summary(tenant_id="tenant_a")
        assert summary["events_by_type"] == {
            "observe_batch": 2,
            DRIFT_CHECK_EVENT: 1,
        }
        assert summary["total_events"] == 3
        assert summary["total_updates_performed"] == 5
        assert summary["last_event_at"] is not None

    async def test_summary_is_tenant_scoped(self, store: LearningLogStore) -> None:
        await seed_event(store, tenant_id="tenant_a", updates_performed=1)
        await seed_event(store, tenant_id="tenant_b", updates_performed=99)
        summary = await store.get_learning_summary(tenant_id="tenant_a")
        assert summary["total_events"] == 1
        assert summary["total_updates_performed"] == 1

    async def test_last_event_at_is_latest_timestamp(
        self, store: LearningLogStore
    ) -> None:
        await seed_event(store, event_type="observe_batch", created_at=at_minute(0))
        await seed_event(
            store, event_type=DRIFT_CHECK_EVENT, created_at=at_minute(45)
        )
        summary = await store.get_learning_summary(tenant_id="tenant_a")
        # SQLite returns naive datetimes; compare without tzinfo.
        assert summary["last_event_at"].replace(tzinfo=None) == at_minute(45).replace(
            tzinfo=None
        )
