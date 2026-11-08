"""``ExcessivePass1DiscardError`` reaches the tenant as HTTP 400 on both routes.

The discard guard's contract is that no API change was needed to surface it:
``ExcessivePass1DiscardError`` subclasses ``ValueError``, and
``bootstrap.InsufficientDataError`` **is** ``ValueError``
(``src/models/bootstrap.py:53``), so both upload paths already map it to a 400.

That reasoning is correct and was entirely untested. It is also the kind of
claim that rots quietly: a later refactor narrowing either ``except`` clause to
a concrete class would turn a precise 400 into a generic 500 — the route would
still *respond*, so nothing would look broken, and the tenant would simply stop
being told what was wrong with their upload.

Two mappings are pinned:

* ``src/api/routes/bootstrap.py`` — ``except InsufficientDataError`` around
  ``ColdStartBootstrap.run``.
* ``src/api/routes/inference.py`` — the handlers around
  ``resolve_upload_checkpoint``.

Both assert the **message survives into the response body**, not merely the
status code. ``ColdStartBootstrap.run`` deliberately raises with the
tenant-safe ``warning.message`` rather than the log text, precisely because
``str(exc)`` becomes this body; a 400 carrying a generic string would discard
the only actionable thing the tenant gets.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.models import Base
from src.db.session import get_db
from src.models.pass1_discard_guard import ExcessivePass1DiscardError

# A realistic tenant-safe fail message: the shape `_with_warning` builds and
# `run()` raises with. Deliberately free of internal identifiers, so the
# leak-free assertion below is about the route's fidelity and not about this
# string being sanitized here.
TENANT_SAFE_DETAIL = (
    "Data cleaning removed 53004 of 104921 readings (50.5%), above the 25% "
    "limit, so training was stopped. Training on what remained would have "
    "taught the model normal behaviour from too small a share of your "
    "history. Please check the sensors flagged above and upload again."
)


# ``src/api/main.py``'s global ``StarletteHTTPException`` handler re-envelopes
# every error body: it discards FastAPI's ``detail`` key and returns
# ``{"message": exc.detail, "request_id": ...}``. So the tenant reads the
# message under ``message``, and a test asserting ``detail`` would raise
# KeyError on a response that is in fact correct.
RESPONSE_MESSAGE_KEY = "message"


def _upload_csv_bytes(rows: int = 40) -> io.BytesIO:
    header = "timestamp,temperature,humidity,co2,light"
    body = "\n".join(
        f"2026-06-06T11:{index // 60:02d}:{index % 60:02d}Z,25.0,50.0,400.0,1200.0"
        for index in range(rows)
    )
    return io.BytesIO(f"{header}\n{body}".encode())


@pytest_asyncio.fixture
async def empty_database() -> AsyncSession:
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
def client(empty_database: AsyncSession) -> TestClient:
    async def _override_get_db():
        yield empty_database

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestBootstrapRoute:
    """``POST /api/v1/bootstrap/run``."""

    def test_excessive_discard_returns_400(self, client: TestClient) -> None:
        with (
            patch("src.api.routes.bootstrap.ColdStartBootstrap") as pass1_cls,
            patch("src.api.routes.bootstrap.Pass2BootstrapDetector"),
        ):
            pass1_cls.return_value.run.side_effect = ExcessivePass1DiscardError(
                TENANT_SAFE_DETAIL
            )
            response = client.post(
                "/api/v1/bootstrap/run",
                data={"tenant_id": "tenant-abc"},
                files={"file": ("upload.csv", _upload_csv_bytes(), "text/csv")},
            )

        assert response.status_code == 400

    def test_the_tenant_safe_message_survives_into_the_body(
        self, client: TestClient
    ) -> None:
        """A 400 that drops the message tells the tenant nothing actionable."""
        with (
            patch("src.api.routes.bootstrap.ColdStartBootstrap") as pass1_cls,
            patch("src.api.routes.bootstrap.Pass2BootstrapDetector"),
        ):
            pass1_cls.return_value.run.side_effect = ExcessivePass1DiscardError(
                TENANT_SAFE_DETAIL
            )
            response = client.post(
                "/api/v1/bootstrap/run",
                data={"tenant_id": "tenant-abc"},
                files={"file": ("upload.csv", _upload_csv_bytes(), "text/csv")},
            )

        assert response.json()[RESPONSE_MESSAGE_KEY] == TENANT_SAFE_DETAIL

    def test_the_400_body_leaks_no_internal_identifiers(
        self, client: TestClient
    ) -> None:
        with (
            patch("src.api.routes.bootstrap.ColdStartBootstrap") as pass1_cls,
            patch("src.api.routes.bootstrap.Pass2BootstrapDetector"),
        ):
            pass1_cls.return_value.run.side_effect = ExcessivePass1DiscardError(
                TENANT_SAFE_DETAIL
            )
            response = client.post(
                "/api/v1/bootstrap/run",
                data={"tenant_id": "tenant-abc"},
                files={"file": ("upload.csv", _upload_csv_bytes(), "text/csv")},
            )

        body = response.text
        for identifier in ("BOOTSTRAP_", "MAD", "IsolationForest", "Traceback"):
            assert identifier not in body

    def test_an_unrelated_runtime_error_still_returns_500(
        self, client: TestClient
    ) -> None:
        """Negative control: 400 is specific to the discard stop, not universal.

        Without this, a route that returned 400 for every exception would pass
        the two tests above.
        """
        with (
            patch("src.api.routes.bootstrap.ColdStartBootstrap") as pass1_cls,
            patch("src.api.routes.bootstrap.Pass2BootstrapDetector"),
        ):
            pass1_cls.return_value.run.side_effect = RuntimeError("GPU fell over")
            response = client.post(
                "/api/v1/bootstrap/run",
                data={"tenant_id": "tenant-abc"},
                files={"file": ("upload.csv", _upload_csv_bytes(), "text/csv")},
            )

        assert response.status_code == 500
        assert TENANT_SAFE_DETAIL not in response.text


class TestInferenceUploadRoute:
    """``POST /api/inference/upload``."""

    def test_excessive_discard_returns_400(self, client: TestClient) -> None:
        with patch(
            "src.api.routes.inference.resolve_upload_checkpoint",
            side_effect=ExcessivePass1DiscardError(TENANT_SAFE_DETAIL),
        ):
            response = client.post(
                "/api/inference/upload",
                files={"file": ("upload.csv", _upload_csv_bytes(), "text/csv")},
                data={"tenant_id": "tenant_1", "greenhouse_id": "default"},
            )

        assert response.status_code == 400

    def test_the_tenant_safe_message_survives_into_the_body(
        self, client: TestClient
    ) -> None:
        with patch(
            "src.api.routes.inference.resolve_upload_checkpoint",
            side_effect=ExcessivePass1DiscardError(TENANT_SAFE_DETAIL),
        ):
            response = client.post(
                "/api/inference/upload",
                files={"file": ("upload.csv", _upload_csv_bytes(), "text/csv")},
                data={"tenant_id": "tenant_1", "greenhouse_id": "default"},
            )

        assert response.json()[RESPONSE_MESSAGE_KEY] == TENANT_SAFE_DETAIL

    def test_a_server_error_type_is_not_mapped_to_400(
        self, client: TestClient
    ) -> None:
        """Negative control, and the contract B2's path 3 depends on.

        ``ColdStartTrainingError`` is a ``RuntimeError``, so it must reach the
        tenant as a 500. This is what makes "a bare ``ValueError`` must not be
        relabelled as a data problem" an enforceable distinction at the route.
        """
        from src.api.services.upload_checkpoint import ColdStartTrainingError

        with patch(
            "src.api.routes.inference.resolve_upload_checkpoint",
            side_effect=ColdStartTrainingError("Cold-start training failed"),
        ):
            response = client.post(
                "/api/inference/upload",
                files={"file": ("upload.csv", _upload_csv_bytes(), "text/csv")},
                data={"tenant_id": "tenant_1", "greenhouse_id": "default"},
            )

        assert response.status_code == 500
