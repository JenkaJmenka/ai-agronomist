import re

with open("src/api/services/csv_persist.py", "r") as f:
    content = f.read()

old_load_sensor = """async def _load_sensor_by_key(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    timestamps: list[datetime],
) -> dict[str, SensorReading]:
    if not timestamps:
        return {}
    norm_ts = [normalize_persist_timestamp(ts) for ts in timestamps]
    stmt = select(SensorReading).where(
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.timestamp >= min(norm_ts),
        SensorReading.timestamp <= max(norm_ts),
    )"""

new_load_sensor = """async def _load_sensor_by_key(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    timestamps: list[datetime],
) -> dict[str, SensorReading]:
    if not timestamps:
        return {}
    norm_ts = list(set([normalize_persist_timestamp(ts) for ts in timestamps]))
    stmt = select(SensorReading).where(
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.timestamp.in_(norm_ts),
    )"""

content = content.replace(old_load_sensor, new_load_sensor)

old_load_raw = """async def _load_raw_by_key(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    timestamps: list[datetime],
) -> dict[str, RawReading]:
    if not timestamps:
        return {}
    norm_ts = [normalize_persist_timestamp(ts) for ts in timestamps]
    stmt = select(RawReading).where(
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
        RawReading.timestamp >= min(norm_ts),
        RawReading.timestamp <= max(norm_ts),
    )"""

new_load_raw = """async def _load_raw_by_key(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    timestamps: list[datetime],
) -> dict[str, RawReading]:
    if not timestamps:
        return {}
    norm_ts = list(set([normalize_persist_timestamp(ts) for ts in timestamps]))
    stmt = select(RawReading).where(
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
        RawReading.timestamp.in_(norm_ts),
    )"""

content = content.replace(old_load_raw, new_load_raw)

with open("src/api/services/csv_persist.py", "w") as f:
    f.write(content)

print("Updated csv_persist.py")
