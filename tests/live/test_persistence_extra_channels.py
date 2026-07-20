"""Tests for persistence of events with extra channels (soil_moisture, root_zone_temp, vpd).

Covers (pre-dual-write, preserved):
    - events with extra channels flush without error; extra channels are NOT
      persisted to SensorReading (only the 4 canonical channels + anomaly_score);
      incomplete events still skipped correctly when extra channels are present.

Covers (dual-write, Phase 11):
    - _event_to_raw_row: full channel dict incl. vpd; extras-only → raw row
      returned; empty/all-NaN → None; NaN stripped not zeroed.
    - Batcher flush: canonical + raw written in same transaction; extras-only
      writes raw but skips canonical; duplicate timestamp no-op on both tables;
      mid-flush failure requeues events covering both tables.
    - NaN guard: non-finite values never reach the JSON column in raw_readings.
    - Read-path integration: after flush, soil_moisture/root_zone_temp/vpd
      surface in fetch_merged_readings output with anomaly_score attached.
"""

from __future__ import annotations

import asyncio
import datetime
import math
from datetime import timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base, RawReading, SensorReading
from src.live.events import LiveReadingEvent
from src.live.persistence import PersistenceBatcher, _event_to_raw_row, _event_to_row


# ---------------------------------------------------------------------------
# Constants / Helpers
# ---------------------------------------------------------------------------

_TENANT = "persist_extra"
_GH = "gh_extra"


def _ts(second: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 7, 18, 12, minute, second, tzinfo=timezone.utc)


def _make_event_with_extras(
    second: int = 0,
    include_vpd: bool = True,
    include_soil: bool = True,
    include_rzt: bool = True,
    anomaly_score: float | None = None,
) -> LiveReadingEvent:
    """Build a reading with the 4 canonical channels + extra channels."""
    channels = {
        "temperature": 25.0,
        "humidity": 65.0,
        "co2": 400.0,
        "light": 500.0,
    }
    sources = {
        "temperature": "src1",
        "humidity": "src1",
        "co2": "src2",
        "light": "src1",
    }
    if include_vpd:
        channels["vpd"] = 1.27
        sources["vpd"] = "derived"
    if include_soil:
        channels["soil_moisture"] = 72.0
        sources["soil_moisture"] = "src3"
    if include_rzt:
        channels["root_zone_temp"] = 21.5
        sources["root_zone_temp"] = "src3"

    return LiveReadingEvent(
        seq="",
        timestamp=_ts(second),
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        channels=channels,
        sources=sources,
        anomaly_score=anomaly_score,
    )


def _make_extras_only_event(second: int = 0) -> LiveReadingEvent:
    """Build a reading with ONLY extra channels — no canonical 4."""
    return LiveReadingEvent(
        seq="",
        timestamp=_ts(second),
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        channels={
            "vpd": 1.27,
            "soil_moisture": 72.0,
            "root_zone_temp": 21.5,
        },
        sources={"vpd": "derived", "soil_moisture": "src3", "root_zone_temp": "src3"},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine) -> async_sessionmaker:
    return async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False,
    )


@pytest_asyncio.fixture
async def batcher(session_factory):
    b = PersistenceBatcher(flush_interval=0.05, session_factory=session_factory)
    yield b
    await b.close()


async def _count_canonical(session_factory, tenant: str = _TENANT) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count(SensorReading.id)).where(SensorReading.tenant_id == tenant)
        )
        return result.scalar() or 0


async def _count_raw(session_factory, tenant: str = _TENANT) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count(RawReading.id)).where(RawReading.tenant_id == tenant)
        )
        return result.scalar() or 0


# ---------------------------------------------------------------------------
# _event_to_row — extra channels ignored (pre-existing tests, preserved)
# ---------------------------------------------------------------------------


class TestEventToRowExtraChannels:

    def test_row_includes_canonical_channels_only(self) -> None:
        """Extra channels (vpd, soil_moisture, root_zone_temp) are not in the row dict."""
        event = _make_event_with_extras()
        row = _event_to_row(event)
        assert row is not None
        assert "temperature" in row
        assert "humidity" in row
        assert "co2" in row
        assert "light" in row
        # Extra channels should NOT be in the canonical row
        assert "vpd" not in row
        assert "soil_moisture" not in row
        assert "root_zone_temp" not in row

    def test_canonical_values_correct(self) -> None:
        """The 4 canonical channel values are correctly extracted."""
        event = _make_event_with_extras()
        row = _event_to_row(event)
        assert row is not None
        assert abs(row["temperature"] - 25.0) < 1e-6
        assert abs(row["humidity"] - 65.0) < 1e-6
        assert abs(row["co2"] - 400.0) < 1e-6
        assert abs(row["light"] - 500.0) < 1e-6

    def test_completeness_check_ignores_extras(self) -> None:
        """Event is complete if 4 canonical channels present, regardless of extras."""
        event = _make_event_with_extras(include_vpd=True, include_soil=True, include_rzt=True)
        row = _event_to_row(event)
        assert row is not None

    def test_missing_canonical_still_rejected(self) -> None:
        """Event with extras but missing a canonical channel is still rejected."""
        event = LiveReadingEvent(
            seq="",
            timestamp=_ts(0),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": 25.0,
                "humidity": 65.0,
                # co2 missing!
                "light": 500.0,
                "vpd": 1.27,
                "soil_moisture": 72.0,
                "root_zone_temp": 21.5,
            },
            sources={},
        )
        row = _event_to_row(event)
        assert row is None


# ---------------------------------------------------------------------------
# _event_to_raw_row — unit tests (Phase 11)
# ---------------------------------------------------------------------------


class TestEventToRawRow:

    def test_full_event_returns_full_channel_dict_incl_vpd(self) -> None:
        """Raw row data contains ALL channels — canonical + extras + vpd."""
        event = _make_event_with_extras()
        row = _event_to_raw_row(event)
        assert row is not None
        data = row["data"]
        # All 7 channels present
        assert abs(data["temperature"] - 25.0) < 1e-6
        assert abs(data["humidity"] - 65.0) < 1e-6
        assert abs(data["co2"] - 400.0) < 1e-6
        assert abs(data["light"] - 500.0) < 1e-6
        assert abs(data["vpd"] - 1.27) < 1e-6
        assert abs(data["soil_moisture"] - 72.0) < 1e-6
        assert abs(data["root_zone_temp"] - 21.5) < 1e-6

    def test_raw_row_source_is_live(self) -> None:
        """The raw row's source field is set to 'live'."""
        row = _event_to_raw_row(_make_event_with_extras())
        assert row is not None
        assert row["source"] == "live"

    def test_raw_row_metadata_matches_event(self) -> None:
        """tenant_id, greenhouse_id, timestamp are copied verbatim."""
        event = _make_event_with_extras(second=42)
        row = _event_to_raw_row(event)
        assert row is not None
        assert row["tenant_id"] == _TENANT
        assert row["greenhouse_id"] == _GH
        assert row["timestamp"] == _ts(42)

    def test_extras_only_event_returns_raw_row(self) -> None:
        """An extras-only event (no canonical channels) still produces a raw row."""
        event = _make_extras_only_event()
        row = _event_to_raw_row(event)
        assert row is not None
        assert "vpd" in row["data"]
        assert "soil_moisture" in row["data"]
        assert "root_zone_temp" in row["data"]

    def test_empty_channels_returns_none(self) -> None:
        """When all channels are absent, _event_to_raw_row returns None."""
        event = LiveReadingEvent.model_construct(
            seq="",
            timestamp=_ts(0),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={},
            sources={},
            anomaly_score=None,
        )
        assert _event_to_raw_row(event) is None

    def test_all_nan_channels_returns_none(self) -> None:
        """When every channel value is NaN, sanitize_channels produces an empty dict → None."""
        event = LiveReadingEvent.model_construct(
            seq="",
            timestamp=_ts(0),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"temperature": float("nan"), "vpd": float("nan")},
            sources={},
            anomaly_score=None,
        )
        assert _event_to_raw_row(event) is None

    def test_nan_channel_stripped_not_zeroed(self) -> None:
        """NaN in a channel is stripped from data (not 0-filled — full-fidelity guard).

        The canonical row uses 0.0 fallback (sensor reported invalid value), but
        the raw row must NOT — a missing key is preferable to a fabricated 0.0
        that could corrupt full-fidelity analytics.
        """
        event = LiveReadingEvent.model_construct(
            seq="",
            timestamp=_ts(1),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": float("nan"),
                "humidity": 65.0,
                "co2": 400.0,
                "light": 500.0,
            },
            sources={},
            anomaly_score=None,
        )
        row = _event_to_raw_row(event)
        # Row is not None because some channels survived sanitization
        assert row is not None
        data = row["data"]
        # NaN temperature should be ABSENT, not 0.0
        assert "temperature" not in data
        # Finite channels remain
        assert abs(data["humidity"] - 65.0) < 1e-6

    def test_inf_channel_stripped_not_in_data(self) -> None:
        """Infinity is stripped; the JSON column never receives a non-finite value."""
        event = LiveReadingEvent.model_construct(
            seq="",
            timestamp=_ts(2),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"vpd": float("inf"), "soil_moisture": 72.0},
            sources={},
            anomaly_score=None,
        )
        row = _event_to_raw_row(event)
        assert row is not None
        data = row["data"]
        assert "vpd" not in data
        assert math.isfinite(data["soil_moisture"])


# ---------------------------------------------------------------------------
# Flush with extra channels — pre-existing tests, preserved
# ---------------------------------------------------------------------------


class TestFlushWithExtraChannels:

    @pytest.mark.asyncio
    async def test_flush_succeeds_with_extra_channels(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """Events with extra channels flush without error."""
        await batcher.add(_make_event_with_extras(second=0))
        await batcher.add(_make_event_with_extras(second=1))
        await batcher._flush()

        # Verify canonical rows written
        async with session_factory() as session:
            rows = (await session.execute(
                select(SensorReading).where(SensorReading.tenant_id == _TENANT)
            )).scalars().all()

        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_persisted_row_has_only_canonical_columns(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """Persisted SensorReading row contains only the canonical 4 channels, not extras."""
        await batcher.add(_make_event_with_extras(second=10, anomaly_score=0.5))
        await batcher._flush()

        async with session_factory() as session:
            row = (await session.execute(
                select(SensorReading).where(SensorReading.tenant_id == _TENANT)
            )).scalars().first()

        assert row is not None
        assert abs(row.temperature - 25.0) < 1e-6
        assert abs(row.humidity - 65.0) < 1e-6
        assert abs(row.co2 - 400.0) < 1e-6
        assert abs(row.light - 500.0) < 1e-6
        assert abs(row.anomaly_score - 0.5) < 1e-6

        # SensorReading model should NOT have vpd/soil_moisture/root_zone_temp columns
        assert not hasattr(row, "vpd")
        assert not hasattr(row, "soil_moisture")
        assert not hasattr(row, "root_zone_temp")

    @pytest.mark.asyncio
    async def test_event_with_only_extras_no_canonical_row(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """An event with only extra channels produces no canonical (SensorReading) row."""
        event = _make_extras_only_event(second=20)
        await batcher.add(event)
        await batcher._flush()

        assert await _count_canonical(session_factory) == 0
        assert batcher._skipped_incomplete_count >= 1


# ---------------------------------------------------------------------------
# Dual-write: both SensorReading + RawReading in one transaction (Phase 11)
# ---------------------------------------------------------------------------


class TestRawRowDualWrite:

    @pytest.mark.asyncio
    async def test_complete_event_writes_both_tables(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """A complete event (all 4 canonical + extras) writes one row to each table."""
        await batcher.add(_make_event_with_extras(second=0, anomaly_score=0.15))
        await batcher._flush()

        assert await _count_canonical(session_factory) == 1
        assert await _count_raw(session_factory) == 1

    @pytest.mark.asyncio
    async def test_raw_row_contains_full_channel_dict_after_flush(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """After flush, the raw row's data dict includes canonical + vpd + extras."""
        await batcher.add(_make_event_with_extras(second=5))
        await batcher._flush()

        async with session_factory() as session:
            raw = (await session.execute(
                select(RawReading).where(RawReading.tenant_id == _TENANT)
            )).scalars().first()

        assert raw is not None
        assert raw.source == "live"
        data = raw.data
        assert abs(data["temperature"] - 25.0) < 1e-6
        assert abs(data["vpd"] - 1.27) < 1e-6
        assert abs(data["soil_moisture"] - 72.0) < 1e-6
        assert abs(data["root_zone_temp"] - 21.5) < 1e-6

    @pytest.mark.asyncio
    async def test_extras_only_event_writes_raw_skips_canonical(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """Extras-only bucket persists the raw row but skips the canonical row."""
        await batcher.add(_make_extras_only_event(second=30))
        await batcher._flush()

        # Canonical skipped (no canonical channels)
        assert await _count_canonical(session_factory) == 0
        assert batcher._skipped_incomplete_count >= 1
        # Raw row still written (no completeness requirement)
        assert await _count_raw(session_factory) == 1

    @pytest.mark.asyncio
    async def test_duplicate_timestamp_noop_on_both_tables(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """Duplicate timestamp is silently ignored on both tables (ON CONFLICT DO NOTHING)."""
        event = _make_event_with_extras(second=50)
        await batcher.add(event)
        await batcher._flush()

        # Second flush with same event — both tables already have a row for this ts.
        await batcher.add(event)
        await batcher._flush()

        # Still exactly 1 row each — no error, no duplicate
        assert await _count_canonical(session_factory) == 1
        assert await _count_raw(session_factory) == 1

    @pytest.mark.asyncio
    async def test_flush_failure_requeues_events_for_both_tables(self) -> None:
        """On flush error, events are requeueed so both tables get another attempt."""

        class _FailingSession:
            async def __aenter__(self):
                raise RuntimeError("DB down")
            async def __aexit__(self, *args):
                pass

        def failing_factory():
            return _FailingSession()

        b = PersistenceBatcher(flush_interval=9999.0, session_factory=failing_factory)
        await b.add(_make_event_with_extras(second=0))
        await b.add(_make_event_with_extras(second=1))

        await b._flush()  # fails — no rows written but no data loss

        # Both events requeueed for next flush cycle (both tables covered on retry)
        assert len(b._buffer) == 2
        await b.close()

    @pytest.mark.asyncio
    async def test_both_tables_in_one_session(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """Both inserts share one session — a single commit covers both tables."""
        sessions_opened: list[int] = [0]

        original_factory = session_factory

        def tracking_factory():
            sessions_opened[0] += 1
            return original_factory()

        b = PersistenceBatcher(flush_interval=9999.0, session_factory=tracking_factory)
        await b.add(_make_event_with_extras(second=0))
        await b.add(_make_event_with_extras(second=1))
        await b._flush()

        # Both canonical and raw inserts used exactly ONE session (one transaction)
        assert sessions_opened[0] == 1
        await b.close()


# ---------------------------------------------------------------------------
# NaN guard — non-finite values never reach raw_readings.data (Phase 11)
# ---------------------------------------------------------------------------


class TestNanGuardRawRow:

    @pytest.mark.asyncio
    async def test_nan_channel_stripped_from_raw_data_column(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """NaN in a channel is stripped from raw_readings.data, not written as NaN/0."""
        # Use model_construct to inject NaN (bypasses Pydantic finite validator).
        event = LiveReadingEvent.model_construct(
            seq="",
            timestamp=_ts(0, minute=10),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": float("nan"),  # NaN — must be stripped
                "humidity": 65.0,
                "co2": 400.0,
                "light": 500.0,
                "vpd": 1.27,
            },
            sources={},
            anomaly_score=None,
        )
        await batcher.add(event)
        await batcher._flush()

        async with session_factory() as session:
            raw = (await session.execute(
                select(RawReading).where(RawReading.tenant_id == _TENANT)
            )).scalars().first()

        assert raw is not None
        data = raw.data
        # NaN temperature must NOT appear in the JSON column
        assert "temperature" not in data
        # Finite channels present
        assert abs(data["vpd"] - 1.27) < 1e-6
        # All remaining values are finite (Postgres NaN gap guard verified)
        for value in data.values():
            assert math.isfinite(value), f"Non-finite value leaked into data column: {value}"

    @pytest.mark.asyncio
    async def test_inf_channel_stripped_from_raw_data_column(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """Inf in a channel is stripped from raw_readings.data."""
        event = LiveReadingEvent.model_construct(
            seq="",
            timestamp=_ts(0, minute=11),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": 25.0,
                "humidity": float("inf"),  # Inf — must be stripped
                "co2": 400.0,
                "light": 500.0,
            },
            sources={},
            anomaly_score=None,
        )
        await batcher.add(event)
        await batcher._flush()

        async with session_factory() as session:
            raw = (await session.execute(
                select(RawReading).where(RawReading.tenant_id == _TENANT)
            )).scalars().first()

        assert raw is not None
        data = raw.data
        assert "humidity" not in data
        assert abs(data["temperature"] - 25.0) < 1e-6
        for value in data.values():
            assert math.isfinite(value), f"Non-finite value leaked into data column: {value}"


# ---------------------------------------------------------------------------
# Read-path integration — extras surface in fetch_merged_readings (Phase 11)
# ---------------------------------------------------------------------------


class TestReadPathIntegration:

    @pytest.mark.asyncio
    async def test_extras_surface_in_merged_readings_with_scores(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """After dual-write flush, fetch_merged_readings returns soil_moisture/vpd/
        root_zone_temp with anomaly_score from the canonical table attached.

        Verifies the zero-read-path-change claim: the existing merge/read path
        already handles RawReading rows written by the CSV upload flow, so live
        raw rows from the batcher surface identically.
        """
        from src.api.services.readings_service import fetch_merged_readings

        ts = _ts(5, minute=15)

        # Write a complete event with extras and a known anomaly_score.
        event = _make_event_with_extras(second=5, anomaly_score=0.42)
        # Use a distinct minute to avoid timestamp collisions with other test methods.
        event = LiveReadingEvent(
            seq="",
            timestamp=ts,
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": 25.0,
                "humidity": 65.0,
                "co2": 400.0,
                "light": 500.0,
                "vpd": 1.27,
                "soil_moisture": 72.0,
                "root_zone_temp": 21.5,
            },
            sources={
                "temperature": "src1",
                "humidity": "src1",
                "co2": "src2",
                "light": "src1",
                "vpd": "derived",
                "soil_moisture": "src3",
                "root_zone_temp": "src3",
            },
            anomaly_score=0.42,
        )
        await batcher.add(event)
        await batcher._flush()

        # Fetch merged readings and verify extras surface with score attached.
        async with session_factory() as session:
            rows = await fetch_merged_readings(
                session,
                tenant_id=_TENANT,
                greenhouse_id=_GH,
            )

        assert len(rows) >= 1, "Expected at least one row from fetch_merged_readings"

        # Take the row — there should be exactly one for this tenant/greenhouse.
        target = rows[-1]  # newest row (ascending sort; last = most recent)

        assert target is not None, f"Expected row with ts={ts} in merged output, got: {rows}"

        # Extra channels surfaced from raw_readings.data
        assert "soil_moisture" in target, "soil_moisture should surface from raw row"
        assert "root_zone_temp" in target, "root_zone_temp should surface from raw row"
        assert "vpd" in target, "vpd should surface from raw row"

        # Anomaly score attached from canonical table
        assert target.get("anomaly_score") is not None, "anomaly_score should be merged from SensorReading"
        assert abs(float(target["anomaly_score"]) - 0.42) < 1e-3
