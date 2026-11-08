"""End-to-end: a Pass-1 warning survives both API consumers (BACKLOG-14).

Two 500s were found on this stack, and they share one shape: a type changed in
``src/models/`` and its ``src/api/`` **consumers** had no test.

* ``_build_pass1_metrics`` fed raw ``Pass1Warning`` dataclasses to
  ``json.dumps`` -> ``TypeError``.
* ``BootstrapRunResponse.pass1_warnings`` was typed ``list[str]`` while the
  route passed ``Pass1Warning`` objects -> ``pydantic.ValidationError`` on
  **every run that produced any warning**.

``tests/api/test_pass1_metrics_builder.py`` and
``tests/api/test_bootstrap_response_warning_shape.py`` pin each consumer in
isolation, which is where the precise diagnosis lives. They do **not** replace
this module: both call their target directly — one builds a
``BootstrapPass1Result`` by hand, the other constructs the response model
in-process — so neither exercises a route, and neither would catch a route
that assembles the right type into the wrong field, or a Pass-1 stage that
stops producing warnings altogether. This module runs the real paths.

## The fixture is the crux

**A clean run produces no warnings and would pass both tests vacuously** —
which is precisely how both breaks hid. ``list(())`` is ``[]``, which satisfies
``list[str]`` as readily as anything else, and the common upload is
warning-free, so the only runs that broke were the ones already in trouble.

So every test here asserts the warning list is **non-empty first**. If fixture
behaviour ever drifts such that no warning is produced, these tests fail loudly
rather than degrading into a green that proves nothing.

Warnings are produced genuinely, by the real producer on the real pipeline:
``BOOTSTRAP_CULL_WARN_FRACTION`` is lowered under the knife-edge fixture's real
~4.0% union discard, so ``evaluate_pass1_discard`` emits a real
``discard_fraction_high``. The limit is patched on ``pass1_discard_guard``
rather than on ``src.config`` because ``_status_for`` resolves the name from
that module's globals at call time. Nothing about the fixture, the scrub or the
serialization is mocked.

Pass 2 (LSTM-VAE training) is mocked in the route test only: it is slow and has
nothing to do with warnings, which are entirely a Pass-1 product. The
cold-start test keeps the real ``_build_pass1_metrics`` and a real database
round trip, because ``metrics_json`` going through a real INSERT and SELECT is
the thing that actually broke.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.api.services import cold_start_training
from src.api.services.cold_start_training import train_cold_start_from_csv
from src.db.models import Base, ModelVersion
from src.db.session import get_db
from src.models import pass1_discard_guard
from src.models.pass1_warning import PASS1_WARNING_CODES, WARNING_CODE_DISCARD_HIGH

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
WARNING_FIXTURE = _FIXTURE_DIR / "pass1_light_knife_edge_high_modal.csv"

# Under the fixture's measured ~3.99% union discard, so the guard warns. Well
# clear of BOOTSTRAP_CULL_FAIL_FRACTION (0.25), so the run still succeeds — a
# fail would raise and there would be no 2xx to assert.
WARNING_TRIGGER_LIMIT = 0.02

TENANT = "tenant_warning_contract"


@pytest.fixture
def warning_producing_pass1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the real discard guard emit a real warning on the real fixture."""
    monkeypatch.setattr(
        pass1_discard_guard, "BOOTSTRAP_CULL_WARN_FRACTION", WARNING_TRIGGER_LIMIT
    )


def _assert_code_message_shape(warnings: list) -> None:
    """Each entry is exactly ``{code, message}`` with string values.

    Asserts non-emptiness first: the whole point of this module is that an
    empty list passes every shape assertion trivially.
    """
    assert warnings, (
        "no warnings were produced — the fixture or the trigger limit has "
        "drifted, and these assertions would otherwise pass vacuously"
    )
    for entry in warnings:
        assert not isinstance(entry, str), f"still a bare string: {entry!r}"
        assert set(entry) == {"code", "message"}, f"unexpected keys: {set(entry)}"
        assert isinstance(entry["code"], str)
        assert isinstance(entry["message"], str)
        assert entry["code"] in PASS1_WARNING_CODES
        assert entry["message"]


# ---------------------------------------------------------------------------
# Consumer 1 — POST /api/v1/bootstrap/run
# ---------------------------------------------------------------------------


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


@pytest.fixture
def route_output_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the route's clean/anomaly CSVs out of ``data/``.

    ``data/`` already holds stale artefacts from manual runs; a test that adds
    to them makes the next person's diagnosis harder.
    """
    monkeypatch.setattr(
        "src.api.routes.bootstrap.BOOTSTRAP_CLEAN_PATH", str(tmp_path / "clean.csv")
    )
    monkeypatch.setattr(
        "src.api.routes.bootstrap.BOOTSTRAP_ANOMALY_PATH", str(tmp_path / "anomaly.csv")
    )


def _post_bootstrap_run(client: TestClient):
    """Real Pass 1 on the real fixture; Pass 2 mocked as irrelevant and slow."""
    with patch("src.api.routes.bootstrap.Pass2BootstrapDetector") as pass2_cls:
        detector = pass2_cls.return_value
        detector.train.return_value = None
        detector.infer.return_value = []
        return client.post(
            "/api/v1/bootstrap/run",
            data={"tenant_id": TENANT},
            files={
                "file": (
                    "upload.csv",
                    WARNING_FIXTURE.read_bytes(),
                    "text/csv",
                )
            },
        )


class TestBootstrapRunResponse:
    """``POST /api/v1/bootstrap/run`` with warnings genuinely present."""

    def test_a_warning_producing_upload_still_returns_2xx(
        self, client: TestClient, warning_producing_pass1: None, route_output_paths: None
    ) -> None:
        """This is the request that 500'd: a warning made the response invalid.

        A "warn" discard status is a *successful* run — only "fail" raises — so
        the warning must not cost the tenant their 200.
        """
        response = _post_bootstrap_run(client)

        assert 200 <= response.status_code < 300, response.text

    def test_the_response_carries_code_message_objects(
        self, client: TestClient, warning_producing_pass1: None, route_output_paths: None
    ) -> None:
        response = _post_bootstrap_run(client)

        assert 200 <= response.status_code < 300, response.text
        _assert_code_message_shape(response.json()["pass1_warnings"])

    def test_the_warning_is_the_discard_one_the_fixture_triggers(
        self, client: TestClient, warning_producing_pass1: None, route_output_paths: None
    ) -> None:
        """Pins *which* warning, so the shape test cannot pass on a stray one."""
        response = _post_bootstrap_run(client)
        codes = [entry["code"] for entry in response.json()["pass1_warnings"]]

        assert WARNING_CODE_DISCARD_HIGH in codes

    def test_a_clean_run_returns_2xx_with_no_warnings(
        self, client: TestClient, route_output_paths: None
    ) -> None:
        """Negative control, and the case that always worked.

        Without the lowered limit the same fixture discards ~4.0%, under the
        real 15% warn threshold, so the list is empty. This is the shape the
        two breaks hid behind, pinned so the contrast is explicit.
        """
        response = _post_bootstrap_run(client)

        assert 200 <= response.status_code < 300, response.text
        assert response.json()["pass1_warnings"] == []


# ---------------------------------------------------------------------------
# Consumer 2 — the cold-start / auto-train path
# ---------------------------------------------------------------------------


class TestColdStartMetricsJson:
    """``train_cold_start_from_csv`` -> ``metrics_json``, through a real DB.

    ``tests/api/test_upload_checkpoint.py`` monkeypatches
    ``train_cold_start_from_csv`` away entirely, which is exactly why this path
    was uncovered: a mocked trainer cannot catch a serialization bug in the
    real one. Here ``_run_pass1_pass2`` and ``_build_pass1_metrics`` both run
    for real, and the metrics dict is read back out of the database rather than
    inspected in memory — the round trip is what broke.

    Only ``put_checkpoint`` is patched: it is artifact storage with no bearing
    on the warning contract.
    """

    @pytest_asyncio.fixture
    async def training_db(self) -> AsyncSession:
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
    def service_output_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            cold_start_training, "BOOTSTRAP_CLEAN_PATH", str(tmp_path / "clean.csv")
        )
        monkeypatch.setattr(
            cold_start_training, "BOOTSTRAP_ANOMALY_PATH", str(tmp_path / "anomaly.csv")
        )

    async def _train_and_read_metrics(self, db: AsyncSession) -> dict:
        """Run the real trainer and return the persisted ``metrics_json`` dict."""
        import json

        async def _fake_put_checkpoint(session, key, local_path):
            return f"artifacts/{key}.pt"

        with patch.object(
            cold_start_training, "put_checkpoint", _fake_put_checkpoint
        ):
            await train_cold_start_from_csv(db, TENANT, str(WARNING_FIXTURE))

        rows = await db.execute(
            select(ModelVersion).where(ModelVersion.customer_id == TENANT)
        )
        version = rows.scalars().one()
        return json.loads(version.metrics_json)

    @pytest.mark.asyncio
    async def test_training_succeeds_with_warnings_present(
        self,
        training_db: AsyncSession,
        warning_producing_pass1: None,
        service_output_paths: None,
    ) -> None:
        """The run that raised ``TypeError`` inside ``_build_pass1_metrics``."""
        metrics = await self._train_and_read_metrics(training_db)

        assert metrics["source"] == "cold_start_upload"

    @pytest.mark.asyncio
    async def test_metrics_json_carries_code_message_objects(
        self,
        training_db: AsyncSession,
        warning_producing_pass1: None,
        service_output_paths: None,
    ) -> None:
        """After a real INSERT and SELECT, not merely in the built dict."""
        metrics = await self._train_and_read_metrics(training_db)

        _assert_code_message_shape(metrics["pass1_warnings"])

    @pytest.mark.asyncio
    async def test_the_persisted_payload_has_no_nan_token(
        self,
        training_db: AsyncSession,
        warning_producing_pass1: None,
        service_output_paths: None,
    ) -> None:
        """``light`` is always skipped, so its statistics are always NaN.

        Production is Postgres, which rejects the bare ``NaN`` token SQLite
        accepts — so a green SQLite test on the raw string is the only place
        this can be caught locally.
        """
        rows = await training_db.execute(
            select(ModelVersion).where(ModelVersion.customer_id == TENANT)
        )
        assert rows.scalars().all() == [], "fixture leaked state between tests"

        metrics = await self._train_and_read_metrics(training_db)

        assert metrics["pass1_mad_channels"]["light"]["median"] is None
        assert metrics["pass1_mad_channels"]["light"]["skip_reason"] == (
            "excluded_channel"
        )
