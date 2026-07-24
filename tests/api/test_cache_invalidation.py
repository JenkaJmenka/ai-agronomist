"""Unit tests for src/api/services/cache_invalidation.py.

Tests:
(a) Helper returns False and does not raise when FastAPICache is uninitialized.
(b) Helper calls FastAPICache.clear() and returns True when initialized.
(c) Warning log emitted with reason on failure.
(d) Reason parameter is passed through to the log message.
(e) Timeout exceptions handled gracefully.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from src.api.services.cache_invalidation import invalidate_response_cache


class TestInvalidateResponseCacheUninitialized:
    """Helper is robust to an uninitialized FastAPICache backend."""

    @pytest.mark.asyncio
    async def test_returns_false_when_backend_attribute_error(self) -> None:
        """Returns False and does not raise when the backend is None (not initialised)."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(
                side_effect=AttributeError("FastAPICache has not been initialized")
            )
            result = await invalidate_response_cache("test-uninitialized")

        assert result is False

    @pytest.mark.asyncio
    async def test_does_not_raise_on_any_exception(self) -> None:
        """Returns False without raising for any exception type (RuntimeError, etc.)."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(side_effect=RuntimeError("redis unavailable"))
            result = await invalidate_response_cache("test-runtime-error")

        assert result is False

    @pytest.mark.asyncio
    async def test_does_not_raise_on_connection_error(self) -> None:
        """Returns False without raising when Redis connection fails."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(side_effect=ConnectionError("cannot connect"))
            result = await invalidate_response_cache("test-connection-error")

        assert result is False

    @pytest.mark.asyncio
    async def test_does_not_raise_on_timeout_error(self) -> None:
        """Returns False without raising on TimeoutError (Redis timeout)."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(side_effect=TimeoutError("connection timed out"))
            result = await invalidate_response_cache("test-timeout")

        assert result is False


class TestInvalidateResponseCacheInitialized:
    """Helper calls FastAPICache.clear() when the cache is properly initialized."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self) -> None:
        """Returns True when FastAPICache.clear() succeeds."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(return_value=1)
            result = await invalidate_response_cache("test-initialized")

        assert result is True

    @pytest.mark.asyncio
    async def test_calls_clear_once(self) -> None:
        """Calls FastAPICache.clear() exactly once per invocation."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(return_value=0)
            await invalidate_response_cache("test-call-count")

        mock_fc.clear.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_calls_clear_without_arguments(self) -> None:
        """Calls FastAPICache.clear() with no arguments (full cache flush)."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(return_value=42)
            await invalidate_response_cache("test-no-args")

        # Verify called with no positional or keyword arguments.
        args, kwargs = mock_fc.clear.call_args
        assert args == ()
        assert kwargs == {}

    @pytest.mark.asyncio
    async def test_returns_true_when_clear_returns_none(self) -> None:
        """Returns True even when FastAPICache.clear() returns None (some backends)."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(return_value=None)
            result = await invalidate_response_cache("test-none-return")

        assert result is True


class TestInvalidateResponseCacheLogging:
    """Warning log includes the reason on failure; no log on success."""

    @pytest.mark.asyncio
    async def test_warning_logged_on_failure_with_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """On failure the warning log message must include the caller's reason string."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(side_effect=RuntimeError("boom"))
            with caplog.at_level(logging.WARNING):
                await invalidate_response_cache("my_custom_reason")

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "my_custom_reason" in r.message
        ]
        assert warning_records, (
            f"Expected warning containing 'my_custom_reason'; "
            f"got: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_no_warning_logged_on_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Successful cache clear should not emit any warning log."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(return_value=1)
            with caplog.at_level(logging.WARNING):
                await invalidate_response_cache("success-reason")

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert not warning_records, (
            f"No warning expected on success; got: {[r.message for r in warning_records]}"
        )

    @pytest.mark.asyncio
    async def test_warning_includes_exc_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warning log must include exc_info for debugging the underlying error."""
        with patch(
            "src.api.services.cache_invalidation.FastAPICache"
        ) as mock_fc:
            mock_fc.clear = AsyncMock(side_effect=ValueError("test-exc-info"))
            with caplog.at_level(logging.WARNING):
                await invalidate_response_cache("exc-info-check")

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert warning_records, "Expected at least one warning"
        assert warning_records[0].exc_info is not None, (
            "Warning log must have exc_info set for debugging"
        )


# ---------------------------------------------------------------------------
# Endpoint integration: cache_invalidated key in response payloads
# ---------------------------------------------------------------------------

import datetime
import io

import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.models import Base, RawReading, SensorReading
from src.db.session import get_db


_TENANT = "tenant_cache_inv_test"
_NOW = datetime.datetime.now(datetime.timezone.utc)


def _csv_bytes(rows: int = 10) -> bytes:
    lines = ["timestamp,temperature,humidity,co2,light"]
    for i in range(rows):
        ts = (_NOW - datetime.timedelta(minutes=5 * (rows - i))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        lines.append(f"{ts},{20 + i * 0.1:.1f},{60 + i * 0.05:.2f},{400 + i},{100 + i}")
    return "\n".join(lines).encode()


@pytest_asyncio.fixture
async def _ci_test_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def _ci_client(_ci_test_db):
    async def override_get_db():
        yield _ci_test_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


class TestClearTenantDataResponseIncludesCacheKey:
    """DELETE /api/upload/data response must contain 'cache_invalidated'."""

    async def test_clear_tenant_data_has_cache_invalidated_key(
        self, _ci_client: TestClient, _ci_test_db: AsyncSession
    ) -> None:
        _ci_client.post(
            "/api/upload",
            files={"file": ("data.csv", io.BytesIO(_csv_bytes()), "text/csv")},
            data={"tenant_id": _TENANT, "greenhouse_id": "default"},
        )
        response = _ci_client.delete(
            "/api/upload/data",
            params={"tenant_id": _TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert "cache_invalidated" in body, (
            f"Response must include 'cache_invalidated'; keys: {list(body.keys())}"
        )
        assert isinstance(body["cache_invalidated"], bool)


class TestRemoveLiveDemoDataResponseIncludesCacheKey:
    """DELETE /api/upload/live-data response must contain 'cache_invalidated'."""

    async def test_remove_live_demo_data_has_cache_invalidated_key(
        self, _ci_client: TestClient, _ci_test_db: AsyncSession
    ) -> None:
        # Seed a live-tagged row so the endpoint has data to work with.
        _ci_test_db.add(
            SensorReading(
                tenant_id=_TENANT,
                greenhouse_id="default",
                timestamp=_NOW,
                temperature=22.0,
                humidity=60.0,
                co2=400.0,
                light=500.0,
                source="live",  # post-migration provenance tag
            )
        )
        _ci_test_db.add(
            RawReading(
                tenant_id=_TENANT,
                greenhouse_id="default",
                timestamp=_NOW,
                source="live",
                data={"temperature": 22.0},
            )
        )
        await _ci_test_db.commit()

        response = _ci_client.delete(
            "/api/upload/live-data",
            params={"tenant_id": _TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert "cache_invalidated" in body, (
            f"Response must include 'cache_invalidated'; keys: {list(body.keys())}"
        )
        assert isinstance(body["cache_invalidated"], bool)
