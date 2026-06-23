"""Unit tests for async IncidentMemory over SQLite (numpy cosine fallback)."""

from __future__ import annotations

import datetime
import logging
from typing import List
from unittest.mock import patch

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import LATENT_DIM, SEQ_LEN
from src.db.embedding_type import EmbeddingType
from src.db.incident_memory import (
    CONFIRMED_ANOMALY_TAG,
    FALSE_POSITIVE_TAG,
    IncidentMemory,
)
from src.db.models import Base, Incident, IncidentAction
from src.db.vector_search import compute_cosine_distance


def unit_embedding(axis: int = 0, sign: float = 1.0) -> List[float]:
    """Unit vector along one latent axis."""
    embedding = [0.0] * LATENT_DIM
    embedding[axis] = sign
    return embedding


def sample_sequence() -> List[List[float]]:
    """Valid sensor sequence [seq_len, input_dim]."""
    row = [22.0, 60.0, 400.0, 500.0]
    return [row[:] for _ in range(SEQ_LEN)]


@pytest_asyncio.fixture
async def memory_session() -> AsyncSession:
    """Isolated in-memory async SQLite session with incident tables."""
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
async def memory(memory_session: AsyncSession) -> IncidentMemory:
    return IncidentMemory(memory_session)


@pytest.mark.asyncio
async def test_store_incident_returns_uuid(memory: IncidentMemory) -> None:
    incident_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=unit_embedding(0),
        anomaly_score=0.85,
        sequence=sample_sequence(),
        embedding_uncertainty=0.12,
        greenhouse_id="gh_1",
        root_cause="boiler_failure",
        tags=["heating"],
        notes="Pilot light out",
    )
    assert isinstance(incident_id, str)
    assert len(incident_id) == 36


@pytest.mark.asyncio
async def test_find_similar_orders_by_cosine_distance(memory: IncidentMemory) -> None:
    query = unit_embedding(0)
    opposite = unit_embedding(0, sign=-1.0)
    orthogonal = unit_embedding(1)

    await memory.store_incident(
        tenant_id="tenant_a",
        embedding=opposite,
        anomaly_score=0.9,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.store_incident(
        tenant_id="tenant_a",
        embedding=query,
        anomaly_score=0.8,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.store_incident(
        tenant_id="tenant_a",
        embedding=orthogonal,
        anomaly_score=0.7,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )

    similar = await memory.find_similar(embedding=query, tenant_id="tenant_a", k=3)
    assert len(similar) == 3
    distances = [distance for _incident, distance in similar]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0, abs=1e-6)
    assert distances[-1] == pytest.approx(2.0, abs=1e-6)


@pytest.mark.asyncio
async def test_find_similar_tenant_isolation(memory: IncidentMemory) -> None:
    embedding = unit_embedding(0)
    await memory.store_incident(
        tenant_id="tenant_a",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.store_incident(
        tenant_id="tenant_b",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )

    results = await memory.find_similar(embedding=embedding, tenant_id="tenant_a", k=5)
    assert len(results) == 1
    assert results[0][0].tenant_id == "tenant_a"


@pytest.mark.asyncio
async def test_find_similar_greenhouse_filter(memory: IncidentMemory) -> None:
    embedding = unit_embedding(0)
    await memory.store_incident(
        tenant_id="tenant_a",
        greenhouse_id="gh_1",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.store_incident(
        tenant_id="tenant_a",
        greenhouse_id="gh_2",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )

    results = await memory.find_similar(
        embedding=embedding, tenant_id="tenant_a", greenhouse_id="gh_2", k=5
    )
    assert len(results) == 1
    assert results[0][0].greenhouse_id == "gh_2"


@pytest.mark.asyncio
async def test_find_similar_resolved_only_filter(memory: IncidentMemory) -> None:
    embedding = unit_embedding(0)
    open_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    resolved_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.resolve_incident(
        incident_id=resolved_id,
        tenant_id="tenant_a",
        root_cause="co2_leak",
        actions_taken=["ventilate"],
        actions_effective=True,
    )

    results = await memory.find_similar(
        embedding=embedding, tenant_id="tenant_a", resolved_only=True, k=5
    )
    returned_ids = {incident.id for incident, _distance in results}
    assert resolved_id in returned_ids
    assert open_id not in returned_ids


@pytest.mark.asyncio
async def test_find_similar_max_age_days_filter(
    memory: IncidentMemory, memory_session: AsyncSession
) -> None:
    embedding = unit_embedding(0)
    old_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    recent_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )

    old_incident = await memory_session.get(Incident, old_id)
    assert old_incident is not None
    old_incident.detected_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=30
    )
    await memory_session.commit()

    results = await memory.find_similar(
        embedding=embedding, tenant_id="tenant_a", max_age_days=7, k=5
    )
    returned_ids = {incident.id for incident, _distance in results}
    assert recent_id in returned_ids
    assert old_id not in returned_ids


@pytest.mark.asyncio
async def test_suggest_fixes_empty_history_warning(memory: IncidentMemory) -> None:
    suggestions = await memory.suggest_fixes(
        embedding=unit_embedding(0), tenant_id="tenant_a"
    )
    assert suggestions["warning"] == "No similar resolved incidents found"
    assert suggestions["confidence"] == 0
    assert suggestions["recommended_actions"] == []


@pytest.mark.asyncio
async def test_suggest_fixes_ranks_causes_and_filters_actions(memory: IncidentMemory) -> None:
    embedding = unit_embedding(0)
    for root_cause, action, effective in [
        ("boiler_failure", "restart_heating", True),
        ("boiler_failure", "replace_filter", True),
        ("co2_leak", "open_vents", False),
    ]:
        incident_id = await memory.store_incident(
            tenant_id="tenant_a",
            embedding=embedding,
            anomaly_score=0.8,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
            root_cause=root_cause,
        )
        await memory.resolve_incident(
            incident_id=incident_id,
            tenant_id="tenant_a",
            root_cause=root_cause,
            actions_taken=[action],
            actions_effective=effective,
        )

    suggestions = await memory.suggest_fixes(
        embedding=embedding, tenant_id="tenant_a", min_confidence=0.5
    )
    assert suggestions["most_likely_cause"]["root_cause"] == "boiler_failure"
    action_names = {entry["action"] for entry in suggestions["recommended_actions"]}
    assert "restart_heating" in action_names
    assert "open_vents" not in action_names


@pytest.mark.asyncio
async def test_resolve_incident_writes_actions_and_time_to_resolve(
    memory: IncidentMemory, memory_session: AsyncSession
) -> None:
    incident_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=unit_embedding(0),
        anomaly_score=0.6,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    incident = await memory_session.get(Incident, incident_id)
    assert incident is not None
    detected = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
    incident.detected_at = detected.replace(tzinfo=None)
    await memory_session.commit()

    await memory.resolve_incident(
        incident_id=incident_id,
        tenant_id="tenant_a",
        root_cause="sensor_drift",
        actions_taken=["recalibrate", "replace_sensor"],
        actions_effective=True,
        notes="Fixed after calibration",
    )

    await memory_session.refresh(incident)
    assert incident.resolved is True
    assert incident.root_cause == "sensor_drift"
    assert incident.time_to_resolve == pytest.approx(30, abs=2)

    actions = (
        await memory_session.scalars(
            select(IncidentAction).where(IncidentAction.incident_id == incident_id)
        )
    ).all()
    assert len(actions) == 2
    assert {action.action for action in actions} == {"recalibrate", "replace_sensor"}


@pytest.mark.asyncio
async def test_resolve_incident_rejects_already_resolved(
    memory: IncidentMemory, memory_session: AsyncSession
) -> None:
    from src.db.incident_memory import IncidentAlreadyResolvedError

    incident_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=unit_embedding(0),
        anomaly_score=0.6,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.resolve_incident(
        incident_id=incident_id,
        tenant_id="tenant_a",
        root_cause="sensor_drift",
        actions_taken=["recalibrate"],
        actions_effective=True,
    )

    with pytest.raises(IncidentAlreadyResolvedError):
        await memory.resolve_incident(
            incident_id=incident_id,
            tenant_id="tenant_a",
            root_cause="other_cause",
            actions_taken=["duplicate_action"],
            actions_effective=False,
        )

    actions = (
        await memory_session.scalars(
            select(IncidentAction).where(IncidentAction.incident_id == incident_id)
        )
    ).all()
    assert len(actions) == 1
    assert actions[0].action == "recalibrate"


@pytest.mark.asyncio
async def test_mark_anomaly_idempotent(memory: IncidentMemory, memory_session: AsyncSession) -> None:
    incident_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=unit_embedding(0),
        anomaly_score=0.4,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.mark_anomaly(incident_id=incident_id, tenant_id="tenant_a", is_anomaly=True)
    await memory.mark_anomaly(incident_id=incident_id, tenant_id="tenant_a", is_anomaly=True)

    incident = await memory_session.get(Incident, incident_id)
    assert incident is not None
    assert incident.tags.count(CONFIRMED_ANOMALY_TAG) == 1

    await memory.mark_anomaly(incident_id=incident_id, tenant_id="tenant_a", is_anomaly=False)
    assert incident.tags.count(FALSE_POSITIVE_TAG) == 1


@pytest.mark.asyncio
async def test_get_statistics(memory: IncidentMemory) -> None:
    embedding = unit_embedding(0)
    open_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    resolved_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=embedding,
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.resolve_incident(
        incident_id=resolved_id,
        tenant_id="tenant_a",
        root_cause="humidity_spike",
        actions_taken=["adjust_vent"],
        actions_effective=True,
    )
    _ = open_id

    stats = await memory.get_statistics(tenant_id="tenant_a")
    assert stats["total_incidents"] == 2
    assert stats["resolved_incidents"] == 1
    assert stats["resolved_rate"] == pytest.approx(0.5)
    assert any(cause["root_cause"] == "humidity_spike" for cause in stats["top_root_causes"])


@pytest.mark.asyncio
async def test_get_recurring_issues(memory: IncidentMemory) -> None:
    embedding = unit_embedding(0)
    for _ in range(3):
        incident_id = await memory.store_incident(
            tenant_id="tenant_a",
            embedding=embedding,
            anomaly_score=0.5,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )
        await memory.resolve_incident(
            incident_id=incident_id,
            tenant_id="tenant_a",
            root_cause="recurring_pump",
            actions_taken=["restart_pump"],
            actions_effective=True,
        )

    recurring = await memory.get_recurring_issues(tenant_id="tenant_a", min_occurrences=3)
    assert len(recurring) == 1
    assert recurring[0]["root_cause"] == "recurring_pump"
    assert recurring[0]["occurrences"] == 3


def test_embedding_type_dimension_enforcement() -> None:
    embedding_type = EmbeddingType(LATENT_DIM)
    dialect = sqlite_dialect.dialect()
    valid = unit_embedding(0)
    assert embedding_type.process_bind_param(valid, dialect) == valid
    with pytest.raises(ValueError, match="dimensions"):
        embedding_type.process_bind_param([1.0] * (LATENT_DIM - 1), dialect)


def test_compute_cosine_distance_opposite_vectors() -> None:
    vector_a = np.array(unit_embedding(0), dtype=np.float64)
    vector_b = np.array(unit_embedding(0, sign=-1.0), dtype=np.float64)
    assert compute_cosine_distance(vector_a, vector_b) == pytest.approx(2.0, abs=1e-6)


@pytest.mark.asyncio
async def test_sqlite_candidate_cap_warning(
    memory: IncidentMemory, caplog: pytest.LogCaptureFixture
) -> None:
    embedding = unit_embedding(0)
    with patch("src.db.vector_search.SQLITE_CANDIDATE_LIMIT", 2):
        for _ in range(3):
            await memory.store_incident(
                tenant_id="tenant_a",
                embedding=embedding,
                anomaly_score=0.5,
                sequence=sample_sequence(),
                embedding_uncertainty=0.1,
            )
        with caplog.at_level(logging.WARNING):
            await memory.find_similar(embedding=embedding, tenant_id="tenant_a", k=1)
    assert any("candidate cap" in record.message for record in caplog.records)
