import os
import json
import pytest
from unittest.mock import MagicMock, AsyncMock
from src.api.services import orchestrator_registry
from src.api.services.orchestrator_registry import (
    _orchestrators,
    get_orchestrator,
    clear_orchestrator_cache,
)
from src.models.retraining_orchestrator import RetrainingOrchestrator
from src.utils.lru import LRUDict


@pytest.mark.asyncio
async def test_orchestrator_cache_eviction_and_rehydration(tmp_path, monkeypatch):
    # Redirect checkpoint writes to a per-test temp directory so parallel
    # xdist workers never collide on the same filesystem path.
    ckpt_dir = str(tmp_path / "orchestrator_checkpoints")
    monkeypatch.setattr(orchestrator_registry, "CHECKPOINT_DIR", ckpt_dir)

    # 1. Start fresh
    clear_orchestrator_cache()

    # Ensure cache is LRUDict with max_size=10
    assert isinstance(_orchestrators, LRUDict)
    assert _orchestrators.max_size == 10

    # 2. Add 10 orchestrators to fill the cache
    orchestrators = []
    for i in range(10):
        cust_id = f"cust_{i}"
        orch = RetrainingOrchestrator(customer_id=cust_id)
        # Mock export_state to return a custom payload
        orch.export_state = MagicMock(return_value={"my_dummy_state": f"value_{i}"})
        _orchestrators[cust_id] = orch
        orchestrators.append(orch)

    # 3. Verify they are all in the cache
    assert len(_orchestrators) == 10
    assert "cust_0" in _orchestrators

    # 4. Add the 11th orchestrator to trigger eviction of "cust_0"
    orch_11 = RetrainingOrchestrator(customer_id="cust_10")
    orch_11.export_state = MagicMock(return_value={"my_dummy_state": "value_10"})
    _orchestrators["cust_10"] = orch_11

    # 5. Verify "cust_0" was evicted
    assert "cust_0" not in _orchestrators
    assert len(_orchestrators) == 10

    # 6. Verify checkpoint file exists for "cust_0" and contains correct data
    checkpoint_file = os.path.join(ckpt_dir, "cust_0.json")
    assert os.path.exists(checkpoint_file)
    with open(checkpoint_file, "r") as f:
        saved_data = json.load(f)
    assert saved_data == {"my_dummy_state": "value_0"}

    # 7. Get "cust_0" again. It should read from the checkpoint file, rehydrate, delete it, and put it in cache.
    mock_db = AsyncMock()

    load_state_calls = []
    original_load_state = RetrainingOrchestrator.load_state
    def mock_load_state(self, state):
        load_state_calls.append((self.customer_id, state))
        original_load_state(self, state)

    # Temporary monkeypatch
    monkeypatch.setattr(RetrainingOrchestrator, "load_state", mock_load_state)
    restored_orch = await get_orchestrator("cust_0", mock_db)

    # Verify restoration
    assert restored_orch.customer_id == "cust_0"
    assert len(load_state_calls) == 1
    assert load_state_calls[0][1] == {"my_dummy_state": "value_0"}

    # 8. Check that checkpoint file is deleted
    assert not os.path.exists(checkpoint_file)

    # 9. Verify "cust_0" is back in cache
    assert "cust_0" in _orchestrators
    assert _orchestrators["cust_0"] is restored_orch
