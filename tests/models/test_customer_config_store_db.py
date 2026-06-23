"""
Tests for CustomerConfigStore DB persistence paths.

Targets uncovered lines in src/models/customer_config_store.py:
  71->73  – promote() with non-None snapshot stored
  88-90   – clear() with specific customer_id
  98-107  – load_from_db() with existing DB row
  113-133 – save_to_db() create new row / update existing row
  141     – get_config_store() singleton
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base
from src.models.customer_config_store import (
    CustomerConfigStore,
    default_customer_config,
    get_config_store,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def store() -> CustomerConfigStore:
    return CustomerConfigStore()


# ---------------------------------------------------------------------------
# default_customer_config()
# ---------------------------------------------------------------------------


class TestDefaultCustomerConfig:
    def test_returns_dict_with_all_keys(self):
        cfg = default_customer_config()
        assert "ANOMALY_THRESHOLD" in cfg
        assert "ENSEMBLE_DETECTOR_WEIGHTS" in cfg
        assert "ENSEMBLE_ANOMALY_THRESHOLD" in cfg
        assert "ENSEMBLE_MIN_AGREEMENT" in cfg

    def test_ensemble_weights_is_dict(self):
        cfg = default_customer_config()
        assert isinstance(cfg["ENSEMBLE_DETECTOR_WEIGHTS"], dict)

    def test_returns_independent_copy_each_call(self):
        a = default_customer_config()
        b = default_customer_config()
        a["ANOMALY_THRESHOLD"] = 999
        assert b["ANOMALY_THRESHOLD"] != 999


# ---------------------------------------------------------------------------
# CustomerConfigStore.get()
# ---------------------------------------------------------------------------


class TestCustomerConfigStoreGet:
    def test_returns_defaults_for_unknown_customer(self, store):
        cfg = store.get("unknown_customer")
        expected = default_customer_config()
        assert cfg["ANOMALY_THRESHOLD"] == expected["ANOMALY_THRESHOLD"]

    def test_override_applied(self, store):
        store._overrides["c1"] = {"ANOMALY_THRESHOLD": 0.99}
        cfg = store.get("c1")
        assert cfg["ANOMALY_THRESHOLD"] == pytest.approx(0.99)

    def test_ensemble_weights_override_is_deep_copied(self, store):
        """Returned weights dict must be independent of stored override."""
        weights = {"lstm_vae": 0.5, "isolation_forest": 0.5}
        store._overrides["c2"] = {"ENSEMBLE_DETECTOR_WEIGHTS": weights}
        cfg = store.get("c2")
        cfg["ENSEMBLE_DETECTOR_WEIGHTS"]["lstm_vae"] = 0.0
        # Original override must be unchanged
        assert store._overrides["c2"]["ENSEMBLE_DETECTOR_WEIGHTS"]["lstm_vae"] == 0.5


# ---------------------------------------------------------------------------
# CustomerConfigStore.promote()  – line 71->73 (snapshot branch)
# ---------------------------------------------------------------------------


class TestPromote:
    def test_promote_without_snapshot(self, store):
        proposed = {"ANOMALY_THRESHOLD": 0.7}
        store.promote("c_nsnap", proposed)
        assert store._overrides["c_nsnap"]["ANOMALY_THRESHOLD"] == pytest.approx(0.7)
        assert "c_nsnap" not in store._snapshots

    def test_promote_with_snapshot_stores_snapshot(self, store):
        """Line 71->73: snapshot is not None → stored in _snapshots."""
        proposed = {"ANOMALY_THRESHOLD": 0.7}
        snapshot = {"ANOMALY_THRESHOLD": 0.5}
        store.promote("c_snap", proposed, snapshot=snapshot)

        assert "c_snap" in store._snapshots
        assert store._snapshots["c_snap"]["ANOMALY_THRESHOLD"] == pytest.approx(0.5)

    def test_promote_deep_copies_snapshot(self, store):
        """Mutating the original snapshot dict must not affect the stored copy."""
        snapshot = {"ANOMALY_THRESHOLD": 0.5}
        store.promote("c_deep", {"ANOMALY_THRESHOLD": 0.8}, snapshot=snapshot)

        snapshot["ANOMALY_THRESHOLD"] = 999
        assert store._snapshots["c_deep"]["ANOMALY_THRESHOLD"] == pytest.approx(0.5)

    def test_promote_deep_copies_proposed(self, store):
        """Mutating proposed dict after promote must not affect stored override."""
        proposed = {"ANOMALY_THRESHOLD": 0.75}
        store.promote("c_prop", proposed)

        proposed["ANOMALY_THRESHOLD"] = 999
        assert store._overrides["c_prop"]["ANOMALY_THRESHOLD"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# CustomerConfigStore.rollback()
# ---------------------------------------------------------------------------


class TestRollback:
    def test_rollback_with_no_snapshot_returns_false(self, store):
        assert store.rollback("no_snap") is False

    def test_rollback_with_snapshot_returns_true(self, store):
        store._snapshots["rb1"] = {"ANOMALY_THRESHOLD": 0.5}
        store._overrides["rb1"] = {"ANOMALY_THRESHOLD": 0.9}
        result = store.rollback("rb1")
        assert result is True

    def test_rollback_restores_override_from_snapshot(self, store):
        snapshot = {"ANOMALY_THRESHOLD": 0.5}
        store._snapshots["rb2"] = snapshot.copy()
        store._overrides["rb2"] = {"ANOMALY_THRESHOLD": 0.9}
        store.rollback("rb2")
        assert store._overrides["rb2"]["ANOMALY_THRESHOLD"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# CustomerConfigStore.clear()  – lines 88-90 (specific customer_id branch)
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_all_removes_all_overrides(self, store):
        """clear(None) removes everything."""
        store._overrides["c1"] = {}
        store._overrides["c2"] = {}
        store.clear()  # customer_id=None by default
        assert store._overrides == {}
        assert store._snapshots == {}

    def test_clear_specific_customer_removes_only_that_customer(self, store):
        """Lines 88-90: clear(customer_id) removes only the named customer."""
        store._overrides["c1"] = {"ANOMALY_THRESHOLD": 0.6}
        store._overrides["c2"] = {"ANOMALY_THRESHOLD": 0.7}
        store._snapshots["c1"] = {"ANOMALY_THRESHOLD": 0.5}

        store.clear("c1")

        assert "c1" not in store._overrides
        assert "c1" not in store._snapshots
        assert "c2" in store._overrides

    def test_clear_specific_customer_no_error_if_not_present(self, store):
        """Clearing a non-existent customer does not raise."""
        store.clear("nonexistent")  # must not raise

    def test_clear_specific_customer_does_not_touch_others(self, store):
        """Other customers' overrides survive a specific clear."""
        store._overrides["keeper"] = {"ANOMALY_THRESHOLD": 0.88}
        store._overrides["remove_me"] = {}
        store.clear("remove_me")
        assert store._overrides["keeper"]["ANOMALY_THRESHOLD"] == pytest.approx(0.88)


# ---------------------------------------------------------------------------
# CustomerConfigStore.load_from_db()  – lines 98-107
# ---------------------------------------------------------------------------


class TestLoadFromDb:
    @pytest.mark.asyncio
    async def test_load_returns_defaults_when_no_row(
        self, store, db_session: AsyncSession
    ) -> None:
        """load_from_db returns default config when no DB row exists."""
        cfg = await store.load_from_db("missing_cust", db_session)
        expected = default_customer_config()
        assert cfg["ANOMALY_THRESHOLD"] == expected["ANOMALY_THRESHOLD"]

    @pytest.mark.asyncio
    async def test_load_returns_stored_override_when_row_exists(
        self, store, db_session: AsyncSession
    ) -> None:
        """load_from_db loads overrides from CustomerModelConfig row."""
        from src.db.models import CustomerModelConfig
        import datetime

        row = CustomerModelConfig(
            customer_id="db_load_cust",
            config_json={"ANOMALY_THRESHOLD": 0.77},
            updated_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db_session.add(row)
        await db_session.commit()

        cfg = await store.load_from_db("db_load_cust", db_session)
        assert cfg["ANOMALY_THRESHOLD"] == pytest.approx(0.77)

    @pytest.mark.asyncio
    async def test_load_caches_override_in_memory(
        self, store, db_session: AsyncSession
    ) -> None:
        """load_from_db populates _overrides in-memory for subsequent get() calls."""
        from src.db.models import CustomerModelConfig
        import datetime

        row = CustomerModelConfig(
            customer_id="cache_cust",
            config_json={"ANOMALY_THRESHOLD": 0.65},
            updated_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db_session.add(row)
        await db_session.commit()

        await store.load_from_db("cache_cust", db_session)
        # In-memory cache should now reflect the loaded value
        assert store._overrides["cache_cust"]["ANOMALY_THRESHOLD"] == pytest.approx(0.65)


# ---------------------------------------------------------------------------
# CustomerConfigStore.save_to_db()  – lines 113-133
# ---------------------------------------------------------------------------


class TestSaveToDb:
    @pytest.mark.asyncio
    async def test_save_creates_new_row(self, store, db_session: AsyncSession) -> None:
        """save_to_db creates a CustomerModelConfig row when none exists."""
        from sqlalchemy import select
        from src.db.models import CustomerModelConfig

        store._overrides["new_cust"] = {"ANOMALY_THRESHOLD": 0.82}
        await store.save_to_db("new_cust", db_session)

        result = await db_session.execute(
            select(CustomerModelConfig).where(
                CustomerModelConfig.customer_id == "new_cust"
            )
        )
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.config_json["ANOMALY_THRESHOLD"] == pytest.approx(0.82)

    @pytest.mark.asyncio
    async def test_save_updates_existing_row(
        self, store, db_session: AsyncSession
    ) -> None:
        """save_to_db updates the config_json of an existing row."""
        from sqlalchemy import select
        from src.db.models import CustomerModelConfig
        import datetime

        initial = CustomerModelConfig(
            customer_id="update_cust",
            config_json={"ANOMALY_THRESHOLD": 0.5},
            updated_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db_session.add(initial)
        await db_session.commit()

        store._overrides["update_cust"] = {"ANOMALY_THRESHOLD": 0.9}
        await store.save_to_db("update_cust", db_session)

        result = await db_session.execute(
            select(CustomerModelConfig).where(
                CustomerModelConfig.customer_id == "update_cust"
            )
        )
        row = result.scalar_one()
        assert row.config_json["ANOMALY_THRESHOLD"] == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_save_empty_overrides_creates_row(
        self, store, db_session: AsyncSession
    ) -> None:
        """save_to_db with no override for customer creates row with empty dict."""
        from sqlalchemy import select
        from src.db.models import CustomerModelConfig

        # No override for 'empty_cust' → saves {}
        await store.save_to_db("empty_cust", db_session)

        result = await db_session.execute(
            select(CustomerModelConfig).where(
                CustomerModelConfig.customer_id == "empty_cust"
            )
        )
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.config_json == {}


# ---------------------------------------------------------------------------
# get_config_store() singleton  – line 141
# ---------------------------------------------------------------------------


class TestGetConfigStoreSingleton:
    def test_returns_customer_config_store_instance(self):
        """get_config_store() returns a CustomerConfigStore instance."""
        store = get_config_store()
        assert isinstance(store, CustomerConfigStore)

    def test_same_instance_returned_on_repeated_calls(self):
        """get_config_store() is a module-level singleton."""
        a = get_config_store()
        b = get_config_store()
        assert a is b
