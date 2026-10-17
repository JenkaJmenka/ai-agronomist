"""API integration tests for the online-learning endpoints (Gap 3)."""

from __future__ import annotations

from typing import List

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.api.main import app
from src.api.routes import learning as learning_module
from src.config import LATENT_DIM, MAX_OBSERVE_BATCH, MODEL_CONFIG, SEQ_LEN
from src.db.learning_logs import DRIFT_CHECK_EVENT, LearningLogStore
from src.db.models import Base
from src.db.session import get_db
from src.models.drift_detector import DriftDetector
from src.models.lstm_vae import LSTMVAE

pytestmark = pytest.mark.api


def sample_sequence() -> List[List[float]]:
    """Valid sensor sequence [SEQ_LEN, INPUT_DIM] (normalized-ish values)."""
    row = [0.5, 0.6, 0.4, 0.5]
    return [row[:] for _ in range(SEQ_LEN)]


def observe_payload(tenant_id: str = "tenant_a", n_sequences: int = 3, **overrides) -> dict:
    payload = {
        "tenant_id": tenant_id,
        "greenhouse_id": "gh_1",
        "sequences": [sample_sequence() for _ in range(n_sequences)],
    }
    payload.update(overrides)
    return payload


@pytest_asyncio.fixture
async def learning_db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def learning_client(
    learning_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """
    TestClient with the in-memory DB injected, the base-model loader patched
    to a fresh LSTMVAE (no checkpoint on disk needed), and the module-level
    manager/drift-detector singletons reset between tests.
    """
    monkeypatch.setattr(
        learning_module, "_load_base_model", lambda: LSTMVAE(**MODEL_CONFIG)
    )
    learning_module._manager = None
    learning_module._drift_detectors.clear()

    async def override_get_db():
        yield learning_db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    learning_module._manager = None
    learning_module._drift_detectors.clear()


# ---------------------------------------------------------------------------
# Base-model loader failure path
# ---------------------------------------------------------------------------


def test_load_base_model_raises_503_with_an_accurate_reason() -> None:
    """Online learning has no shared base model, and now says so.

    This used to patch ``get_cached_detector`` to raise and assert the message
    was propagated. In production nothing needed to be patched: the real call
    was ``get_cached_detector(None)``, which resolved ``models/baseline.pt`` —
    excluded from the image by ``.dockerignore`` — so the route always returned
    503 reading "No such file or directory". The failure is now explicit and
    explains itself; making online learning per-tenant is BACKLOG-12.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        learning_module._load_base_model()

    assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    detail = exc_info.value.detail
    assert "shared base model" in detail
    assert "BACKLOG-12" in detail
    assert "baseline.pt" not in detail, (
        "the 503 must no longer blame a missing file that never existed"
    )


# ---------------------------------------------------------------------------
# POST /api/learning/observe
# ---------------------------------------------------------------------------


class TestObserveEndpoint:
    def test_observe_returns_batch_losses(self, learning_client: TestClient) -> None:
        response = learning_client.post(
            "/api/learning/observe", json=observe_payload(n_sequences=3)
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert len(body["losses"]) == 3
        assert all(loss > 0.0 for loss in body["losses"])
        assert body["sequences_observed"] == 3
        # Default update_interval (50) is never reached by a 3-sequence batch.
        assert body["updates_performed"] == 0

    def test_observe_drift_block_warns_without_history(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.post(
            "/api/learning/observe", json=observe_payload(n_sequences=2)
        )
        drift = response.json()["drift"]
        assert drift is not None
        assert drift["drift_detected"] is False
        assert "Insufficient data" in drift["warning"]

    def test_observe_drift_block_evaluates_with_small_window(
        self, learning_client: TestClient
    ) -> None:
        # Pre-seed a tiny-window detector so 5 losses produce a real evaluation.
        learning_module._drift_detectors["tenant_d"] = DriftDetector(window_size=2)
        response = learning_client.post(
            "/api/learning/observe",
            json=observe_payload(tenant_id="tenant_d", n_sequences=5),
        )
        drift = response.json()["drift"]
        assert drift["warning"] is None
        assert drift["recent_avg"] is not None
        assert drift["historical_avg"] is not None
        assert drift["action"] in {"retrain_model", "continue_learning"}

    def test_observe_logs_observe_batch_event(
        self, learning_client: TestClient
    ) -> None:
        learning_client.post(
            "/api/learning/observe", json=observe_payload(n_sequences=3)
        )
        response = learning_client.get(
            "/api/learning/history", params={"tenant_id": "tenant_a"}
        )
        assert response.status_code == status.HTTP_200_OK
        entries = response.json()
        observe_events = [
            e for e in entries if e["event_type"] == "observe_batch"
        ]
        assert len(observe_events) == 1
        event = observe_events[0]
        assert event["sequences_observed"] == 3
        assert event["loss_before"] > 0.0
        assert event["loss_after"] > 0.0
        assert event["greenhouse_id"] == "gh_1"

    def test_observe_logs_drift_check_once_detector_has_history(
        self, learning_client: TestClient
    ) -> None:
        learning_module._drift_detectors["tenant_d"] = DriftDetector(window_size=2)
        learning_client.post(
            "/api/learning/observe",
            json=observe_payload(tenant_id="tenant_d", n_sequences=5),
        )
        response = learning_client.get(
            "/api/learning/history",
            params={"tenant_id": "tenant_d", "event_type": DRIFT_CHECK_EVENT},
        )
        entries = response.json()
        assert len(entries) == 1
        assert entries[0]["drift_score"] is not None
        assert entries[0]["drift_detected"] is not None

    def test_observe_rejects_wrong_seq_len(self, learning_client: TestClient) -> None:
        short_sequence = sample_sequence()[:-1]
        response = learning_client.post(
            "/api/learning/observe",
            json=observe_payload(sequences=[short_sequence]),
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_observe_rejects_wrong_input_dim(
        self, learning_client: TestClient
    ) -> None:
        bad_sequence = [[0.5, 0.6, 0.4] for _ in range(SEQ_LEN)]
        response = learning_client.post(
            "/api/learning/observe",
            json=observe_payload(sequences=[bad_sequence]),
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_observe_rejects_oversized_batch(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.post(
            "/api/learning/observe",
            json=observe_payload(n_sequences=MAX_OBSERVE_BATCH + 1),
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_observe_rejects_empty_tenant_id(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.post(
            "/api/learning/observe", json=observe_payload(tenant_id="")
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_observe_rejects_empty_sequence_batch(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.post(
            "/api/learning/observe", json=observe_payload(sequences=[])
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_validation_error_body_keeps_detail_and_message(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.post(
            "/api/learning/observe", json=observe_payload(tenant_id="")
        )
        body = response.json()
        assert "detail" in body
        assert "message" in body


# ---------------------------------------------------------------------------
# POST /api/learning/predict
# ---------------------------------------------------------------------------


class TestPredictEndpoint:
    def test_predict_unknown_tenant_falls_back_to_base_model(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.post(
            "/api/learning/predict",
            json={"tenant_id": "ghost_tenant", "sequence": sample_sequence()},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["learner_active"] is False
        assert body["anomaly_score"] > 0.0
        assert len(body["embedding"]) == LATENT_DIM
        assert body["uncertainty"] > 0.0
        assert body["updates_performed"] == 0

    def test_predict_after_observe_uses_tenant_learner(
        self, learning_client: TestClient
    ) -> None:
        learning_client.post(
            "/api/learning/observe", json=observe_payload(tenant_id="tenant_p")
        )
        response = learning_client.post(
            "/api/learning/predict",
            json={"tenant_id": "tenant_p", "sequence": sample_sequence()},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["learner_active"] is True
        assert len(body["embedding"]) == LATENT_DIM

    def test_predict_rejects_wrong_shape(self, learning_client: TestClient) -> None:
        response = learning_client.post(
            "/api/learning/predict",
            json={"tenant_id": "tenant_a", "sequence": sample_sequence()[:-1]},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /api/learning/statistics
# ---------------------------------------------------------------------------


class TestStatisticsEndpoint:
    def test_statistics_without_learner_returns_marker(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.get(
            "/api/learning/statistics", params={"tenant_id": "ghost"}
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["tenant_id"] == "ghost"
        assert body["learner"] == {"status": "no_learner"}
        assert body["summary"]["total_events"] == 0
        assert body["summary"]["last_event_at"] is None

    def test_statistics_after_observe(self, learning_client: TestClient) -> None:
        learning_client.post(
            "/api/learning/observe", json=observe_payload(tenant_id="tenant_s")
        )
        response = learning_client.get(
            "/api/learning/statistics", params={"tenant_id": "tenant_s"}
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["learner"]["sequences_observed"] == 3
        assert body["summary"]["total_events"] >= 1
        assert "observe_batch" in body["summary"]["events_by_type"]

    def test_statistics_requires_tenant_id(self, learning_client: TestClient) -> None:
        response = learning_client.get("/api/learning/statistics")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /api/learning/history
# ---------------------------------------------------------------------------


class TestHistoryEndpoint:
    @pytest.mark.asyncio
    async def test_history_filters_by_event_type(
        self, learning_client: TestClient, learning_db_session: AsyncSession
    ) -> None:
        store = LearningLogStore(learning_db_session)
        await store.log_event(tenant_id="tenant_h", event_type="observe_batch")
        await store.log_event(
            tenant_id="tenant_h", event_type=DRIFT_CHECK_EVENT, drift_score=0.2
        )
        response = learning_client.get(
            "/api/learning/history",
            params={"tenant_id": "tenant_h", "event_type": DRIFT_CHECK_EVENT},
        )
        entries = response.json()
        assert len(entries) == 1
        assert entries[0]["event_type"] == DRIFT_CHECK_EVENT

    @pytest.mark.asyncio
    async def test_history_is_tenant_isolated(
        self, learning_client: TestClient, learning_db_session: AsyncSession
    ) -> None:
        store = LearningLogStore(learning_db_session)
        await store.log_event(tenant_id="tenant_x", event_type="observe_batch")
        await store.log_event(tenant_id="tenant_y", event_type="observe_batch")
        response = learning_client.get(
            "/api/learning/history", params={"tenant_id": "tenant_x"}
        )
        entries = response.json()
        assert len(entries) == 1
        assert entries[0]["tenant_id"] == "tenant_x"

    @pytest.mark.asyncio
    async def test_history_respects_limit(
        self, learning_client: TestClient, learning_db_session: AsyncSession
    ) -> None:
        store = LearningLogStore(learning_db_session)
        for _ in range(4):
            await store.log_event(tenant_id="tenant_h", event_type="observe_batch")
        response = learning_client.get(
            "/api/learning/history", params={"tenant_id": "tenant_h", "limit": 2}
        )
        assert len(response.json()) == 2

    def test_history_rejects_zero_limit(self, learning_client: TestClient) -> None:
        response = learning_client.get(
            "/api/learning/history", params={"tenant_id": "tenant_h", "limit": 0}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_history_empty_for_unknown_tenant(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.get(
            "/api/learning/history", params={"tenant_id": "nobody"}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []


# ---------------------------------------------------------------------------
# GET /api/learning/drift
# ---------------------------------------------------------------------------


class TestDriftEndpoint:
    def test_drift_unknown_tenant_returns_404_with_message_key(
        self, learning_client: TestClient
    ) -> None:
        response = learning_client.get(
            "/api/learning/drift", params={"tenant_id": "ghost"}
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        body = response.json()
        # Custom HTTPException handler returns {"message"} instead of {"detail"}.
        assert "message" in body
        assert "detail" not in body
        assert "ghost" in body["message"]

    def test_drift_with_live_detector_but_no_persisted_checks(
        self, learning_client: TestClient
    ) -> None:
        learning_module._drift_detectors["tenant_live"] = DriftDetector(
            window_size=3
        )
        response = learning_client.get(
            "/api/learning/drift", params={"tenant_id": "tenant_live"}
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["latest_check"] is None
        assert body["detector"]["window_size"] == 3
        assert body["detector"]["losses_tracked"] == 0

    def test_drift_returns_latest_persisted_check(
        self, learning_client: TestClient
    ) -> None:
        learning_module._drift_detectors["tenant_d"] = DriftDetector(window_size=2)
        learning_client.post(
            "/api/learning/observe",
            json=observe_payload(tenant_id="tenant_d", n_sequences=5),
        )
        response = learning_client.get(
            "/api/learning/drift", params={"tenant_id": "tenant_d"}
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["tenant_id"] == "tenant_d"
        assert body["latest_check"] is not None
        assert body["latest_check"]["event_type"] == DRIFT_CHECK_EVENT
        assert body["detector"]["window_size"] == 2
        assert body["detector"]["losses_tracked"] == 4  # deque maxlen = 2 * window
