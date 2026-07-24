"""Tests for POST /api/v1/bootstrap/run.

All heavy engine operations (ColdStartBootstrap, Pass2BootstrapDetector
training, LSTM inference) are mocked with unittest.mock so tests run in
milliseconds without triggering actual model training.

A small synthetic CSV (50 rows) is used as the uploaded file; the route still
exercises the full HTTP layer, form parsing, temp-file IO, and response
serialisation.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.models.bootstrap import BootstrapPass1Result
from src.models.bootstrap import BootstrapRowResult as EngineBootstrapRowResult
from src.api.main import app

# ---------------------------------------------------------------------------
# Shared synthetic data
# ---------------------------------------------------------------------------

_N_ROWS = 50
_TIMESTAMPS = pd.date_range("2026-01-01", periods=_N_ROWS, freq="5min")


def _make_upload_bytes(n_rows: int = _N_ROWS) -> bytes:
    """Return a minimal SCADA CSV as bytes suitable for multipart upload.

    The timestamp column is the first column (index_col=0 in the route's
    pd.read_csv call will parse it as a DatetimeIndex).
    """
    rng = np.random.default_rng(1)
    timestamps = pd.date_range("2026-01-01", periods=n_rows, freq="5min")
    df = pd.DataFrame(
        {
            "timestamp": timestamps.strftime("%Y-%m-%d %H:%M:%S"),
            "temperature": rng.uniform(20.0, 30.0, n_rows),
            "humidity": rng.uniform(50.0, 80.0, n_rows),
            "co2": rng.uniform(300.0, 500.0, n_rows),
            "light": rng.uniform(0.0, 1000.0, n_rows),
        }
    )
    return df.to_csv(index=False).encode()


def _make_pass1_result(n_rows: int = _N_ROWS) -> BootstrapPass1Result:
    """Construct a mock BootstrapPass1Result for n_rows."""
    idx = pd.date_range("2026-01-01", periods=n_rows, freq="5min")
    clean_df = pd.DataFrame(
        {
            "temperature": 25.0,
            "humidity": 65.0,
            "co2": 400.0,
            "light": 500.0,
        },
        index=idx[: n_rows - 2],
    )
    anomaly_df = pd.DataFrame(
        {
            "temperature": 25.0,
            "humidity": 65.0,
            "co2": 400.0,
            "light": 500.0,
        },
        index=idx[n_rows - 2 :],
    )
    physics_flags = pd.Series(False, index=idx)
    if_flags = pd.Series(False, index=idx)
    return BootstrapPass1Result(
        clean_df=clean_df,
        anomaly_df=anomaly_df,
        physics_flags=physics_flags,
        mad_flags=pd.Series(False, index=idx),
        if_flags=if_flags,
        gas_mask_cols=frozenset(),
    )


def _make_infer_results(df: pd.DataFrame) -> list[EngineBootstrapRowResult]:
    """Return one synthetic EngineBootstrapRowResult per row in *df*."""
    return [
        EngineBootstrapRowResult(
            timestamp=ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            is_anomaly=(i % 10 == 0),  # every 10th row is anomalous for variety
            reconstruction_loss=float(i) * 0.01,
        )
        for i, ts in enumerate(df.index)
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_bootstrap_pipeline():
    """Patch ColdStartBootstrap and Pass2BootstrapDetector with fast mocks."""
    pass1_result = _make_pass1_result()

    with (
        patch("src.api.routes.bootstrap.ColdStartBootstrap") as mock_p1_cls,
        patch("src.api.routes.bootstrap.Pass2BootstrapDetector") as mock_p2_cls,
    ):
        # Pass 1 mock
        mock_p1_cls.return_value.run.return_value = pass1_result

        # Pass 2 mock — train is a no-op; infer returns per-row results
        mock_detector = mock_p2_cls.return_value
        mock_detector.train.return_value = None
        mock_detector.infer.side_effect = _make_infer_results

        yield mock_p1_cls, mock_p2_cls


# ---------------------------------------------------------------------------
# Success path tests
# ---------------------------------------------------------------------------


def test_run_bootstrap_returns_200(
    client: TestClient,
    mock_bootstrap_pipeline: Any,
) -> None:
    """POST /api/v1/bootstrap/run with a valid CSV must return HTTP 200."""
    response = client.post(
        "/api/v1/bootstrap/run",
        data={"tenant_id": "tenant-abc"},
        files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
    )
    assert response.status_code == 200, response.text


def test_response_has_required_fields(
    client: TestClient,
    mock_bootstrap_pipeline: Any,
) -> None:
    """Response JSON must contain rows_ingested, pass1_clean_rows, pass1_anomaly_rows, results."""
    response = client.post(
        "/api/v1/bootstrap/run",
        data={"tenant_id": "tenant-abc"},
        files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
    )
    body = response.json()
    assert "rows_ingested" in body
    assert "pass1_clean_rows" in body
    assert "pass1_anomaly_rows" in body
    assert "results" in body


def test_results_match_row_count(
    client: TestClient,
    mock_bootstrap_pipeline: Any,
) -> None:
    """len(results) must equal rows_ingested — one result per uploaded row."""
    response = client.post(
        "/api/v1/bootstrap/run",
        data={"tenant_id": "tenant-abc"},
        files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
    )
    body = response.json()
    assert len(body["results"]) == body["rows_ingested"]


def test_each_result_has_correct_schema(
    client: TestClient,
    mock_bootstrap_pipeline: Any,
) -> None:
    """Every element in results must have the full BootstrapRowResult schema."""
    response = client.post(
        "/api/v1/bootstrap/run",
        data={"tenant_id": "tenant-abc"},
        files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
    )
    body = response.json()

    for i, row in enumerate(body["results"]):
        assert "timestamp" in row, f"results[{i}] missing 'timestamp'"
        assert "is_anomaly" in row, f"results[{i}] missing 'is_anomaly'"
        assert "reconstruction_loss" in row, f"results[{i}] missing 'reconstruction_loss'"
        assert "pass1_filtered" in row, f"results[{i}] missing 'pass1_filtered'"
        assert "root_cause_tags" in row, f"results[{i}] missing 'root_cause_tags'"
        assert "contributing_sensors" in row, f"results[{i}] missing 'contributing_sensors'"
        assert "physics_validation_status" in row, (
            f"results[{i}] missing 'physics_validation_status'"
        )

        assert isinstance(row["is_anomaly"], bool), f"results[{i}].is_anomaly should be bool"
        assert isinstance(row["reconstruction_loss"], float), (
            f"results[{i}].reconstruction_loss should be float"
        )
        assert isinstance(row["pass1_filtered"], bool), (
            f"results[{i}].pass1_filtered should be bool"
        )
        assert isinstance(row["root_cause_tags"], list), (
            f"results[{i}].root_cause_tags should be list"
        )
        assert isinstance(row["contributing_sensors"], list), (
            f"results[{i}].contributing_sensors should be list"
        )
        assert isinstance(row["physics_validation_status"], str), (
            f"results[{i}].physics_validation_status should be str"
        )


def test_pass1_counts_are_consistent(
    client: TestClient,
    mock_bootstrap_pipeline: Any,
) -> None:
    """pass1_clean_rows + pass1_anomaly_rows must equal rows from Pass 1 result."""
    _, _ = mock_bootstrap_pipeline
    response = client.post(
        "/api/v1/bootstrap/run",
        data={"tenant_id": "tenant-abc"},
        files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
    )
    body = response.json()
    # The mock Pass1 result has _N_ROWS-2 clean and 2 anomaly rows.
    assert body["pass1_clean_rows"] == _N_ROWS - 2
    assert body["pass1_anomaly_rows"] == 2


def test_reconstruction_loss_values_are_floats(
    client: TestClient,
    mock_bootstrap_pipeline: Any,
) -> None:
    """All reconstruction_loss values in results must be JSON-serialisable floats."""
    response = client.post(
        "/api/v1/bootstrap/run",
        data={"tenant_id": "tenant-abc"},
        files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
    )
    body = response.json()
    for row in body["results"]:
        assert isinstance(row["reconstruction_loss"], (int, float))


# ---------------------------------------------------------------------------
# Validation error paths
# ---------------------------------------------------------------------------


def test_missing_file_returns_422(client: TestClient) -> None:
    """POST without a file must return HTTP 422 Unprocessable Entity."""
    response = client.post(
        "/api/v1/bootstrap/run",
        data={"tenant_id": "tenant-abc"},
        # No 'file' field
    )
    assert response.status_code == 422


def test_missing_tenant_returns_422(client: TestClient) -> None:
    """POST without tenant_id must return HTTP 422 Unprocessable Entity."""
    response = client.post(
        "/api/v1/bootstrap/run",
        # No 'tenant_id' field
        files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Error-mapping tests
# ---------------------------------------------------------------------------


def test_insufficient_clean_rows_returns_400(client: TestClient) -> None:
    """If Pass 1 raises 'Insufficient clean rows', the route must return 400."""
    with (
        patch("src.api.routes.bootstrap.ColdStartBootstrap") as mock_p1_cls,
        patch("src.api.routes.bootstrap.Pass2BootstrapDetector"),
    ):
        mock_p1_cls.return_value.run.side_effect = ValueError(
            "Insufficient clean rows: 5 (minimum 100 required after Pass 1 filtering)"
        )

        response = client.post(
            "/api/v1/bootstrap/run",
            data={"tenant_id": "tenant-abc"},
            files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
        )

    assert response.status_code == 400


def test_training_failure_returns_500(client: TestClient) -> None:
    """If Pass 2 training raises an unexpected exception, the route must return 500."""
    with (
        patch("src.api.routes.bootstrap.ColdStartBootstrap") as mock_p1_cls,
        patch("src.api.routes.bootstrap.Pass2BootstrapDetector") as mock_p2_cls,
    ):
        mock_p1_cls.return_value.run.return_value = _make_pass1_result()
        mock_p2_cls.return_value.train.side_effect = RuntimeError("GPU OOM")

        response = client.post(
            "/api/v1/bootstrap/run",
            data={"tenant_id": "tenant-abc"},
            files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
        )

    assert response.status_code == 500


def test_pass1_generic_exception_returns_500(client: TestClient) -> None:
    """If Pass 1 raises an unexpected exception, the route must return 500."""
    with (
        patch("src.api.routes.bootstrap.ColdStartBootstrap") as mock_p1_cls,
        patch("src.api.routes.bootstrap.Pass2BootstrapDetector"),
    ):
        mock_p1_cls.return_value.run.side_effect = RuntimeError("unexpected")

        response = client.post(
            "/api/v1/bootstrap/run",
            data={"tenant_id": "tenant-abc"},
            files={"file": ("upload.csv", _make_upload_bytes(), "text/csv")},
        )

    assert response.status_code == 500
