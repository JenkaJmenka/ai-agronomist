"""Regression tests for the GET /api/jobs list-view performance fix.

Confirms two invariants after the server-side JSON-stripping optimisation:

(a) List response is field-for-field identical to the detail response for every
    *light* key — no accidental truncation or mutation of returned data.

(b) Detail route (GET /api/jobs/{id}) returns the heavy keys that the list view
    deliberately omits — i.e. the full result is still accessible via the
    detail endpoint.

Root cause that this guards against
-------------------------------------
Before the fix, list_jobs() selected the full Job ORM object (all columns),
shipped several MB of payload + result JSON from the DB to Python for every row,
then called _lighten_result() to strip the heavy per-sequence arrays in Python.
The fix moves stripping to the SQL layer via _JsonStripHeavyKeys, which removes
heavy keys before the data leaves the database:

  PostgreSQL : (result::jsonb - '{...}'::text[])::text
  SQLite     : json_remove(result, '$.k1', '$.k2', …)

These tests run against SQLite in-memory so they also validate the SQLite
json_remove() compilation path used in the test suite.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.jobs import _HEAVY_RESULT_KEYS, create_job, set_job_result
from src.db.models import Base
from src.db.session import get_db

# ---------------------------------------------------------------------------
# Fixture data — contains both heavy (large arrays) and light (scalar) keys.
# The arrays use 3 000 elements to represent a realistic upload job that
# processes a few hours of 1-minute data at 1 sequence-per-row.
# ---------------------------------------------------------------------------

_N = 3_000  # simulate ~3 000 sequences in the upload job

_HEAVY_RESULT: dict[str, Any] = {
    # Heavy keys — stripped from list response
    "anomaly_scores": [round(0.1 + 0.0003 * i, 4) for i in range(_N)],
    "sequence_timestamps": [f"2024-01-01T{i // 60:02d}:{i % 60:02d}:00Z" for i in range(_N)],
    "embeddings": [],  # excluded before storing in production, but kept for belt-and-suspenders
    "uncertainties": [],
    "high_anomalies": [
        {"timestamp": "2024-01-01T00:30:00Z", "score": 0.95, "sensor": "temperature"}
    ]
    * 50,
    "statistical_summary": {
        "mean": 0.35,
        "std": 0.18,
        "p95": 0.82,
        "p99": 0.91,
        "outlier_count": 50,
    },
    # Light keys — must survive in list response unchanged
    "tenant_id": "t1",
    "greenhouse_id": "gh1",
    "sequences_processed": _N,
    "anomalies_detected": 50,
    "threshold_used": 0.75,
    "threshold_method": "p99",
    "imputed_channels": [],
    "embedding_dim": 8,
    "data_persisted": True,
    "rows_uploaded": _N,
    "sensor_rows_uploaded": _N,
    "raw_rows_uploaded": 0,
    "model_trained": True,
    "model_id": "model-test-abc",
    "training_skipped_reason": None,
}

_LIGHT_KEYS: frozenset[str] = frozenset(_HEAVY_RESULT) - frozenset(_HEAVY_RESULT_KEYS)

# Bytes in the full result JSON — used to assert the list response is meaningfully
# smaller than what was previously shipped to Python.
_FULL_RESULT_BYTES: int = len(json.dumps(_HEAVY_RESULT).encode())


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def test_db() -> AsyncSession:  # type: ignore[misc]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def client(test_db: AsyncSession) -> TestClient:  # type: ignore[misc]
    async def _override_db() -> AsyncSession:  # type: ignore[misc]
        yield test_db

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# (b) Detail route must return the heavy keys the list view omits
# ---------------------------------------------------------------------------


class TestDetailRouteReturnsHeavyKeys:
    """GET /api/jobs/{id} always returns the full result including heavy keys."""

    @pytest_asyncio.fixture
    async def completed_job(self, test_db: AsyncSession) -> Any:
        job = await create_job(
            test_db, tenant_id="t1", job_type="upload_csv", greenhouse_id="gh1"
        )
        await set_job_result(test_db, job.id, status="completed", result=_HEAVY_RESULT)
        return job

    def test_detail_has_anomaly_scores(self, client: TestClient, completed_job: Any) -> None:
        resp = client.get(f"/api/jobs/{completed_job.id}")
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert "anomaly_scores" in result
        assert len(result["anomaly_scores"]) == _N

    def test_detail_has_sequence_timestamps(
        self, client: TestClient, completed_job: Any
    ) -> None:
        resp = client.get(f"/api/jobs/{completed_job.id}")
        result = resp.json()["result"]
        assert "sequence_timestamps" in result
        assert len(result["sequence_timestamps"]) == _N

    def test_detail_has_high_anomalies(self, client: TestClient, completed_job: Any) -> None:
        resp = client.get(f"/api/jobs/{completed_job.id}")
        result = resp.json()["result"]
        assert "high_anomalies" in result
        assert len(result["high_anomalies"]) == 50

    def test_detail_has_statistical_summary(
        self, client: TestClient, completed_job: Any
    ) -> None:
        resp = client.get(f"/api/jobs/{completed_job.id}")
        result = resp.json()["result"]
        assert "statistical_summary" in result
        assert result["statistical_summary"]["outlier_count"] == 50

    def test_detail_has_all_heavy_keys(self, client: TestClient, completed_job: Any) -> None:
        """Parameterised guard: every key in _HEAVY_RESULT_KEYS is present in detail."""
        resp = client.get(f"/api/jobs/{completed_job.id}")
        assert resp.status_code == 200
        result = resp.json()["result"]
        missing = [k for k in _HEAVY_RESULT_KEYS if k not in result]
        assert not missing, f"Detail route missing heavy keys: {missing}"


# ---------------------------------------------------------------------------
# (a) List view omits heavy keys and preserves light keys unchanged
# ---------------------------------------------------------------------------


class TestListViewHeavyKeysOmitted:
    """GET /api/jobs strips heavy result keys; scalar fields and light result keys survive."""

    @pytest_asyncio.fixture
    async def completed_job(self, test_db: AsyncSession) -> Any:
        job = await create_job(
            test_db, tenant_id="t1", job_type="upload_csv", greenhouse_id="gh1"
        )
        await set_job_result(test_db, job.id, status="completed", result=_HEAVY_RESULT)
        return job

    def _list_first(self, client: TestClient) -> dict[str, Any]:
        resp = client.get("/api/jobs", params={"tenant_id": "t1"})
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert len(jobs) == 1
        return jobs[0]

    def test_list_omits_anomaly_scores(self, client: TestClient, completed_job: Any) -> None:
        row = self._list_first(client)
        assert "anomaly_scores" not in (row["result"] or {})

    def test_list_omits_sequence_timestamps(
        self, client: TestClient, completed_job: Any
    ) -> None:
        row = self._list_first(client)
        assert "sequence_timestamps" not in (row["result"] or {})

    def test_list_omits_high_anomalies(self, client: TestClient, completed_job: Any) -> None:
        row = self._list_first(client)
        assert "high_anomalies" not in (row["result"] or {})

    def test_list_omits_statistical_summary(
        self, client: TestClient, completed_job: Any
    ) -> None:
        row = self._list_first(client)
        assert "statistical_summary" not in (row["result"] or {})

    def test_list_omits_all_heavy_keys(self, client: TestClient, completed_job: Any) -> None:
        """Parameterised guard: every key in _HEAVY_RESULT_KEYS is absent from list result."""
        row = self._list_first(client)
        result = row["result"] or {}
        present = [k for k in _HEAVY_RESULT_KEYS if k in result]
        assert not present, f"List response must not contain heavy keys: {present}"

    def test_list_preserves_sequences_processed(
        self, client: TestClient, completed_job: Any
    ) -> None:
        row = self._list_first(client)
        assert row["result"]["sequences_processed"] == _N

    def test_list_preserves_anomalies_detected(
        self, client: TestClient, completed_job: Any
    ) -> None:
        row = self._list_first(client)
        assert row["result"]["anomalies_detected"] == 50

    def test_list_preserves_model_id(self, client: TestClient, completed_job: Any) -> None:
        row = self._list_first(client)
        assert row["result"]["model_id"] == "model-test-abc"

    def test_list_preserves_threshold_used(
        self, client: TestClient, completed_job: Any
    ) -> None:
        row = self._list_first(client)
        assert row["result"]["threshold_used"] == pytest.approx(0.75)

    def test_list_light_result_matches_detail_for_all_light_keys(
        self, client: TestClient, completed_job: Any
    ) -> None:
        """Field-for-field: every light key has the same value in list and detail."""
        detail_result = client.get(f"/api/jobs/{completed_job.id}").json()["result"]
        list_result = self._list_first(client)["result"] or {}

        mismatches = []
        for key in _LIGHT_KEYS:
            if key not in detail_result:
                continue  # key absent from both — irrelevant
            if list_result.get(key) != detail_result[key]:
                mismatches.append(
                    f"{key!r}: detail={detail_result[key]!r}, list={list_result.get(key)!r}"
                )
        assert not mismatches, "Light key value mismatches:\n" + "\n".join(mismatches)

    def test_list_scalar_fields_match_detail(
        self, client: TestClient, completed_job: Any
    ) -> None:
        """Scalar Job fields (status, counts, timestamps) are identical in both views."""
        detail = client.get(f"/api/jobs/{completed_job.id}").json()
        row = self._list_first(client)
        for field in (
            "id",
            "tenant_id",
            "greenhouse_id",
            "job_type",
            "status",
            "total_portions",
            "completed_portions",
            "failed_portions",
            "error",
            "created_at",
            "updated_at",
            "finished_at",
        ):
            assert row[field] == detail[field], (
                f"Field {field!r}: list={row[field]!r}, detail={detail[field]!r}"
            )

    def test_list_progress_pct_matches_detail(
        self, client: TestClient, completed_job: Any
    ) -> None:
        detail = client.get(f"/api/jobs/{completed_job.id}").json()
        row = self._list_first(client)
        assert row["progress_pct"] == detail["progress_pct"]

    def test_list_result_bytes_much_smaller_than_full_result(
        self, client: TestClient, completed_job: Any
    ) -> None:
        """Confirms the list response is <10% the size of the full result blob.

        This is the core regression guard for the performance fix: the list
        endpoint used to transfer _FULL_RESULT_BYTES from the DB to Python,
        then discard the heavy arrays.  After the fix it transfers only the
        light subset.
        """
        row = self._list_first(client)
        list_result_bytes = len(json.dumps(row["result"]).encode())
        # Light result must be at least 10× smaller than the full blob
        assert list_result_bytes * 10 < _FULL_RESULT_BYTES, (
            f"List result ({list_result_bytes}B) should be much smaller than "
            f"full result ({_FULL_RESULT_BYTES}B).  Heavy keys may not be stripped."
        )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestJobsListEdgeCases:
    """Null result, multi-job filtering, and pending jobs."""

    @pytest.mark.asyncio
    async def test_pending_job_result_is_none(
        self, client: TestClient, test_db: AsyncSession
    ) -> None:
        """A pending job with no result yet returns null in both list and detail."""
        job = await create_job(test_db, tenant_id="t_pending", job_type="upload_csv")
        list_resp = client.get("/api/jobs", params={"tenant_id": "t_pending"})
        assert list_resp.status_code == 200
        jobs = list_resp.json()["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["result"] is None

        detail_resp = client.get(f"/api/jobs/{job.id}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["result"] is None

    @pytest.mark.asyncio
    async def test_result_without_heavy_keys_passes_through_unchanged(
        self, client: TestClient, test_db: AsyncSession
    ) -> None:
        """If a job result has no heavy keys, the light result is unchanged."""
        light_only_result = {
            "sequences_processed": 10,
            "anomalies_detected": 1,
            "model_trained": False,
        }
        job = await create_job(test_db, tenant_id="t_light", job_type="upload_csv")
        await set_job_result(
            test_db, job.id, status="completed", result=light_only_result
        )

        list_resp = client.get("/api/jobs", params={"tenant_id": "t_light"})
        assert list_resp.status_code == 200
        row_result = list_resp.json()["jobs"][0]["result"]
        assert row_result == light_only_result

    @pytest.mark.asyncio
    async def test_multiple_jobs_heavy_keys_stripped_from_all(
        self, client: TestClient, test_db: AsyncSession
    ) -> None:
        """All rows in a multi-job list have heavy keys stripped."""
        for i in range(3):
            job = await create_job(
                test_db, tenant_id="t_multi", job_type="upload_csv", greenhouse_id=f"gh{i}"
            )
            await set_job_result(
                test_db, job.id, status="completed", result=_HEAVY_RESULT
            )

        list_resp = client.get("/api/jobs", params={"tenant_id": "t_multi"})
        assert list_resp.status_code == 200
        jobs = list_resp.json()["jobs"]
        assert len(jobs) == 3
        for job_row in jobs:
            result = job_row["result"] or {}
            present_heavy = [k for k in _HEAVY_RESULT_KEYS if k in result]
            assert not present_heavy, (
                f"Job {job_row['id']}: heavy keys must be stripped, found {present_heavy}"
            )


# ---------------------------------------------------------------------------
# Timing benchmark — measures data savings, not latency (SQLite in-memory
# has no network overhead so absolute times are not meaningful, but we can
# verify the result payload is dramatically smaller than the full blob).
# ---------------------------------------------------------------------------


class TestJsonStripDialectCompilation:
    """Smoke-tests that _JsonStripHeavyKeys compiles correctly for SQLite."""

    def test_sqlite_strip_expression_compiles(self) -> None:
        """The _JsonStripHeavyKeys expression must produce valid SQL for SQLite."""
        from sqlalchemy import create_engine
        from sqlalchemy.dialects import sqlite as sqlite_dialect

        from src.db.jobs import Job, _JsonStripHeavyKeys

        expr = _JsonStripHeavyKeys(Job.result)
        dialect = sqlite_dialect.dialect()
        compiled = expr.compile(dialect=dialect)
        sql = str(compiled)

        # Each heavy key should appear as a json_remove path argument
        for key in _HEAVY_RESULT_KEYS:
            assert f"$.{key}" in sql, (
                f"SQLite compiled expression missing path for key {key!r}: {sql!r}"
            )
        assert "json_remove" in sql

    def test_heavy_result_keys_constant_is_nonempty(self) -> None:
        """_HEAVY_RESULT_KEYS is defined and non-empty — guards against accidental clear."""
        assert len(_HEAVY_RESULT_KEYS) >= 4
        assert "anomaly_scores" in _HEAVY_RESULT_KEYS
        assert "sequence_timestamps" in _HEAVY_RESULT_KEYS
