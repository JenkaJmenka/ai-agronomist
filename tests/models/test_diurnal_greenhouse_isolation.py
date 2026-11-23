"""Two greenhouses of the same tenant get independent diurnal profiles.

The pipeline caches diurnal profiles keyed by ``(customer_id,
greenhouse_id)``.  Before the fix the cache was keyed by customer_id
alone, so the second greenhouse scored against the first greenhouse's
profile.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services import comprehensive_pipeline as pipeline_module
from src.api.services import diurnal_profile_service
from src.api.services.comprehensive_pipeline import ComprehensiveInferencePipeline
from src.config import DIURNAL_VOTE_MIN_DAYS, SENSOR_NAMES, SEQ_LEN
from src.db.models import Base, SensorReading
from src.models.inference import AnomalyDetector

_CHECKPOINT = "tests/fixtures/epoch_0072_trained_baseline.pt"
_TENANT = "greenhouse-isolation-test"
_GH_A = "greenhouse-a"
_GH_B = "greenhouse-b"
# Seed data lives at this date; pin datetime.now so the trailing window
# includes it.
_BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)
_PIN = _BASE + timedelta(days=9)


class _PinnedDatetime(datetime):
    """datetime whose .now() returns a fixed timestamp."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        if tz is not None and _PIN.tzinfo is None:
            return _PIN.replace(tzinfo=tz)
        return _PIN


def _seed_rows(
    tenant: str,
    greenhouse: str,
    base_temp: float,
    base_hum: float,
    n_days: int = 8,
) -> list[SensorReading]:
    """Generate ``n_days`` of 5-min readings with a fixed diurnal centre."""
    rng = np.random.default_rng(hash((tenant, greenhouse)) & 0xFFFFFFFF)
    rows: list[SensorReading] = []
    for day in range(n_days):
        for step in range(288):
            ts = _BASE + timedelta(days=day, minutes=step * 5)
            hour_frac = ts.hour + ts.minute / 60.0
            temp = base_temp + 3.0 * np.sin(2 * np.pi * (hour_frac - 6) / 24)
            temp += float(rng.normal(0, 0.3))
            hum = base_hum - 5.0 * np.sin(2 * np.pi * (hour_frac - 6) / 24)
            hum += float(rng.normal(0, 0.5))
            co2 = 400.0 + float(rng.normal(0, 3))
            light = max(0.0, 500.0 * np.sin(np.pi * max(0, hour_frac - 6) / 12)
                        if 6 <= hour_frac <= 18 else 0)
            rows.append(SensorReading(
                tenant_id=tenant,
                greenhouse_id=greenhouse,
                timestamp=ts,
                temperature=float(temp),
                humidity=float(hum),
                co2=float(co2),
                light=float(light),
                source=None,
            ))
    return rows


@pytest.mark.asyncio
async def test_two_greenhouses_get_separate_profiles() -> None:
    """Each greenhouse builds its own profile; cache keys do not collide."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False,
    )

    # Greenhouse A: warm (base temp 28 C, hum 60 %)
    # Greenhouse B: cool (base temp 18 C, hum 80 %)
    rows_a = _seed_rows(_TENANT, _GH_A, base_temp=28.0, base_hum=60.0)
    rows_b = _seed_rows(_TENANT, _GH_B, base_temp=18.0, base_hum=80.0)

    async with factory() as session:
        session.add_all(rows_a + rows_b)
        await session.commit()

    # 8 days of seed data exceeds the 7-day vote minimum.
    assert 8 >= DIURNAL_VOTE_MIN_DAYS
    assert len(rows_a) > 0 and len(rows_b) > 0

    detector = AnomalyDetector(checkpoint_path=_CHECKPOINT)
    pipeline = ComprehensiveInferencePipeline(detector=detector)

    # Pin datetime.now in both the pipeline and the profile service so the
    # trailing history window covers the seed data (July 2026).
    original_pipeline = pipeline_module.datetime
    original_diurnal = diurnal_profile_service.datetime
    pipeline_module.datetime = _PinnedDatetime
    diurnal_profile_service.datetime = _PinnedDatetime
    try:
        # Build profiles for greenhouse A
        async with factory() as session:
            profiles_a: Dict[str, Dict[int, tuple]] = (
                await pipeline._ensure_diurnal_profiles(_TENANT, _GH_A, session)
            )

        # Build profiles for greenhouse B
        async with factory() as session:
            profiles_b: Dict[str, Dict[int, tuple]] = (
                await pipeline._ensure_diurnal_profiles(_TENANT, _GH_B, session)
            )
    finally:
        pipeline_module.datetime = original_pipeline
        diurnal_profile_service.datetime = original_diurnal

    # Both must exist
    assert profiles_a, "greenhouse A profiles should build"
    assert profiles_b, "greenhouse B profiles should build"

    # They must differ: greenhouse A has base temp ~28, B has ~18
    assert "temperature" in profiles_a
    assert "temperature" in profiles_b

    # Pick a midday hour where both have stats
    hour = 12
    mean_a = profiles_a["temperature"][hour][0]
    mean_b = profiles_b["temperature"][hour][0]

    # The centres should reflect the 10 C gap (allow some noise)
    assert mean_a > mean_b + 5.0, (
        f"greenhouse A mean ({mean_a:.1f}) should be well above "
        f"greenhouse B mean ({mean_b:.1f})"
    )

    # Verify cache keys are distinct (key is (tenant, greenhouse, anchor_date))
    assert any(k[0] == _TENANT and k[1] == _GH_A for k in pipeline._diurnal_profiles_cache), (
        f"Expected key for {_GH_A!r} in diurnal cache, got {list(pipeline._diurnal_profiles_cache)}"
    )
    assert any(k[0] == _TENANT and k[1] == _GH_B for k in pipeline._diurnal_profiles_cache), (
        f"Expected key for {_GH_B!r} in diurnal cache, got {list(pipeline._diurnal_profiles_cache)}"
    )
