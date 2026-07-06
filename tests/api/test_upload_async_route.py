"""Tests for async CSV upload queue endpoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse_connext.csv"


@pytest.fixture
def client():
    return TestClient(app)


def test_upload_async_returns_202_before_background_finishes(client: TestClient):
    """Upload-async must return 202 immediately; work continues in background."""
    calls: list[dict] = []

    async def record_pipeline(**kwargs):
        calls.append(kwargs)

    with patch(
        "src.api.routes.inference.run_upload_async_pipeline",
        side_effect=record_pipeline,
    ):
        with FIXTURE.open("rb") as fh:
            resp = client.post(
                "/api/inference/upload-async",
                data={
                    "tenant_id": "tenant_async",
                    "greenhouse_id": "default",
                    "persist_data": "false",
                    "train_model": "false",
                },
                files={"file": ("greenhouse_connext.csv", fh, "text/csv")},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"]
    assert body["portion_count"] == 0
    assert body["poll_url"] == f"/api/jobs/{body['job_id']}"
    assert len(calls) == 1
    assert calls[0]["job_id"] == body["job_id"]
