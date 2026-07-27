"""Parity tests: SQL path vs. Python path for /api/anomalies/groups.

Task requirement: "a fast wrong answer is worse than a slow right one."

Each test inserts a multi-day, multi-type, multi-sensor fixture, runs both
the old Python path (fetch_flagged_window → enrich_anomaly_rows →
group_anomaly_rows) and the new SQL path (fetch_anomaly_groups), and asserts
that their outputs are IDENTICAL in every observable field.

Fixture design
--------------
Three calendar days, five anomaly rows, three incidents with distinct
fault_type labels.  Sensor values are chosen so that:
- Rows matched to an incident take the incident's fault_type + affected_sensors.
- Rows without a nearby incident fall back to sensor-value inference.
- No two rows have the same anomaly_score (avoids sort-stability ambiguity).
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.anomaly_group_service import fetch_anomaly_groups
from src.api.services.anomaly_insights import enrich_anomaly_rows, group_anomaly_rows
from src.api.services.readings_service import fetch_flagged_window, fetch_incidents
from src.config import ANOMALY_GROUP_SAMPLE_IDS
from src.db.models import Base, Incident, SensorReading

# ─── fixture constants ────────────────────────────────────────────────────────

TENANT = "parity_tenant"
GREENHOUSE = "parity_gh"
THRESHOLD = 0.5

# 2026-09-01 00:00:00 UTC
BASE_DAY = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)

# Anomaly rows: (day_offset, hour, score, temperature, humidity, co2, light)
#   Day 1 hour  2: no incident, temperature=35.0 (>30 TEMP range) → spike_fault
#   Day 1 hour 10: matched to Inc1 at same hour  → spike_fault / temperature
#   Day 2 hour  6: matched to Inc2               → calibration_drift / humidity+co2
#   Day 3 hour  5: no incident, co2=600 (>500)   → spike_fault / co2
#   Day 3 hour 14: matched to Inc3               → stuck_fault / light
ANOMALY_ROWS: list[tuple[int, int, float, float, float, float, float]] = [
    (0,  2, 0.60, 35.0,  60.0, 400.0,  500.0),  # Day1 h2:  temp out-of-range
    (0, 10, 0.95, 22.0,  60.0, 400.0,  500.0),  # Day1 h10: matches Inc1
    (1,  6, 0.85, 22.0,  60.0, 400.0,  500.0),  # Day2 h6:  matches Inc2
    (2,  5, 0.70, 22.0,  60.0, 600.0,  500.0),  # Day3 h5:  co2 out-of-range
    (2, 14, 0.90, 22.0,  60.0, 400.0,  500.0),  # Day3 h14: matches Inc3
]

# Normal rows below threshold (should not appear in any group).
NORMAL_HOURS: list[tuple[int, int]] = [(0, 0), (1, 0), (2, 0), (2, 20)]


def _ts(day_offset: int, hour: int) -> datetime.datetime:
    return BASE_DAY + datetime.timedelta(days=day_offset, hours=hour)


@pytest_asyncio.fixture
async def parity_db():
    """In-memory SQLite database seeded with the parity fixture."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        # Insert anomaly rows.
        for day_off, hour, score, temp, hum, co2, light in ANOMALY_ROWS:
            session.add(
                SensorReading(
                    tenant_id=TENANT,
                    greenhouse_id=GREENHOUSE,
                    timestamp=_ts(day_off, hour),
                    temperature=temp,
                    humidity=hum,
                    co2=co2,
                    light=light,
                    anomaly_score=score,
                )
            )
        # Insert normal rows.
        for day_off, hour in NORMAL_HOURS:
            session.add(
                SensorReading(
                    tenant_id=TENANT,
                    greenhouse_id=GREENHOUSE,
                    timestamp=_ts(day_off, hour),
                    temperature=22.0,
                    humidity=60.0,
                    co2=400.0,
                    light=500.0,
                    anomaly_score=0.1,
                )
            )
        # Three incidents with distinct fault types and sensors.
        # Inc1: Day1 hour 10 — spike_fault, temperature
        session.add(
            Incident(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                detected_at=_ts(0, 10),
                anomaly_score=0.95,
                fault_type="spike_fault",
                root_cause="heating_failure",
                root_cause_details={"affected_sensors": ["temperature"]},
                sequence=[[22.0, 60.0, 400.0, 500.0]] * 20,
                sequence_length=20,
                embedding=[0.0] * 8,
                embedding_uncertainty=0.1,
            )
        )
        # Inc2: Day2 hour 6 — calibration_drift, humidity + co2
        session.add(
            Incident(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                detected_at=_ts(1, 6),
                anomaly_score=0.85,
                fault_type="calibration_drift",
                root_cause="sensor_drift",
                root_cause_details={"affected_sensors": ["humidity", "co2"]},
                sequence=[[22.0, 60.0, 400.0, 500.0]] * 20,
                sequence_length=20,
                embedding=[0.0] * 8,
                embedding_uncertainty=0.1,
            )
        )
        # Inc3: Day3 hour 14 — stuck_fault, light
        session.add(
            Incident(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                detected_at=_ts(2, 14),
                anomaly_score=0.90,
                fault_type="stuck_fault",
                root_cause="lighting_stuck",
                root_cause_details={"affected_sensors": ["light"]},
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

_START = _ts(0, 0)
_END = _ts(3, 23)

_QUERY = {
    "tenant_id": TENANT,
    "greenhouse_id": GREENHOUSE,
    "start": _START,
    "end": _END,
    "threshold": THRESHOLD,
}


async def _old_path_groups(
    db: AsyncSession, group_by: str
) -> list[dict[str, Any]]:
    """Reference implementation: full-row fetch → enrich all → group."""
    flagged = await fetch_flagged_window(db, **_QUERY)
    incidents = await fetch_incidents(
        db,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        start=_START,
        end=_END,
    )
    enriched = enrich_anomaly_rows(flagged, incidents)
    return group_anomaly_rows(enriched, group_by, sample_limit=ANOMALY_GROUP_SAMPLE_IDS)


async def _new_path_groups(
    db: AsyncSession, group_by: str
) -> list[dict[str, Any]]:
    """Optimised implementation: SQL aggregation + binary-search incident join."""
    incidents = await fetch_incidents(
        db,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        start=_START,
        end=_END,
    )
    return await fetch_anomaly_groups(
        db,
        **_QUERY,
        group_by=group_by,  # type: ignore[arg-type]
        sample_limit=ANOMALY_GROUP_SAMPLE_IDS,
        incidents=incidents,
    )


def _normalise_group(group: dict[str, Any]) -> dict[str, Any]:
    """Normalise fields that might have trivial ordering differences."""
    return {
        "key": group["key"],
        "count": group["count"],
        "top_score": pytest.approx(group["top_score"], abs=1e-9),
        # sample_ids and samples are already sorted by timestamp DESC.
        "sample_ids": group["sample_ids"],
        "sample_timestamps": [s["timestamp"] for s in group.get("samples", [])],
        "sample_scores": [
            pytest.approx(s["anomaly_score"], abs=1e-9)
            for s in group.get("samples", [])
        ],
        # Sort affected_sensors so list-order differences don't fail equality.
        "sample_sensors": [
            sorted(s.get("affected_sensors") or []) for s in group.get("samples", [])
        ],
    }


def _groups_to_map(groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Index groups by key for order-independent comparison."""
    return {g["key"]: _normalise_group(g) for g in groups}


# ─── parity tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestGroupByDateParity:
    async def test_group_keys_identical(self, parity_db):
        old = await _old_path_groups(parity_db, "date")
        new = await _new_path_groups(parity_db, "date")
        assert {g["key"] for g in old} == {g["key"] for g in new}

    async def test_counts_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "date"))
        new = _groups_to_map(await _new_path_groups(parity_db, "date"))
        for key in old:
            assert old[key]["count"] == new[key]["count"], f"count mismatch for day {key}"

    async def test_top_scores_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "date"))
        new = _groups_to_map(await _new_path_groups(parity_db, "date"))
        for key in old:
            assert old[key]["top_score"] == new[key]["top_score"], (
                f"top_score mismatch for day {key}"
            )

    async def test_sample_ids_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "date"))
        new = _groups_to_map(await _new_path_groups(parity_db, "date"))
        for key in old:
            assert old[key]["sample_ids"] == new[key]["sample_ids"], (
                f"sample_ids mismatch for day {key}"
            )

    async def test_group_order_newest_first(self, parity_db):
        new = await _new_path_groups(parity_db, "date")
        keys = [g["key"] for g in new]
        assert keys == sorted(keys, reverse=True)

    async def test_day1_has_two_anomaly_rows(self, parity_db):
        new = _groups_to_map(await _new_path_groups(parity_db, "date"))
        day1_key = _START.date().isoformat()  # "2026-09-01"
        assert new[day1_key]["count"] == 2

    async def test_day3_has_two_anomaly_rows(self, parity_db):
        new = _groups_to_map(await _new_path_groups(parity_db, "date"))
        day3_key = (_START + datetime.timedelta(days=2)).date().isoformat()
        assert new[day3_key]["count"] == 2


@pytest.mark.asyncio
class TestGroupByTypeParity:
    async def test_group_keys_identical(self, parity_db):
        old = await _old_path_groups(parity_db, "type")
        new = await _new_path_groups(parity_db, "type")
        assert {g["key"] for g in old} == {g["key"] for g in new}

    async def test_counts_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "type"))
        new = _groups_to_map(await _new_path_groups(parity_db, "type"))
        for key in old:
            assert old[key]["count"] == new[key]["count"], (
                f"count mismatch for fault_type={key!r}"
            )

    async def test_top_scores_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "type"))
        new = _groups_to_map(await _new_path_groups(parity_db, "type"))
        for key in old:
            assert old[key]["top_score"] == new[key]["top_score"], (
                f"top_score mismatch for fault_type={key!r}"
            )

    async def test_sample_ids_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "type"))
        new = _groups_to_map(await _new_path_groups(parity_db, "type"))
        for key in old:
            assert old[key]["sample_ids"] == new[key]["sample_ids"], (
                f"sample_ids mismatch for fault_type={key!r}"
            )

    async def test_known_fault_types_present(self, parity_db):
        """Incident fault types must appear in the type grouping."""
        new = _groups_to_map(await _new_path_groups(parity_db, "type"))
        # Rows matched to incidents carry the incident's fault_type.
        assert "spike_fault" in new, "Expected spike_fault group"
        assert "calibration_drift" in new, "Expected calibration_drift group"
        assert "stuck_fault" in new, "Expected stuck_fault group"

    async def test_total_count_equals_anomaly_row_count(self, parity_db):
        """Sum of all group counts must equal total anomaly rows."""
        new = await _new_path_groups(parity_db, "type")
        assert sum(g["count"] for g in new) == len(ANOMALY_ROWS)


@pytest.mark.asyncio
class TestGroupBySensorParity:
    async def test_group_keys_identical(self, parity_db):
        old = await _old_path_groups(parity_db, "sensor")
        new = await _new_path_groups(parity_db, "sensor")
        assert {g["key"] for g in old} == {g["key"] for g in new}

    async def test_counts_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "sensor"))
        new = _groups_to_map(await _new_path_groups(parity_db, "sensor"))
        for key in old:
            assert old[key]["count"] == new[key]["count"], (
                f"count mismatch for sensor={key!r}"
            )

    async def test_top_scores_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "sensor"))
        new = _groups_to_map(await _new_path_groups(parity_db, "sensor"))
        for key in old:
            assert old[key]["top_score"] == new[key]["top_score"], (
                f"top_score mismatch for sensor={key!r}"
            )

    async def test_sample_ids_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "sensor"))
        new = _groups_to_map(await _new_path_groups(parity_db, "sensor"))
        for key in old:
            assert old[key]["sample_ids"] == new[key]["sample_ids"], (
                f"sample_ids mismatch for sensor={key!r}"
            )

    async def test_row_with_two_sensors_counted_in_both_buckets(self, parity_db):
        """Day2 row matched to Inc2 (affected_sensors=humidity+co2) must appear
        in both the humidity bucket and the co2 bucket."""
        new = _groups_to_map(await _new_path_groups(parity_db, "sensor"))
        # Inc2 affected_sensors = ["humidity", "co2"]
        assert "humidity" in new, "Expected humidity sensor group"
        assert "co2" in new, "Expected co2 sensor group"
        # Day2 row (score=0.85) must appear in both.
        # SQLite strips timezone on read-back → timestamps appear without +00:00.
        day2_ts_naive = _ts(1, 6).replace(tzinfo=None).isoformat()
        humidity_ids = new["humidity"]["sample_ids"]
        co2_ids = new["co2"]["sample_ids"]
        # Match on the date/time prefix (ignore trailing tz suffix if present).
        assert any(s.startswith(day2_ts_naive) for s in humidity_ids), (
            f"Day2 row must be in humidity group sample_ids, got: {humidity_ids}"
        )
        assert any(s.startswith(day2_ts_naive) for s in co2_ids), (
            f"Day2 row must be in co2 group sample_ids, got: {co2_ids}"
        )

    async def test_sample_affected_sensors_identical(self, parity_db):
        old = _groups_to_map(await _old_path_groups(parity_db, "sensor"))
        new = _groups_to_map(await _new_path_groups(parity_db, "sensor"))
        for key in old:
            assert old[key]["sample_sensors"] == new[key]["sample_sensors"], (
                f"sample affected_sensors mismatch for sensor={key!r}"
            )
