"""
Load test scenarios for the AI Agronomist inference API.

Run with:
    locust -f tests/load/locustfile.py --host http://localhost:8000

Targets:
  - Single comprehensive inference: p95 < 50ms
  - Batch inference (20 sequences): p95 < 500ms
  - Incident similarity search: p95 < 100ms
"""

from locust import HttpUser, task, between
import json
import random

_SEQUENCE = [[20.0 + random.uniform(-1, 1), 65.0, 400.0, 500.0] for _ in range(20)]
_EMBEDDING = [random.uniform(-1, 1) for _ in range(8)]

SINGLE_PAYLOAD = {
    "customer_id": "load_test_tenant",
    "greenhouse_id": "gh_001",
    "sequence": _SEQUENCE,
}

BATCH_PAYLOAD = {
    "customer_id": "load_test_tenant",
    "greenhouse_id": "gh_001",
    "sequences": [_SEQUENCE for _ in range(20)],
}

SIMILAR_PARAMS = [("embedding", str(v)) for v in _EMBEDDING]


class AgronomistUser(HttpUser):
    wait_time = between(0.1, 0.5)

    headers = {"X-API-Key": "load_test_key", "Content-Type": "application/json"}

    @task(5)
    def single_inference(self):
        self.client.post(
            "/api/inference/comprehensive",
            data=json.dumps(SINGLE_PAYLOAD),
            headers=self.headers,
            name="POST /api/inference/comprehensive",
        )

    @task(2)
    def batch_inference(self):
        self.client.post(
            "/api/inference/batch",
            data=json.dumps(BATCH_PAYLOAD),
            headers=self.headers,
            name="POST /api/inference/batch",
        )

    @task(3)
    def similarity_search(self):
        self.client.get(
            "/api/incidents/similar",
            params=SIMILAR_PARAMS,
            headers=self.headers,
            name="GET /api/incidents/similar",
        )
