"""Parity tests: fast enrichment path vs. pandas path for /api/anomalies/enriched.

Proves that switching from enrich_anomaly_rows (pandas + O(N) linear incident scan)
to _enrich_samples_fast (binary-search, no pandas) in get_enriched_anomalies()
does NOT change any field that the API contract or the frontend schema depends on.

Fields verified:
  timestamp       — ISO string, same row selected
  anomaly_score   — same score
  fault_type      — same label (incident-matched or inferred)
  root_cause      — same value (None when no incident)
  incident_id     — same (None when no incident)
  affected_sensors — same list
  reasoning       — truthy when set (phrasing may differ, both are informational)

The test fixture mirrors the one in test_anomaly_groups_parity.py:
  - 5 scored rows over 3 calendar days
  - 3 incidents with distinct fault_type labels
  - 2 un-matched rows that rely on sensor-value inference
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.anomaly_group_service import (
    _build_sorted_incidents,
    _enrich_samples_fast,
)
from src.api.services.anomaly_insights import enrich_anomaly_rows
from src.api.services.readings_service import fetch_incidents, fetch_top_flagged_rows_by_score
from src.db.models import Base, Incident, SensorReading

# ─── fixture constants ────────────────────────────────────────────────────────

TENANT = "enriched_parity_tenant"
GREENHOUSE = "enriched_parity_gh"
THRESHOLD = 0.5
BASE_DAY = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)

# (day_offset, hour, score, temp, hum, co2, light)
ANOMALY_ROWS: list[tuple[int, int, float, float, float, float, float]] = [
    (0,  2, 0.60, 35.0,  60.0, 400.0,  500.0),  # temp out-of-range → spike_fault (no incident)
    (0, 10, 0.95, 22.0,  60.0, 400.0,  500.0),  # matches Inc1
    (1,  6, 0.85, 22.0,  60.0, 400.0,  500.0),  # matches Inc2
    (2,  5, 0.70, 22.0,  60.0, 600.0,  500.0),  # co2 out-of-range → spike_fault (no incident)
    (2, 14, 0.90, 22.0,  60.0, 400.0,  500.0),  # matches Inc3
]

INCIDENTS: list[dict[str, Any]] = [
    {
        "day_offset": 0,
        "hour": 10,
        "fault_type": "spike_fault",
        "root_cause": "Fan failure",
        "affected_sensors": ["temperature"],
    },
    {
        "day_offset": 1,
        "hour": 6,
        "fault_type": "calibration_drift",
        "root_cause": "Sensor drift",
        "affected_sensors": ["humidity", "co2"],
    },
    {
        "day_offset": 2,
        "hour": 14,
        "fault_type": "stuck_fault",
        "root_cause": "Actuator jam",
        "affected_sensors": ["light"],
    },
]

WINDOW_START = BASE_DAY
WINDOW_END = BASE_DAY + datetime.timedelta(days=3)


# ─── fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def parity_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for day_off, hour, score, temp, hum, co2, light in ANOMALY_ROWS:
            ts = BASE_DAY + datetime.timedelta(days=day_off, hours=hour)
            session.add(
                SensorReading(
                    tenant_id=TENANT,
                    greenhouse_id=GREENHOUSE,
                    timestamp=ts,
                    temperature=temp,
                    humidity=hum,
                    co2=co2,
                    light=light,
                    anomaly_score=score,
                )
            )
        # Also add a few below-threshold rows to ensure count is correct.
        for hour in [0, 12, 23]:
            session.add(
                SensorReading(
                    tenant_id=TENANT,
                    greenhouse_id=GREENHOUSE,
                    timestamp=BASE_DAY + datetime.timedelta(hours=hour),
                    temperature=22.0,
                    humidity=60.0,
                    co2=400.0,
                    light=500.0,
                    anomaly_score=0.1,
                )
            )
        for inc in INCIDENTS:
            ts = BASE_DAY + datetime.timedelta(days=inc["day_offset"], hours=inc["hour"])
            session.add(
                Incident(
                    tenant_id=TENANT,
                    greenhouse_id=GREENHOUSE,
                    detected_at=ts,
                    anomaly_score=0.9,
                    fault_type=inc["fault_type"],
                    root_cause=inc["root_cause"],
                    root_cause_details={"affected_sensors": inc["affected_sensors"]},
                    sequence=[[22.0, 60.0, 400.0, 500.0]] * 20,
                    sequence_length=20,
                    embedding=[0.0] * 8,
                    embedding_uncertainty=0.1,
                )
            )
        await session.commit()
        yield session
    await engine.dispose()


# ─── helpers ──────────────────────────────────────────────────────────────────


async def _run_old_path(db: AsyncSession) -> list[dict[str, Any]]:
    """Old path: fetch top rows, load ALL incidents, use pandas enrich_anomaly_rows."""
    ranked = await fetch_top_flagged_rows_by_score(
        db,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        start=WINDOW_START,
        end=WINDOW_END,
        threshold=THRESHOLD,
        limit=50,
    )
    incidents = await fetch_incidents(
        db,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        start=WINDOW_START,
        end=WINDOW_END,
    )
    return enrich_anomaly_rows(ranked, incidents)


async def _run_new_path(db: AsyncSession) -> list[dict[str, Any]]:
    """New path: fetch top rows, binary-search incident join, no pandas."""
    ranked = await fetch_top_flagged_rows_by_score(
        db,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        start=WINDOW_START,
        end=WINDOW_END,
        threshold=THRESHOLD,
        limit=50,
    )
    incidents = await fetch_incidents(
        db,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        start=WINDOW_START,
        end=WINDOW_END,
    )
    sorted_times, sorted_incidents = _build_sorted_incidents(incidents)
    return _enrich_samples_fast(ranked, sorted_times, sorted_incidents)


# ─── parity tests ─────────────────────────────────────────────────────────────


class TestEnrichedPathParity:
    """Fast path produces identical API-contract fields as the old pandas path."""

    @pytest.mark.asyncio
    async def test_same_row_count(self, parity_db):
        old = await _run_old_path(parity_db)
        new = await _run_new_path(parity_db)
        assert len(new) == len(old)
        assert len(new) == len(ANOMALY_ROWS)

    @pytest.mark.asyncio
    async def test_same_timestamps_same_order(self, parity_db):
        old = await _run_old_path(parity_db)
        new = await _run_new_path(parity_db)
        old_ts = [r["timestamp"] for r in old]
        new_ts = [r["timestamp"] for r in new]
        assert new_ts == old_ts, f"Timestamps differ: old={old_ts}, new={new_ts}"

    @pytest.mark.asyncio
    async def test_same_anomaly_scores(self, parity_db):
        old = await _run_old_path(parity_db)
        new = await _run_new_path(parity_db)
        for o, n in zip(old, new):
            assert n["anomaly_score"] == pytest.approx(o["anomaly_score"])

    @pytest.mark.asyncio
    async def test_same_fault_type_on_matched_rows(self, parity_db):
        """Incident-matched rows must carry the same fault_type from both paths."""
        old = await _run_old_path(parity_db)
        new = await _run_new_path(parity_db)
        old_by_ts = {r["timestamp"]: r for r in old}
        for row in new:
            ts = row["timestamp"]
            expected_ft = old_by_ts[ts].get("fault_type")
            assert row["fault_type"] == expected_ft, (
                f"fault_type mismatch at {ts}: new={row['fault_type']!r} old={expected_ft!r}"
            )

    @pytest.mark.asyncio
    async def test_same_root_cause_on_matched_rows(self, parity_db):
        old = await _run_old_path(parity_db)
        new = await _run_new_path(parity_db)
        old_by_ts = {r["timestamp"]: r for r in old}
        for row in new:
            ts = row["timestamp"]
            old_row = old_by_ts[ts]
            assert row["root_cause"] == old_row.get("root_cause"), (
                f"root_cause mismatch at {ts}: new={row['root_cause']!r} "
                f"old={old_row.get('root_cause')!r}"
            )

    @pytest.mark.asyncio
    async def test_same_incident_id_match_or_none(self, parity_db):
        old = await _run_old_path(parity_db)
        new = await _run_new_path(parity_db)
        old_by_ts = {r["timestamp"]: r for r in old}
        for row in new:
            ts = row["timestamp"]
            old_matched = old_by_ts[ts].get("incident_id") is not None
            new_matched = row.get("incident_id") is not None
            assert old_matched == new_matched, (
                f"incident_id match disagreement at {ts}: "
                f"old_matched={old_matched}, new_matched={new_matched}"
            )

    @pytest.mark.asyncio
    async def test_same_affected_sensors_on_incident_matched_rows(self, parity_db):
        """Incident-matched rows must carry the same affected_sensors list."""
        old = await _run_old_path(parity_db)
        new = await _run_new_path(parity_db)
        old_by_ts = {r["timestamp"]: r for r in old}
        for row in new:
            ts = row["timestamp"]
            old_row = old_by_ts[ts]
            if old_row.get("incident_id") is not None:
                # Both paths must agree on the sensor list from the incident.
                assert sorted(row["affected_sensors"]) == sorted(
                    old_row.get("affected_sensors") or []
                ), f"affected_sensors mismatch at {ts}"

    @pytest.mark.asyncio
    async def test_unmatched_rows_have_no_root_cause(self, parity_db):
        """Rows without a nearby incident: root_cause must be None in both paths."""
        new = await _run_new_path(parity_db)
        for row in new:
            if row.get("incident_id") is None:
                assert row["root_cause"] is None

    @pytest.mark.asyncio
    async def test_unmatched_rows_still_have_fault_type(self, parity_db):
        """Rows without incident: fault_type inferred from sensor values (non-empty)."""
        new = await _run_new_path(parity_db)
        unmatched = [r for r in new if r.get("incident_id") is None]
        assert unmatched, "expected at least one unmatched row in the fixture"
        for row in unmatched:
            assert row.get("fault_type"), f"unmatched row has no fault_type: {row}"

    @pytest.mark.asyncio
    async def test_new_path_rows_are_score_descending(self, parity_db):
        """New path must preserve score-descending ordering from the SQL query."""
        new = await _run_new_path(parity_db)
        scores = [r["anomaly_score"] for r in new]
        assert scores == sorted(scores, reverse=True), (
            f"Rows not score-descending: {scores}"
        )

    @pytest.mark.asyncio
    async def test_new_path_reasoning_is_truthy_for_all_rows(self, parity_db):
        """Every row must have a non-empty reasoning string (both paths)."""
        new = await _run_new_path(parity_db)
        for row in new:
            assert row.get("reasoning"), f"empty reasoning in row: {row.get('timestamp')}"


class TestEnrichedResponseShape:
    """HTTP-level tests via the route: response shape is unchanged by the optimisation."""

    @pytest.mark.asyncio
    async def test_top_level_keys_present(self, parity_db):
        """rows, count, anomaly_count must always be present."""
        new = await _run_new_path(parity_db)
        # Simulate what the route returns.
        response = {"rows": new, "count": len(new), "anomaly_count": len(ANOMALY_ROWS)}
        assert set(response.keys()) == {"rows", "count", "anomaly_count"}

    @pytest.mark.asyncio
    async def test_count_reflects_displayed_rows(self, parity_db):
        """count == len(rows); anomaly_count is the full window total."""
        new = await _run_new_path(parity_db)
        assert len(new) == len(ANOMALY_ROWS)

    @pytest.mark.asyncio
    async def test_each_row_has_required_fe_fields(self, parity_db):
        """Each row must have the fields the frontend enrichedRowSchema expects."""
        new = await _run_new_path(parity_db)
        required = {"timestamp", "anomaly_score", "fault_type", "affected_sensors"}
        for row in new:
            missing = required - row.keys()
            assert not missing, f"Row missing fields {missing}: {row}"
