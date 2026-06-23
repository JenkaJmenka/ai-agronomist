import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from src.api.main import app
from src.db.session import AsyncSessionLocal, engine
from src.db.models import Base
from src.models.model_registry import ModelRegistry

@pytest.fixture
def client():
    # We use a new TestClient per test to trigger lifespan and get clean state
    with TestClient(app) as test_client:
        yield test_client

@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture()
async def test_db():
    async with AsyncSessionLocal() as db:
        yield db

@pytest.mark.asyncio
async def test_model_registration(test_db):
    registry = ModelRegistry(test_db)
    model_id = await registry.register_model("test_cust", "transfer_learned", "path/to/model.pt", metrics={"mse": 0.05})
    
    assert model_id is not None
    active = await registry.get_active_model("test_cust")
    assert active is not None
    assert active["model_id"] == model_id
    assert active["checkpoint_path"] == "path/to/model.pt"

@pytest.mark.asyncio
async def test_get_active_model(test_db, client):
    registry = ModelRegistry(test_db)
    await registry.register_model("test_cust", "transfer_learned", "path/to/model.pt")
    
    response = client.get("/api/models/test_cust")
    assert response.status_code == 200
    assert response.json()["customer_id"] == "test_cust"
    assert response.json()["checkpoint_path"] == "path/to/model.pt"

@pytest.mark.asyncio
async def test_list_model_versions(test_db, client):
    registry = ModelRegistry(test_db)
    await registry.register_model("test_cust", "transfer_learned", "path/v1.pt")
    await registry.register_model("test_cust", "transfer_learned", "path/v2.pt")
    
    response = client.get("/api/models/test_cust/versions")
    assert response.status_code == 200
    versions = response.json()
    assert len(versions) == 2
    
    active_count = sum(1 for v in versions if v["is_active"])
    assert active_count == 1

def test_train_customer_model_endpoint(client):
    response = client.post("/api/models/test_cust/train")
    assert response.status_code == 200
    assert response.json()["status"] == "started"

@pytest.mark.asyncio
async def test_rollback_model(test_db, client):
    registry = ModelRegistry(test_db)
    m1 = await registry.register_model("test_cust", "transfer_learned", "path/v1.pt")
    m2 = await registry.register_model("test_cust", "transfer_learned", "path/v2.pt")
    
    active = await registry.get_active_model("test_cust")
    assert active["model_id"] == m2
    
    response = client.post(f"/api/models/test_cust/rollback", json={"model_id": m1})
    assert response.status_code == 200
    
    active_now = await registry.get_active_model("test_cust")
    assert active_now["model_id"] == m1
