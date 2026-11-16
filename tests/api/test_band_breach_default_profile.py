"""Pin: a default tomato band breach is not an anomaly.

A greenhouse with no saved operational overrides is on the tomato profile.
Breaching that profile stays on ``scada_anomalies`` and does not set
``is_anomaly``. Promotion requires a non-empty raw override.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.comprehensive_pipeline import ComprehensiveInferencePipeline
from src.config import CROP_SETPOINT_PROFILES, SEQ_LEN
from src.db.models import Base
from src.models.data_utils import SENSOR_RANGES, normalize_data
from src.models.inference import AnomalyDetector
from src.models.operational_config_store import get_operational_config_store

_CHECKPOINT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "epoch_0072_trained_baseline.pt"
)
_TENANT = "band-breach-default-tomato"
_GREENHOUSE = "default"
_NOON = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


def _breach_window() -> np.ndarray:
    """Daytime window held above the configured tomato temperature maximum.

    Temperature and humidity trends have opposite signs so the physics
    diurnal-suppression rule does not return before the band finding is
    attached. The swing is wide enough that the window is not a flatline.
    """
    day_hi = float(CROP_SETPOINT_PROFILES["tomato"]["temperature"]["day"][1])
    step = np.arange(SEQ_LEN, dtype=float)
    temperature = (day_hi + 1.4) + 0.2 * np.sin(2.0 * np.pi * step / SEQ_LEN)
    humidity = 70.0 - 0.5 * np.sin(2.0 * np.pi * step / SEQ_LEN)
    co2 = 420.0 + 3.0 * np.sin(2.0 * np.pi * step / SEQ_LEN)
    light = 480.0 + 12.0 * np.sin(2.0 * np.pi * step / 8.0)
    return np.column_stack([temperature, humidity, co2, light])


async def _score_default_profile() -> dict:
    store = get_operational_config_store()
    store.reset(_TENANT, _GREENHOUSE)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        assert store.get_raw_overrides(_TENANT, _GREENHOUSE) == {}
        detector = AnomalyDetector(checkpoint_path=str(_CHECKPOINT))
        pipeline = ComprehensiveInferencePipeline(detector=detector)
        normalized = normalize_data(_breach_window(), SENSOR_RANGES)
        async with factory() as session:
            result = await pipeline.run(
                customer_id=_TENANT,
                sequence=normalized.tolist(),
                db=session,
                greenhouse_id=_GREENHOUSE,
                sequence_timestamp=_NOON.isoformat(),
            )
        scada = result.get("scada_anomalies") or []
        return {
            "is_anomaly": bool(result["is_anomaly"]),
            "fault_type": result.get("fault_type"),
            "anomaly_score": result.get("anomaly_score"),
            "physics_validation": result.get("physics_validation"),
            "raw_overrides": store.get_raw_overrides(_TENANT, _GREENHOUSE),
            "scada_types": [item.get("anomaly_type") for item in scada],
        }
    finally:
        store.reset(_TENANT, _GREENHOUSE)
        await engine.dispose()


def test_default_tomato_band_breach_does_not_raise_anomaly() -> None:
    """Empty get_raw_overrides: TEMP_BAND_HIGH is visible and is_anomaly stays false."""
    report = asyncio.run(_score_default_profile())
    message = json.dumps(report, indent=2, default=str)
    assert report["raw_overrides"] == {}, message
    assert "TEMP_BAND_HIGH" in report["scada_types"], message
    assert report["is_anomaly"] is False, message
    assert report["fault_type"] is None, message
