"""Tests for API key authentication and learning-state persistence."""

from __future__ import annotations

import os
from typing import Generator

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.auth import (
    clear_test_api_keys,
    register_test_api_key,
    register_test_web_api_key,
)
from src.api.main import app
from src.api.services.orchestrator_registry import (
    clear_orchestrator_cache,
    get_orchestrator,
)
from src.db.models import Base
from src.db.session import get_db


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def client(db_session: AsyncSession) -> Generator[TestClient, None, None]:
    async def override_get_db():
        yield db_session

    clear_orchestrator_cache()
    clear_test_api_keys()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    clear_orchestrator_cache()
    clear_test_api_keys()


@pytest.fixture
def auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    # jobs.py imports AUTH_ENABLED by value (its own module-level binding) for
    # the job-ownership 404 check, so it needs patching independently too.
    monkeypatch.setattr("src.api.routes.jobs.AUTH_ENABLED", True)
    register_test_api_key("tenant_a", "secret-a")
    register_test_api_key("tenant_b", "secret-b")


class TestAuthDefaultOff:
    def test_feedback_mark_without_key_when_auth_disabled(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.config.AUTH_ENABLED", False)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", False)
        response = client.post(
            "/api/feedback/mark",
            json={
                "customer_id": "tenant_a",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_200_OK


class TestAuthEnabled:
    def test_feedback_mark_401_without_key(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        response = client.post(
            "/api/feedback/mark",
            json={
                "customer_id": "tenant_a",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_feedback_mark_401_invalid_key(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        response = client.post(
            "/api/feedback/mark",
            headers={"X-API-Key": "wrong-key"},
            json={
                "customer_id": "tenant_a",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_feedback_mark_403_cross_tenant(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        response = client.post(
            "/api/feedback/mark",
            headers={"X-API-Key": "secret-a"},
            json={
                "customer_id": "tenant_b",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_feedback_mark_200_valid_key(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        response = client.post(
            "/api/feedback/mark",
            headers={"X-API-Key": "secret-a"},
            json={
                "customer_id": "tenant_a",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
async def test_learning_state_survives_cache_clear(
    db_session: AsyncSession,
) -> None:
    """Feedback persisted to DB is restored after in-memory cache eviction."""
    customer_id = "persist_cust"
    orch = await get_orchestrator(customer_id, db_session)
    orch.observe(
        incident_id="inc_persist",
        is_true_anomaly=True,
        operator_id="op_persist",
        pattern_type_observed="drift",
    )
    from src.api.services.orchestrator_registry import save_orchestrator

    await save_orchestrator(customer_id, orch, db_session)
    clear_orchestrator_cache()

    restored = await get_orchestrator(customer_id, db_session)
    stats = restored.learner.get_statistics()
    assert stats["total_feedback"] == 1
    assert stats["operator_count"] == 1


@pytest.fixture
def web_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a single wildcard-tenant `caller_type=web` credential.

    Mirrors the approved production shape: the BFF holds exactly one web
    credential and selects the tenant per-request via X-Tenant-Id.
    """
    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    register_test_web_api_key("web-secret", tenant_ids="*")


class TestWebCredentialTenantHeader:
    """caller_type=web derives the tenant from X-Tenant-Id, never the body/path alone."""

    def test_missing_x_tenant_id_is_400(
        self, client: TestClient, web_auth_enabled: None
    ) -> None:
        response = client.get(
            "/api/feedback/statistics/tenant_a",
            headers={"X-API-Key": "web-secret"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_x_tenant_id_mismatch_with_path_is_403(
        self, client: TestClient, web_auth_enabled: None
    ) -> None:
        response = client.get(
            "/api/feedback/statistics/tenant_a",
            headers={"X-API-Key": "web-secret", "X-Tenant-Id": "tenant_b"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_x_tenant_id_matching_path_is_200(
        self, client: TestClient, web_auth_enabled: None
    ) -> None:
        response = client.get(
            "/api/feedback/statistics/tenant_a",
            headers={"X-API-Key": "web-secret", "X-Tenant-Id": "tenant_a"},
        )
        assert response.status_code == status.HTTP_200_OK

    def test_scoped_web_credential_rejects_out_of_scope_tenant(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A web credential scoped to specific tenants (not '*') can't claim others."""
        monkeypatch.setattr("src.config.AUTH_ENABLED", True)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
        register_test_web_api_key("scoped-web-secret", tenant_ids=frozenset({"tenant_a"}))
        response = client.get(
            "/api/feedback/statistics/tenant_b",
            headers={"X-API-Key": "scoped-web-secret", "X-Tenant-Id": "tenant_b"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestIntegrationCredentialTenantHeader:
    """caller_type=integration never honours X-Tenant-Id — it's rejected outright."""

    def test_integration_credential_rejects_x_tenant_id_header(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        # auth_enabled registers "tenant_a" -> "secret-a" as an integration
        # credential (register_test_api_key's default caller_type).
        response = client.get(
            "/api/feedback/statistics/tenant_a",
            headers={"X-API-Key": "secret-a", "X-Tenant-Id": "tenant_a"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestJobOwnership:
    """Cross-tenant job access 404s (never 403) so existence isn't leaked."""

    @pytest.mark.asyncio
    async def test_get_job_wrong_tenant_is_404(
        self, client: TestClient, db_session: AsyncSession, auth_enabled: None
    ) -> None:
        from src.db.jobs import create_job

        job = await create_job(
            db_session, tenant_id="tenant_b", job_type="upload_csv", payload={}
        )
        response = client.get(f"/api/jobs/{job.id}", headers={"X-API-Key": "secret-a"})
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_get_job_owning_tenant_is_200(
        self, client: TestClient, db_session: AsyncSession, auth_enabled: None
    ) -> None:
        from src.db.jobs import create_job

        job = await create_job(
            db_session, tenant_id="tenant_a", job_type="upload_csv", payload={}
        )
        response = client.get(f"/api/jobs/{job.id}", headers={"X-API-Key": "secret-a"})
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.asyncio
    async def test_cancel_job_wrong_tenant_is_404(
        self, client: TestClient, db_session: AsyncSession, auth_enabled: None
    ) -> None:
        from src.db.jobs import create_job

        job = await create_job(
            db_session, tenant_id="tenant_b", job_type="upload_csv", payload={}
        )
        response = client.post(
            f"/api/jobs/{job.id}/cancel", headers={"X-API-Key": "secret-a"}
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAssertAuthConfigured:
    """A deployed environment must never start with auth disabled.

    Two independent signals (ENVIRONMENT, RAILWAY_ENVIRONMENT_NAME) each
    independently require AUTH_ENABLED=true — neither can silently fail
    open just because the other was left unset.
    """

    def test_raises_when_production_and_auth_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.api.auth import assert_auth_configured

        monkeypatch.setattr("src.config.ENVIRONMENT", "production")
        monkeypatch.setattr("src.api.auth.ENVIRONMENT", "production")
        monkeypatch.setattr("src.config.AUTH_ENABLED", False)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", False)
        with pytest.raises(RuntimeError, match="deployed environment"):
            assert_auth_configured()

    def test_environment_defaults_to_production_when_unset(self) -> None:
        """The fail-closed default itself, in a clean subprocess: with no
        ENVIRONMENT/AUTH_ENABLED/RAILWAY_ENVIRONMENT_NAME set at all,
        importing config must resolve ENVIRONMENT to "production" — the
        opposite of the old "development" default that silently allowed
        auth to stay off in a forgotten-var deployment."""
        import subprocess
        import sys

        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("ENVIRONMENT", "AUTH_ENABLED", "RAILWAY_ENVIRONMENT_NAME")
        }
        result = subprocess.run(
            [sys.executable, "-c", "import src.config as c; print(c.ENVIRONMENT)"],
            capture_output=True,
            text=True,
            cwd=".",
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == "production"

    def test_raises_when_railway_environment_name_set_even_if_environment_is_not_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RAILWAY_ENVIRONMENT_NAME alone is sufficient — corroborating signal
        for the case where ENVIRONMENT itself was never configured on the
        service, so one forgotten variable can't fail open."""
        from src.api.auth import assert_auth_configured

        monkeypatch.setattr("src.config.ENVIRONMENT", "development")
        monkeypatch.setattr("src.api.auth.ENVIRONMENT", "development")
        monkeypatch.setattr("src.config.RAILWAY_ENVIRONMENT_NAME", "production")
        monkeypatch.setattr("src.api.auth.RAILWAY_ENVIRONMENT_NAME", "production")
        monkeypatch.setattr("src.config.AUTH_ENABLED", False)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", False)
        with pytest.raises(RuntimeError, match="deployed environment"):
            assert_auth_configured()

    def test_allows_production_with_auth_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.api.auth import assert_auth_configured

        monkeypatch.setattr("src.config.ENVIRONMENT", "production")
        monkeypatch.setattr("src.api.auth.ENVIRONMENT", "production")
        monkeypatch.setattr("src.config.AUTH_ENABLED", True)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
        assert_auth_configured()  # must not raise

    def test_allows_non_production_with_auth_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.api.auth import assert_auth_configured

        monkeypatch.setattr("src.config.ENVIRONMENT", "development")
        monkeypatch.setattr("src.api.auth.ENVIRONMENT", "development")
        monkeypatch.setattr("src.config.RAILWAY_ENVIRONMENT_NAME", "")
        monkeypatch.setattr("src.api.auth.RAILWAY_ENVIRONMENT_NAME", "")
        monkeypatch.setattr("src.config.AUTH_ENABLED", False)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", False)
        assert_auth_configured()  # must not raise


class TestDocsGatedInProduction:
    """Interactive docs are disabled when ENVIRONMENT=production."""

    def test_docs_disabled_in_production(self) -> None:
        import json
        import subprocess
        import sys

        env_code = (
            "import os; os.environ['ENVIRONMENT'] = 'production'; "
            "os.environ['AUTH_ENABLED'] = 'true'; "
            "import src.api.main as m; "
            "print(__import__('json').dumps("
            "{'docs_url': m.app.docs_url, 'redoc_url': m.app.redoc_url, "
            "'openapi_url': m.app.openapi_url}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", env_code],
            capture_output=True,
            text=True,
            cwd=".",
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload == {"docs_url": None, "redoc_url": None, "openapi_url": None}

    def test_docs_enabled_outside_production(self) -> None:
        import json
        import subprocess
        import sys

        env_code = (
            "import os; os.environ['ENVIRONMENT'] = 'development'; "
            "import src.api.main as m; "
            "print(__import__('json').dumps("
            "{'docs_url': m.app.docs_url, 'redoc_url': m.app.redoc_url, "
            "'openapi_url': m.app.openapi_url}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", env_code],
            capture_output=True,
            text=True,
            cwd=".",
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload == {
            "docs_url": "/docs",
            "redoc_url": "/redoc",
            "openapi_url": "/openapi.json",
        }
