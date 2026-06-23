import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.db.models import Base, SensorReading
from src.models.training_pipeline import PerCustomerTrainingPipeline
from src.models.model_registry import ModelRegistry
import datetime
import numpy as np

import os
DB_PATH = "sqlite+aiosqlite:///./test_pipeline.db"
if os.path.exists("./test_pipeline.db"):
    os.remove("./test_pipeline.db")
engine = create_async_engine(DB_PATH, echo=False)
TestingSessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=AsyncSession)

@pytest_asyncio.fixture(autouse=True)
async def test_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    async with TestingSessionLocal() as db:
        yield db
        
    async with TestingSessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            await db.execute(table.delete())
        await db.commit()

    # Close pooled aiosqlite connections: their non-daemon worker threads
    # otherwise outlive the test loop and hang interpreter shutdown.
    # Each test re-creates the schema above, so disposal is safe.
    await engine.dispose()

@pytest_asyncio.fixture
async def populated_db(test_db):
    for i in range(150):
        reading = SensorReading(
            tenant_id="pipeline_cust",
            greenhouse_id="gh1",
            timestamp=datetime.datetime.now() - datetime.timedelta(minutes=15*(150-i)),
            temperature=25.0 + i*0.01,
            humidity=60.0,
            co2=400.0,
            light=100.0
        )
        test_db.add(reading)
    await test_db.commit()
    return test_db

@pytest.mark.asyncio
async def test_load_customer_data(populated_db):
    pipeline = PerCustomerTrainingPipeline(populated_db)
    data = await pipeline._load_customer_data("pipeline_cust")
    
    assert len(data) == 150
    assert data.shape[1] == 4

@pytest.mark.asyncio
async def test_train_customer_model_completes(populated_db):
    import glob
    checkpoints = sorted(glob.glob("models/checkpoints/epoch_*.pt"))
    if not checkpoints:
        pytest.skip("No baseline checkpoints available")
        
    pipeline = PerCustomerTrainingPipeline(populated_db)
    
    from src.models.model_evaluator import ModelEvaluator
    original_compare = ModelEvaluator.compare
    def mock_compare(self, base, new):
        return {"improvement_percent": 10.0}
    ModelEvaluator.compare = mock_compare
    
    try:
        model_id = await pipeline.train_customer_model("pipeline_cust")
        
        assert model_id is not None
        assert model_id.startswith("model_")
        
        registry = ModelRegistry(populated_db)
        active = await registry.get_active_model("pipeline_cust")
        assert active is not None
        assert active["model_id"] == model_id
    finally:
        ModelEvaluator.compare = original_compare

@pytest.mark.asyncio
async def test_train_customer_no_improvement(populated_db):
    import glob
    checkpoints = sorted(glob.glob("models/checkpoints/epoch_*.pt"))
    if not checkpoints:
        pytest.skip("No baseline checkpoints available")
        
    pipeline = PerCustomerTrainingPipeline(populated_db)
    
    from src.models.model_evaluator import ModelEvaluator
    original_compare = ModelEvaluator.compare
    def mock_compare(self, base, new):
        return {"improvement_percent": -5.0}
    ModelEvaluator.compare = mock_compare
    
    try:
        registry = ModelRegistry(populated_db)
        await registry.register_model("pipeline_cust", "baseline", "path/base.pt")
        
        model_id = await pipeline.train_customer_model("pipeline_cust")
        assert model_id is None
    finally:
        ModelEvaluator.compare = original_compare
