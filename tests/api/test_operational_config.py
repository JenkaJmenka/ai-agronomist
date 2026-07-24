"""Tests for the operational config backend (Phase P1).

Coverage
--------
1. Store merge semantics: crop switch re-derives defaults; explicit overrides survive.
2. Validation rejection: bad band, missing month, unknown crop, invalid season,
   out-of-range day_window hour, equal start/end hours.
3. Upsert round-trip through the store (in-memory + DB).
4. Cache invalidation called on PUT (mock invalidate_response_cache).
5. Migration upgrade/downgrade on an in-process SQLite DB.
"""

from __future__ import annotations

import copy
import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.api.services.operational_config_service import validate_operational_config
from src.config import CROP_SETPOINT_PROFILES, DEFAULT_DAY_WINDOW, DEFAULT_SEASON_MAP
from src.db.models import Base, OperationalConfig
from src.db.session import get_db
from src.models.operational_config_store import (
    OperationalConfigStore,
    default_operational_config,
    get_operational_config_store,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TENANT = "tenant_opcfg_test"
_GH = "gh_main"


def _valid_config(crop: str = "tomato") -> dict[str, Any]:
    """Build a minimal valid config document for *crop*.

    Uses deepcopy so that mutations to the returned dict do not corrupt the
    global CROP_SETPOINT_PROFILES constant across tests.
    """
    return {
        "crop": crop,
        "setpoints": copy.deepcopy(CROP_SETPOINT_PROFILES[crop]),
        "day_window": dict(DEFAULT_DAY_WINDOW),
        "season_map": dict(DEFAULT_SEASON_MAP),
        "seasonal_overrides": {
            "summer": {"detector_scale": None},
            "winter": {"detector_scale": None},
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mem_db():
    """In-memory SQLite session with the full schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def store() -> OperationalConfigStore:
    """Fresh OperationalConfigStore instance for each test."""
    return OperationalConfigStore()


@pytest.fixture
def api_client(mem_db: AsyncSession) -> TestClient:
    """TestClient wired to the in-memory DB; resets the global store after each test."""
    get_operational_config_store().reset()

    async def _override_db():
        yield mem_db

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    get_operational_config_store().reset()


# ---------------------------------------------------------------------------
# 1. Store merge semantics
# ---------------------------------------------------------------------------


class TestStoreMergeSemantics:
    """OperationalConfigStore.get() correctly deep-merges defaults with overrides."""

    def test_no_override_returns_tomato_defaults(self, store: OperationalConfigStore) -> None:
        """With no stored overrides, get() returns the tomato default profile."""
        merged = store.get(_TENANT, _GH)
        assert merged["crop"] == "tomato"
        assert merged["setpoints"] == CROP_SETPOINT_PROFILES["tomato"]
        assert merged["day_window"] == DEFAULT_DAY_WINDOW
        assert merged["season_map"] == DEFAULT_SEASON_MAP

    def test_crop_switch_re_derives_defaults(self, store: OperationalConfigStore) -> None:
        """Storing crop='cucumber' re-derives setpoint defaults from cucumber profile."""
        cucumber_cfg = _valid_config("cucumber")
        store.set_overrides(_TENANT, _GH, cucumber_cfg)
        merged = store.get(_TENANT, _GH)
        assert merged["crop"] == "cucumber"
        assert merged["setpoints"] == CROP_SETPOINT_PROFILES["cucumber"]

    def test_explicit_setpoint_override_survives(self, store: OperationalConfigStore) -> None:
        """Custom setpoint bands stored in the override survive the merge."""
        cfg = _valid_config("tomato")
        cfg["setpoints"]["temperature"]["day"] = [20.0, 30.0]
        store.set_overrides(_TENANT, _GH, cfg)
        merged = store.get(_TENANT, _GH)
        assert merged["setpoints"]["temperature"]["day"] == [20.0, 30.0]

    def test_crop_switch_does_not_carry_over_old_setpoints(
        self, store: OperationalConfigStore
    ) -> None:
        """Switching crop re-derives setpoints; old crop's defaults are not carried over."""
        tomato_cfg = _valid_config("tomato")
        store.set_overrides(_TENANT, _GH, tomato_cfg)

        cucumber_cfg = _valid_config("cucumber")
        store.set_overrides(_TENANT, _GH, cucumber_cfg)
        merged = store.get(_TENANT, _GH)
        # cucumber temperature day band differs from tomato
        assert merged["setpoints"]["temperature"]["day"] == CROP_SETPOINT_PROFILES["cucumber"]["temperature"]["day"]

    def test_season_map_override_survives(self, store: OperationalConfigStore) -> None:
        """Custom season_map is preserved after merge."""
        cfg = _valid_config("tomato")
        cfg["season_map"]["6"] = "winter"  # override one month
        store.set_overrides(_TENANT, _GH, cfg)
        merged = store.get(_TENANT, _GH)
        assert merged["season_map"]["6"] == "winter"

    def test_get_raw_overrides_empty_when_no_cache(self, store: OperationalConfigStore) -> None:
        """get_raw_overrides returns {} when nothing has been stored."""
        assert store.get_raw_overrides(_TENANT, _GH) == {}

    def test_get_raw_overrides_returns_stored_keys(self, store: OperationalConfigStore) -> None:
        """get_raw_overrides returns exactly the keys stored in the override."""
        cfg = _valid_config("tomato")
        store.set_overrides(_TENANT, _GH, cfg)
        raw = store.get_raw_overrides(_TENANT, _GH)
        assert set(raw.keys()) == {"crop", "setpoints", "day_window", "season_map", "seasonal_overrides"}


# ---------------------------------------------------------------------------
# 2. Validation rejection
# ---------------------------------------------------------------------------


class TestValidationRejections:
    """validate_operational_config raises HTTPException 422 for each rule violation."""

    def _assert_422(self, config: dict[str, Any]) -> None:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            validate_operational_config(config)
        assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_unknown_crop_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["crop"] = "artichoke"
        self._assert_422(cfg)

    def test_band_min_equals_max_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["setpoints"]["temperature"]["day"] = [22.0, 22.0]
        self._assert_422(cfg)

    def test_band_min_greater_than_max_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["setpoints"]["temperature"]["day"] = [30.0, 20.0]
        self._assert_422(cfg)

    def test_band_not_list_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["setpoints"]["temperature"]["day"] = {"lo": 18, "hi": 26}
        self._assert_422(cfg)

    def test_band_wrong_length_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["setpoints"]["temperature"]["day"] = [18.0]
        self._assert_422(cfg)

    def test_missing_month_in_season_map_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        del cfg["season_map"]["6"]
        self._assert_422(cfg)

    def test_invalid_season_value_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["season_map"]["6"] = "spring"
        self._assert_422(cfg)

    def test_day_window_hour_above_23_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["day_window"]["start_hour"] = 24
        self._assert_422(cfg)

    def test_day_window_hour_below_0_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["day_window"]["end_hour"] = -1
        self._assert_422(cfg)

    def test_day_window_equal_start_end_raises_422(self) -> None:
        cfg = _valid_config("tomato")
        cfg["day_window"]["start_hour"] = 6
        cfg["day_window"]["end_hour"] = 6
        self._assert_422(cfg)

    def test_valid_config_does_not_raise(self) -> None:
        """Sanity: a well-formed config passes validation without error."""
        validate_operational_config(_valid_config("tomato"))
        validate_operational_config(_valid_config("cucumber"))

    def test_null_night_band_is_valid(self) -> None:
        """night=null is explicitly allowed (day-band fallback)."""
        cfg = _valid_config("tomato")
        cfg["setpoints"]["temperature"]["night"] = None
        validate_operational_config(cfg)  # must not raise


# ---------------------------------------------------------------------------
# 3. Upsert round-trip through the store
# ---------------------------------------------------------------------------


class TestUpsertRoundTrip:
    """save_to_db persists and load_from_db restores the exact config."""

    @pytest.mark.asyncio
    async def test_save_then_load_returns_same_config(
        self, store: OperationalConfigStore, mem_db: AsyncSession
    ) -> None:
        cfg = _valid_config("cucumber")
        await store.save_to_db(_TENANT, _GH, cfg, mem_db)

        fresh = OperationalConfigStore()
        loaded = await fresh.load_from_db(_TENANT, _GH, mem_db)
        assert loaded["crop"] == "cucumber"
        assert loaded["setpoints"] == CROP_SETPOINT_PROFILES["cucumber"]

    @pytest.mark.asyncio
    async def test_second_save_overwrites_first(
        self, store: OperationalConfigStore, mem_db: AsyncSession
    ) -> None:
        await store.save_to_db(_TENANT, _GH, _valid_config("tomato"), mem_db)
        await store.save_to_db(_TENANT, _GH, _valid_config("cucumber"), mem_db)

        fresh = OperationalConfigStore()
        loaded = await fresh.load_from_db(_TENANT, _GH, mem_db)
        assert loaded["crop"] == "cucumber"

    @pytest.mark.asyncio
    async def test_db_row_created_on_first_save(
        self, store: OperationalConfigStore, mem_db: AsyncSession
    ) -> None:
        from sqlalchemy import select
        cfg = _valid_config("tomato")
        await store.save_to_db(_TENANT, _GH, cfg, mem_db)
        row = await mem_db.scalar(
            select(OperationalConfig).where(
                OperationalConfig.tenant_id == _TENANT,
                OperationalConfig.greenhouse_id == _GH,
            )
        )
        assert row is not None
        assert row.config_json["crop"] == "tomato"

    @pytest.mark.asyncio
    async def test_cache_updated_synchronously_after_save(
        self, store: OperationalConfigStore, mem_db: AsyncSession
    ) -> None:
        """Cache reflects the new config immediately after save_to_db returns."""
        cfg = _valid_config("cucumber")
        await store.save_to_db(_TENANT, _GH, cfg, mem_db)
        # No load_from_db needed — cache was already updated
        merged = store.get(_TENANT, _GH)
        assert merged["crop"] == "cucumber"


# ---------------------------------------------------------------------------
# 4. Cache invalidation on PUT
# ---------------------------------------------------------------------------


class TestCacheInvalidationOnPut:
    """PUT /api/config/operational must call invalidate_response_cache."""

    def test_put_calls_cache_invalidation(self, api_client: TestClient) -> None:
        with patch(
            "src.api.services.operational_config_service.invalidate_response_cache",
            new=AsyncMock(return_value=True),
        ) as mock_inv:
            response = api_client.put(
                "/api/config/operational",
                params={"tenant_id": _TENANT, "greenhouse_id": _GH},
                json=_valid_config("tomato"),
            )
        assert response.status_code == status.HTTP_200_OK, response.text
        mock_inv.assert_called_once_with("operational_config")

    def test_put_with_invalid_crop_does_not_call_cache(self, api_client: TestClient) -> None:
        """Validation failure before upsert: cache invalidation must NOT be called."""
        cfg = _valid_config("tomato")
        cfg["crop"] = "artichoke"
        with patch(
            "src.api.services.operational_config_service.invalidate_response_cache",
            new=AsyncMock(return_value=True),
        ) as mock_inv:
            response = api_client.put(
                "/api/config/operational",
                params={"tenant_id": _TENANT, "greenhouse_id": _GH},
                json=cfg,
            )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_inv.assert_not_called()


# ---------------------------------------------------------------------------
# 5. GET endpoint
# ---------------------------------------------------------------------------


class TestGetEndpoint:
    """GET /api/config/operational returns merged config + source annotations."""

    def test_get_returns_200_with_sources(self, api_client: TestClient) -> None:
        response = api_client.get(
            "/api/config/operational",
            params={"tenant_id": _TENANT, "greenhouse_id": _GH},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert "config" in body
        assert "sources" in body

    def test_get_fresh_tenant_sources_all_default(self, api_client: TestClient) -> None:
        """Before any PUT, all top-level keys are sourced from code defaults."""
        response = api_client.get(
            "/api/config/operational",
            params={"tenant_id": _TENANT, "greenhouse_id": _GH},
        )
        body = response.json()
        assert all(v == "default" for v in body["sources"].values()), body["sources"]

    def test_get_after_put_sources_all_override(self, api_client: TestClient) -> None:
        """After a PUT, all top-level keys stored in the override are 'override'."""
        api_client.put(
            "/api/config/operational",
            params={"tenant_id": _TENANT, "greenhouse_id": _GH},
            json=_valid_config("tomato"),
        )
        response = api_client.get(
            "/api/config/operational",
            params={"tenant_id": _TENANT, "greenhouse_id": _GH},
        )
        body = response.json()
        # All five top-level keys were stored in the PUT, so all are 'override'
        for key in ("crop", "setpoints", "day_window", "season_map", "seasonal_overrides"):
            assert body["sources"].get(key) == "override", f"{key} expected 'override'"

    def test_crop_profiles_endpoint(self, api_client: TestClient) -> None:
        """GET /api/config/crop-profiles returns the CROP_SETPOINT_PROFILES dict."""
        response = api_client.get("/api/config/crop-profiles")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert "tomato" in body
        assert "cucumber" in body


# ---------------------------------------------------------------------------
# 6. Alembic migration upgrade / downgrade on SQLite
# ---------------------------------------------------------------------------


class TestMigration:
    """018_add_operational_config upgrades and downgrades cleanly on SQLite."""

    def test_upgrade_creates_table_and_downgrade_drops_it(self, tmp_path) -> None:
        """Run alembic upgrade to 018 then downgrade to 017 on a scratch DB."""
        import os
        from alembic.config import Config
        from alembic import command
        import sqlalchemy

        db_path = tmp_path / "test_migration.db"
        db_url = f"sqlite:///{db_path}"

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)

        # Upgrade to head (which includes 018)
        command.upgrade(cfg, "018")

        # Verify the table exists
        engine = sqlalchemy.create_engine(db_url)
        inspector = sqlalchemy.inspect(engine)
        assert "operational_config" in inspector.get_table_names(), (
            "operational_config table should exist after upgrade to 018"
        )
        cols = {c["name"] for c in inspector.get_columns("operational_config")}
        assert {"tenant_id", "greenhouse_id", "config_json", "updated_at"} <= cols
        engine.dispose()

        # Downgrade back to 017
        command.downgrade(cfg, "017")

        engine = sqlalchemy.create_engine(db_url)
        inspector = sqlalchemy.inspect(engine)
        assert "operational_config" not in inspector.get_table_names(), (
            "operational_config table should be gone after downgrade to 017"
        )
        engine.dispose()
