"""
Unit tests for the SQLAlchemy ORM models.

Verifies that the SensorReading model is correctly configured, indexes are present,
and that we can successfully create tables, insert, and query records using an in-memory database.
"""

from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, SensorReading


@pytest.fixture
def db_session():
    """Create an empty in-memory SQLite database and start a session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def test_base_class_setup():
    """Verify that Base is the declarative base class for all models."""
    assert issubclass(SensorReading, Base)


def test_sensor_reading_table_structure():
    """Verify table name and required columns/indexes on the sensor_readings table."""
    engine = create_engine("sqlite:///:memory:")
    inspector = inspect(engine)
    
    # Assert table exists after creation
    Base.metadata.create_all(engine)
    tables = inspector.get_table_names()
    assert "sensor_readings" in tables

    # Verify column structures
    columns = {col["name"]: col for col in inspector.get_columns("sensor_readings")}
    
    required_fields = [
        "id", "tenant_id", "greenhouse_id", "timestamp", "temperature",
        "humidity", "co2", "light", "anomaly_score", "created_at"
    ]
    for field in required_fields:
        assert field in columns, f"Column '{field}' is missing from sensor_readings table."

    # Verify column nullability and types (where SQLite reports standard mappings)
    assert columns["id"]["primary_key"] == 1
    assert not columns["tenant_id"]["nullable"]
    assert not columns["greenhouse_id"]["nullable"]
    assert not columns["timestamp"]["nullable"]
    assert not columns["temperature"]["nullable"]
    assert not columns["humidity"]["nullable"]
    assert not columns["co2"]["nullable"]
    assert not columns["light"]["nullable"]
    assert columns["anomaly_score"]["nullable"]
    assert not columns["created_at"]["nullable"]

    # Verify indexes exist for tenant_id and timestamp
    indexes = {idx["name"]: idx for idx in inspector.get_indexes("sensor_readings")}
    # SQLite indexes usually auto-named or user-named
    index_cols = [col for idx in indexes.values() for col in idx["column_names"]]
    assert "tenant_id" in index_cols, "Index missing on tenant_id column."
    assert "timestamp" in index_cols, "Index missing on timestamp column."


def test_sensor_reading_create_and_query(db_session):
    """Test standard instantiation, persistence, and querying of a SensorReading."""
    now = datetime.now(timezone.utc)
    reading = SensorReading(
        tenant_id="tenant-123",
        greenhouse_id="greenhouse-abc",
        timestamp=now,
        temperature=24.5,
        humidity=62.3,
        co2=450.0,
        light=800.0,
        anomaly_score=0.12
    )

    db_session.add(reading)
    db_session.commit()

    # Query back
    retrieved = db_session.query(SensorReading).filter_by(tenant_id="tenant-123").first()
    assert retrieved is not None
    assert retrieved.id == 1
    assert retrieved.tenant_id == "tenant-123"
    assert retrieved.greenhouse_id == "greenhouse-abc"
    # Note: SQLite date roundtrip may lose tz offset representation depending on config, but the time matches.
    assert retrieved.temperature == 24.5
    assert retrieved.humidity == 62.3
    assert retrieved.co2 == 450.0
    assert retrieved.light == 800.0
    assert retrieved.anomaly_score == 0.12
    
    # Assert created_at auto-populated
    assert retrieved.created_at is not None
    assert isinstance(retrieved.created_at, datetime)


def test_sensor_reading_optional_anomaly_score(db_session):
    """Verify that is acceptable to have an empty/None anomaly_score."""
    now = datetime.now(timezone.utc)
    reading = SensorReading(
        tenant_id="tenant-123",
        greenhouse_id="greenhouse-abc",
        timestamp=now,
        temperature=22.0,
        humidity=55.0,
        co2=400.0,
        light=600.0,
        anomaly_score=None  # Explicitly None
    )

    db_session.add(reading)
    db_session.commit()

    retrieved = db_session.query(SensorReading).filter_by(tenant_id="tenant-123").first()
    assert retrieved is not None
    assert retrieved.anomaly_score is None


def test_sensor_reading_repr():
    """Verify the developer-friendly string representation of SensorReading."""
    reading = SensorReading(
        id=42,
        tenant_id="test_tenant",
        greenhouse_id="greenhouse_1",
        timestamp=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        temperature=23.5,
        humidity=60.0,
        co2=420.0,
        light=500.0,
        anomaly_score=0.01
    )
    r_str = repr(reading)
    assert "id=42" in r_str
    assert "tenant_id='test_tenant'" in r_str
    assert "greenhouse_id='greenhouse_1'" in r_str
    assert "temperature=23.5" in r_str
    assert "humidity=60.0" in r_str
    assert "co2=420.0" in r_str
    assert "light=500.0" in r_str
    assert "anomaly_score=0.01" in r_str
