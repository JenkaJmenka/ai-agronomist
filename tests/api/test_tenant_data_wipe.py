"""Tests for ``src/api/services/tenant_data_wipe.py``.

Covers the full-wipe entry point (``wipe_tenant_data``) and the two
best-effort helpers shared with ``wipe_live_demo_data``.  The marker-specific
behaviour of the *live* wipe lives in
``test_synthetic_marker_contract_live_wipe.py``; this file is the module's
baseline coverage.

Why this matters: ``wipe_tenant_data`` issues unconditional multi-table
DELETEs behind ``DELETE /upload/data`` (``src/api/routes/upload.py:227``).  It
had no tests at all, so neither its blast radius nor its tenant scoping was
pinned anywhere — a cascade that silently grew to a new table, or lost a
``tenant_id`` predicate, would have shipped green.

``MetricsSnapshot`` has no ``greenhouse_id`` column, so its deletion is
deliberately tenant-wide rather than greenhouse-scoped
(``tenant_data_wipe.py:144-149``).  That asymmetry is pinned explicitly below
so it reads as a decision rather than a bug.

Redis is not required: ``_clear_live_stream`` and ``invalidate_response_cache``
both swallow every exception and return ``False``, so a missing Redis only
produces warning logs.
"""

from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.tenant_data_wipe import (
    _suppress_live_persistence,
    wipe_tenant_data,
)
from src.db.models import (
    Base,
    Incident,
    IncidentAction,
    LearningLog,
    MetricsSnapshot,
    OperatorFeedback,
    RawReading,
    SensorReading,
)

TENANT = "tenant_wipe"
OTHER_TENANT = "tenant_wipe_other"
GREENHOUSE = "gh_wipe"
OTHER_GREENHOUSE = "gh_wipe_other"

_T0 = datetime.datetime(2026, 6, 1, 0, 0, tzinfo=datetime.timezone.utc)
_STEP = datetime.timedelta(minutes=5)


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class _FakeLiveRuntime:
    def __init__(self) -> None:
        self.suppressed: list[tuple[str, str]] = []

    def suppress_persistence(self, tenant_id: str, greenhouse_id: str) -> None:
        self.suppressed.append((tenant_id, greenhouse_id))


class _WedgedLiveRuntime:
    """Runtime whose suppression call fails — the wipe must proceed anyway."""

    def suppress_persistence(self, tenant_id: str, greenhouse_id: str) -> None:
        raise RuntimeError("live runtime is wedged")


def _incident(*, tenant_id: str, greenhouse_id: str) -> Incident:
    return Incident(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        detected_at=_T0,
        sequence=[[21.0, 60.0, 420.0, 300.0]],
        sequence_length=1,
        anomaly_score=0.8,
        embedding=[0.0] * 8,
        embedding_uncertainty=0.1,
    )


def _sensor_reading(*, tenant_id: str, greenhouse_id: str, index: int) -> SensorReading:
    return SensorReading(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        timestamp=_T0 + index * _STEP,
        temperature=21.0,
        humidity=60.0,
        co2=420.0,
        light=300.0,
        anomaly_score=0.3,
    )


def _raw_reading(*, tenant_id: str, greenhouse_id: str, index: int) -> RawReading:
    return RawReading(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        timestamp=_T0 + index * _STEP,
        source="csv",
        data={"temperature": 21.0},
    )


async def _seed_full_scope(db: AsyncSession) -> str:
    """Seed one row per wiped table for the target scope. Returns the incident id."""
    incident = _incident(tenant_id=TENANT, greenhouse_id=GREENHOUSE)
    db.add(incident)
    await db.flush()
    db.add_all(
        [
            IncidentAction(incident_id=incident.id, action="vent_open"),
            _sensor_reading(tenant_id=TENANT, greenhouse_id=GREENHOUSE, index=0),
            _raw_reading(tenant_id=TENANT, greenhouse_id=GREENHOUSE, index=0),
            OperatorFeedback(id="fb_target", customer_id=TENANT, is_false_positive=True),
            LearningLog(tenant_id=TENANT, greenhouse_id=GREENHOUSE, event_type="drift_check"),
            MetricsSnapshot(tenant_id=TENANT),
        ]
    )
    await db.commit()
    return incident.id


async def _seed_neighbouring_scopes(db: AsyncSession) -> None:
    """Seed rows that must survive: another tenant, and another greenhouse."""
    db.add_all(
        [
            _incident(tenant_id=OTHER_TENANT, greenhouse_id=GREENHOUSE),
            _incident(tenant_id=TENANT, greenhouse_id=OTHER_GREENHOUSE),
            _sensor_reading(tenant_id=OTHER_TENANT, greenhouse_id=GREENHOUSE, index=1),
            _sensor_reading(tenant_id=TENANT, greenhouse_id=OTHER_GREENHOUSE, index=1),
            _raw_reading(tenant_id=OTHER_TENANT, greenhouse_id=GREENHOUSE, index=1),
            _raw_reading(tenant_id=TENANT, greenhouse_id=OTHER_GREENHOUSE, index=1),
            OperatorFeedback(id="fb_other", customer_id=OTHER_TENANT),
            LearningLog(
                tenant_id=TENANT, greenhouse_id=OTHER_GREENHOUSE, event_type="drift_check"
            ),
            MetricsSnapshot(tenant_id=OTHER_TENANT),
        ]
    )
    await db.commit()


async def _count(db: AsyncSession, model, *predicates) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(*predicates))


# ---------------------------------------------------------------------------
# wipe_tenant_data — blast radius
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_report_one_deleted_row_per_wiped_table(db_session: AsyncSession) -> None:
    """The summary counts must account for every table in the cascade."""
    await _seed_full_scope(db_session)

    result = await wipe_tenant_data(db_session, _FakeLiveRuntime(), TENANT, GREENHOUSE)

    assert {key: value for key, value in result.items() if key.startswith("deleted_")} == {
        "deleted_raw": 1,
        "deleted_sensor": 1,
        "deleted_incidents": 1,
        "deleted_incident_actions": 1,
        "deleted_feedback": 1,
        "deleted_learning_logs": 1,
        "deleted_metrics_snapshots": 1,
    }
    assert result["cleared"] is True


@pytest.mark.asyncio
async def test_should_delete_incident_actions_of_the_wiped_incidents(
    db_session: AsyncSession,
) -> None:
    """Actions are keyed by ``incident_id`` only, so they must be collected first.

    Deleting the incidents before their actions would orphan every action row.
    """
    incident_id = await _seed_full_scope(db_session)

    await wipe_tenant_data(db_session, _FakeLiveRuntime(), TENANT, GREENHOUSE)

    assert await _count(db_session, IncidentAction, IncidentAction.incident_id == incident_id) == 0


@pytest.mark.asyncio
async def test_should_leave_other_tenants_and_greenhouses_intact(
    db_session: AsyncSession,
) -> None:
    """Every greenhouse-scoped table must keep its neighbouring rows."""
    await _seed_full_scope(db_session)
    await _seed_neighbouring_scopes(db_session)

    await wipe_tenant_data(db_session, _FakeLiveRuntime(), TENANT, GREENHOUSE)

    assert await _count(db_session, SensorReading) == 2
    assert await _count(db_session, RawReading) == 2
    assert await _count(db_session, Incident) == 2
    assert await _count(db_session, LearningLog) == 1
    assert await _count(db_session, OperatorFeedback) == 1


@pytest.mark.asyncio
async def test_should_wipe_metrics_snapshots_tenant_wide_not_per_greenhouse(
    db_session: AsyncSession,
) -> None:
    """Pinned asymmetry: ``MetricsSnapshot`` has no ``greenhouse_id`` column.

    Its deletion is deliberately tenant-wide (``tenant_data_wipe.py:144-149``)
    so stale aggregates cannot outlive the readings they summarise — a wider
    blast radius than every other table in the cascade.
    """
    await _seed_full_scope(db_session)
    db_session.add(MetricsSnapshot(tenant_id=TENANT))
    db_session.add(MetricsSnapshot(tenant_id=OTHER_TENANT))
    await db_session.commit()

    result = await wipe_tenant_data(db_session, _FakeLiveRuntime(), TENANT, OTHER_GREENHOUSE)

    assert result["deleted_metrics_snapshots"] == 2, "both snapshots of the tenant go"
    assert await _count(db_session, MetricsSnapshot, MetricsSnapshot.tenant_id == OTHER_TENANT) == 1


@pytest.mark.asyncio
async def test_should_report_zero_counts_when_the_scope_is_already_empty(
    db_session: AsyncSession,
) -> None:
    """An empty scope is a success with zero counts, not an error."""
    result = await wipe_tenant_data(db_session, _FakeLiveRuntime(), TENANT, GREENHOUSE)

    assert result["cleared"] is True
    assert result["deleted_sensor"] == 0
    assert result["deleted_incident_actions"] == 0


# ---------------------------------------------------------------------------
# Live-persistence suppression (shared with wipe_live_demo_data)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_suppress_live_persistence_before_deleting(
    db_session: AsyncSession,
) -> None:
    """Mock SCADA must be muted first, or it re-seeds the tables mid-wipe."""
    live_runtime = _FakeLiveRuntime()
    await _seed_full_scope(db_session)

    result = await wipe_tenant_data(db_session, live_runtime, TENANT, GREENHOUSE)

    assert live_runtime.suppressed == [(TENANT, GREENHOUSE)]
    assert result["live_persistence_suppressed"] is True


@pytest.mark.asyncio
async def test_should_still_wipe_when_the_live_runtime_is_wedged(
    db_session: AsyncSession,
) -> None:
    """A failing suppression is reported, not raised — the delete must land."""
    await _seed_full_scope(db_session)

    result = await wipe_tenant_data(db_session, _WedgedLiveRuntime(), TENANT, GREENHOUSE)

    assert result["live_persistence_suppressed"] is False
    assert result["deleted_sensor"] == 1
    assert await _count(db_session, SensorReading) == 0


def test_should_fall_back_to_scope_control_when_no_runtime_is_passed(monkeypatch) -> None:
    """``live_runtime=None`` routes suppression through the process-wide scope.

    This is the worker/CLI path, where no ``LiveRuntime`` instance exists.
    """
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.live.scope_control.suppress_live_scope",
        lambda tenant_id, greenhouse_id: calls.append((tenant_id, greenhouse_id)),
    )

    suppressed = _suppress_live_persistence(None, TENANT, GREENHOUSE, "test_context")

    assert suppressed is True
    assert calls == [(TENANT, GREENHOUSE)]
