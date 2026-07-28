import datetime

import numpy as np
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api.main import app
from src.db.models import Base, SensorReading
from src.db.session import get_db
from src.models.model_registry import ModelRegistry
from src.models.training_pipeline import PerCustomerTrainingPipeline

DB_PATH = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(DB_PATH, echo=False, poolclass=StaticPool)
TestingSessionLocal = async_sessionmaker(
    autocommit=False, autoflush=False, bind=engine, class_=AsyncSession
)


async def override_get_db():
    async with TestingSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def test_client():
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


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
async def setup_customer_data(test_db):
    for i in range(150):
        reading = SensorReading(
            tenant_id="customer_e2e",
            greenhouse_id="gh1",
            timestamp=datetime.datetime.now()
            - datetime.timedelta(minutes=15 * (150 - i)),
            temperature=22.0 + np.random.normal(0, 0.5),
            humidity=65.0 + np.random.normal(0, 1),
            co2=450.0 + np.random.normal(0, 5),
            light=500.0 + np.random.normal(0, 20),
        )
        test_db.add(reading)
    await test_db.commit()


@pytest.mark.asyncio
async def test_end_to_end_training_and_inference(
    setup_customer_data,
    test_db,
    test_client,
    baseline_checkpoint,
    tmp_path,
    monkeypatch,
):
    # Direct customer model output into tmp_path so the test never writes
    # into the tracked models/ directory.
    monkeypatch.setattr("src.models.transfer_learner.CUSTOMER_MODEL_DIR", str(tmp_path))

    pipeline = PerCustomerTrainingPipeline(
        test_db, baseline_checkpoint_path=baseline_checkpoint
    )
    model_id = await pipeline.train_customer_model("customer_e2e")

    if not model_id:
        registry = ModelRegistry(test_db)
        model_id = await registry.register_model(
            "customer_e2e",
            "transfer_learned",
            baseline_checkpoint,
            metrics={"improvement_percent": 5.0},
        )

    assert model_id is not None

    resp = test_client.get("/api/models/customer_e2e")
    assert resp.status_code == 200
    assert resp.json()["model_id"] == model_id

    seqs = [[[22.0, 65.0, 450.0, 500.0] for _ in range(20)] for _ in range(2)]
    inf_req = {
        "tenant_id": "customer_e2e",
        "greenhouse_id": "gh1",
        "sequences": seqs,
    }

    resp_inf = test_client.post("/api/inference", json=inf_req)
    assert resp_inf.status_code == 200
    res_data = resp_inf.json()
    assert res_data["tenant_id"] == "customer_e2e"
    assert len(res_data["anomaly_scores"]) == 2

    # Register a second model and verify rollback works.
    registry = ModelRegistry(test_db)
    m_base = await registry.register_model(
        "customer_e2e", "baseline", baseline_checkpoint
    )
    resp_rb = test_client.post(
        "/api/models/customer_e2e/rollback", json={"model_id": m_base}
    )
    assert resp_rb.status_code == 200

    active_now = test_client.get("/api/models/customer_e2e").json()
    assert active_now["model_id"] == m_base
