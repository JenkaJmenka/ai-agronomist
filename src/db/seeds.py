import math
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SensorReading

_TENANTS = ["tenant_1", "tenant_2", "tenant_3"]
_GREENHOUSES = ["gh_A", "gh_B", "gh_C"]

# 14 days, reading every 30 minutes
_DAYS = 14
_INTERVAL_MINUTES = 30
_READINGS_PER_COMBO = _DAYS * 24 * (60 // _INTERVAL_MINUTES)  # 672


def _daily_cycle(hour: float, lo: float, hi: float, peak_hour: float = 14.0) -> float:
    """Smooth daily sinusoidal cycle: value peaks at peak_hour, troughs 12h later."""
    phase = (hour - peak_hour) / 24.0 * 2 * math.pi
    mid = (lo + hi) / 2.0
    amp = (hi - lo) / 2.0
    return mid - amp * math.cos(phase)


def _realistic_readings(ts: datetime, anomaly: bool = False) -> dict:
    hour = ts.hour + ts.minute / 60.0
    noise = lambda scale: random.gauss(0, scale)

    if anomaly:
        return {
            "temperature": random.uniform(38.0, 45.0),
            "humidity": random.uniform(8.0, 18.0),
            "co2": random.uniform(80.0, 150.0),
            "light": random.uniform(0.0, 40.0),
            "anomaly_score": random.uniform(0.82, 0.99),
        }

    # Light: zero at night (20:00–06:00), peak around noon
    light_base = max(0.0, _daily_cycle(hour, 0.0, 950.0, peak_hour=12.0))
    if hour < 6.0 or hour >= 20.0:
        light_base = 0.0

    return {
        "temperature": max(18.0, _daily_cycle(hour, 19.0, 28.0, 15.0) + noise(0.4)),
        "humidity": max(40.0, _daily_cycle(hour, 55.0, 72.0, 4.0) + noise(1.0)),
        "co2": max(280.0, _daily_cycle(hour, 380.0, 520.0, 3.0) + noise(8.0)),
        "light": max(0.0, light_base + noise(15.0)),
        "anomaly_score": random.uniform(0.02, 0.28),
    }


async def seed_test_data(session: AsyncSession, num_samples: int = _READINGS_PER_COMBO) -> None:
    """Seed the database with 14 days of realistic sensor readings per tenant/greenhouse."""
    base_time = datetime.now(timezone.utc) - timedelta(days=_DAYS)
    records = []

    for tenant in _TENANTS:
        for gh in _GREENHOUSES:
            # Inject ~3% anomalies at random positions
            anomaly_indices = set(
                random.sample(range(num_samples), max(1, num_samples // 30))
            )
            for i in range(num_samples):
                ts = base_time + timedelta(minutes=i * _INTERVAL_MINUTES)
                is_anomaly = i in anomaly_indices
                vals = _realistic_readings(ts, anomaly=is_anomaly)
                records.append(
                    SensorReading(
                        tenant_id=tenant,
                        greenhouse_id=gh,
                        timestamp=ts,
                        temperature=vals["temperature"],
                        humidity=vals["humidity"],
                        co2=vals["co2"],
                        light=vals["light"],
                        anomaly_score=vals["anomaly_score"],
                    )
                )

    session.add_all(records)
    await session.commit()
    print(
        f"Seeded {len(records)} readings "
        f"({len(_TENANTS)} tenants × {len(_GREENHOUSES)} greenhouses × {num_samples} readings)"
    )
