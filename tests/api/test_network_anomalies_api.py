"""API tests for /api/network endpoints (P2: stateful registry + new routes).

Covers the two previously missing endpoints (IAT CV, Isolation Forest
threat scoring) and the per-tenant statefulness that per-request detector
instantiation used to break (jitter memory, dynamic-P99 accumulation).
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.services.network_detector_registry import clear_network_registries


@pytest.fixture
def client():
    clear_network_registries()
    with TestClient(app) as test_client:
        yield test_client
    clear_network_registries()


class TestRTTJitterState:
    def test_jitter_tracked_across_requests(self, client: TestClient) -> None:
        """Second request computes jitter from the first without the caller
        resending previous_rtt_ms — the per-tenant detector remembers it."""
        first = client.post(
            "/api/network/rtt-jitter",
            json={"rtt_ms": 2.0, "tenant_id": "t_jitter"},
        )
        assert first.status_code == 200
        assert first.json()["is_anomaly"] is False

        second = client.post(
            "/api/network/rtt-jitter",
            json={"rtt_ms": 157.0, "tenant_id": "t_jitter"},
        )
        body = second.json()
        assert body["rtt_violation"] is True
        assert body["jitter_ms"] == pytest.approx(155.0)
        assert body["jitter_violation"] is True

    def test_tenants_do_not_share_state(self, client: TestClient) -> None:
        client.post("/api/network/rtt-jitter", json={"rtt_ms": 2.0, "tenant_id": "t_a"})
        other = client.post(
            "/api/network/rtt-jitter", json={"rtt_ms": 157.0, "tenant_id": "t_b"}
        )
        # t_b has no previous RTT, so no jitter can be computed.
        assert other.json()["jitter_ms"] is None


class TestIATEndpoint:
    def test_spec_table12_series(self, client: TestClient) -> None:
        """Spec §11 Table 12: 4.95s spacing at index 5 must flag."""
        res = client.post(
            "/api/network/iat",
            json={"iat_series": [1.0, 1.0, 1.0, 1.0, 1.0, 4.95, 0.05, 0.10]},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["is_anomaly"] is True
        assert body["flags"][5] is True

    def test_steady_polling_is_normal(self, client: TestClient) -> None:
        res = client.post("/api/network/iat", json={"iat_series": [1.0] * 30})
        assert res.json()["is_anomaly"] is False


class TestServiceResponseState:
    def test_baseline_accumulates_across_requests(self, client: TestClient) -> None:
        """Observed durations feed the per-tenant P99 window across requests."""
        from src.api.services.network_detector_registry import get_service_detector

        for _ in range(5):
            res = client.post(
                "/api/network/service-response",
                json={"duration_ms": 45.0, "tenant_id": "t_svc"},
            )
            assert res.json()["is_anomaly"] is False

        detector = get_service_detector("t_svc")
        assert len(detector._samples) == 5

    def test_static_floor_still_flags(self, client: TestClient) -> None:
        res = client.post(
            "/api/network/service-response",
            json={"duration_ms": 350.0, "tenant_id": "t_svc2"},
        )
        assert res.json()["is_anomaly"] is True


class TestThreatEndpoints:
    @staticmethod
    def _baseline_vectors(n: int = 200) -> list[list[float]]:
        rng = np.random.RandomState(0)
        base = rng.normal(0.0, 1.0, (n, 19))
        base[:, 0] = 2.0 + rng.normal(0.0, 0.2, n)  # RTT
        base[:, 1] = rng.exponential(0.1, n)  # jitter
        base[:, 2] = 1.0 + rng.normal(0.0, 0.05, n)  # CV_IAT
        return base.tolist()

    def test_score_before_fit_returns_409(self, client: TestClient) -> None:
        res = client.post(
            "/api/network/threat/score",
            json={"vector": [0.0] * 19, "tenant_id": "t_unfitted"},
        )
        assert res.status_code == 409

    def test_fit_then_score_separates_rogue_from_normal(self, client: TestClient) -> None:
        vectors = self._baseline_vectors()
        fit = client.post(
            "/api/network/threat/fit",
            json={"vectors": vectors, "tenant_id": "t_threat"},
        )
        assert fit.status_code == 200
        assert fit.json()["fitted_samples"] == len(vectors)

        normal = client.post(
            "/api/network/threat/score",
            json={"vector": vectors[0], "tenant_id": "t_threat"},
        ).json()

        rogue_vector = list(vectors[1])
        rogue_vector[0] = 157.0  # RTT
        rogue_vector[1] = 118.0  # jitter
        rogue_vector[2] = 4.95  # CV_IAT
        rogue = client.post(
            "/api/network/threat/score",
            json={"vector": rogue_vector, "tenant_id": "t_threat"},
        ).json()

        assert rogue["threat_score"] > normal["threat_score"]
        assert 0.0 <= normal["threat_score"] <= 100.0
