"""Unit tests for database models."""

import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from src.db.models import Base, SensorReading


def test_sensor_reading_model_creation() -> None:
    """Tests schema generation and record insertion/retrieval with SensorReading."""
    # Setup an in-memory database
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    
    # Verify model table is registered
    assert "sensor_readings" in Base.metadata.tables
    
    with Session(engine) as session:
        # Create unique timestamp
        now = datetime.datetime.now(datetime.timezone.utc)
        
        reading = SensorReading(
            timestamp=now,
            tenant_id="test_tenant_xyz",
            greenhouse_id="temp-sensor-01",
            temperature=25.5,
            humidity=60.1,
            light=500,
            co2=450,
        )
        session.add(reading)
        session.commit()
        
        # Query it back
        saved = session.query(SensorReading).filter_by(
            tenant_id="test_tenant_xyz",
            greenhouse_id="temp-sensor-01"
        ).first()
        
        assert saved is not None
        assert saved.tenant_id == "test_tenant_xyz"
        assert saved.greenhouse_id == "temp-sensor-01"
        assert saved.temperature == 25.5
        assert saved.humidity == 60.1
        assert saved.light == 500
        assert saved.co2 == 450
        
        # Verify __repr__ output
        repr_str = repr(saved)
        assert "test_tenant_xyz" in repr_str
        assert "temp-sensor-01" in repr_str
