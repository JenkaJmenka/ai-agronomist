"""Acceptance: the ``anomaly_episodes`` rows an upload writes (Task 3, Part C).

Covers items 1-5, 7, 8, 9 and 18 of ``docs/handoff/task3_part_c.md``: what the
stuck-fault detector persists for ``tests/fixtures/native_dec_stuck_fault.csv``
(fixture A), read back FROM THE DATABASE rather than from the upload response.

Why the database and not the response: these are two different concepts that
share the English word "episode" (``docs/handoff/task3_4_spec.md`` §1). The
response's episodes are grouped from alerts in memory by
``src/models/episode_grouper.py`` and persist nothing; the rows asserted here
are written at detection time by
``src/api/services/anomaly_episode_writer.py`` and are the durable record.
``tests/workers/test_stuck_async_parity.py`` covers the response side.

co2 is asserted to produce ZERO episodes. Fixture A has no co2 column at all:
co2 is an optional sensor imputed with a constant fill, so it reads as frozen
for the whole file, and all three upload entry points build
``StuckCarry(skip_channels=frozenset(imputed_channels))`` to suppress exactly
that false alert. A harness that builds a bare ``StuckCarry()`` instead
manufactures a phantom file-long co2 episode — so every count here comes from
a real upload path, never from a hand-built carry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.cold_run_upload import run_cold_run_upload
from src.api.services.upload_producer import produce_upload_portions
from src.config import LATENT_DIM, STUCK_WALLCLOCK_MIN_HOURS
from src.db.models import AnomalyEpisodeRecord, Base, SensorReading
from src.vendors.csv_adapter import CSVAdapter
from src.workers.upload_consumer import handle_upload_finalize, handle_upload_portion

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_A = FIXTURE_DIR / "native_dec_stuck_fault.csv"
FIXTURE_CLEARSKY = FIXTURE_DIR / "native_july_clearsky.csv"

TENANT = "episode_persistence"
GH_ID = "gh_episode_persistence"
CHUNK_ROWS = 500

# Item 7: the pinned span of every episode fixture A produces, measured
# through both upload paths. frozen_sample_count is observed frozen samples;
# raw_slot_count is EXPECTED slots at the freeze's pinned cadence
# ((end - onset) / cadence + 1), so the two differ exactly where readings are
# missing for the channel — including whole rows CSVAdapter dropped over a
# different sensor's null (70 of them in this fixture).
EXPECTED_EPISODES: tuple[dict[str, Any], ...] = (
    {
        "channel": "humidity",
        "onset_ts": "2026-12-14T00:00:00+00:00",
        "end_ts": "2026-12-15T10:50:00+00:00",
        "release_ts": "2026-12-15T10:55:00+00:00",
        "detected_ts": "2026-12-14T06:00:00+00:00",
        "duration_seconds": 125400,
        "frozen_sample_count": 419,
        "raw_slot_count": 419,
        "severity": "urgent",
        "is_ongoing": False,
    },
    {
        "channel": "humidity",
        "onset_ts": "2026-12-15T11:05:00+00:00",
        "end_ts": "2026-12-16T09:10:00+00:00",
        "release_ts": "2026-12-16T09:15:00+00:00",
        "detected_ts": "2026-12-15T17:05:00+00:00",
        "duration_seconds": 79500,
        "frozen_sample_count": 266,
        "raw_slot_count": 266,
        "severity": "urgent",
        "is_ongoing": False,
    },
    {
        "channel": "humidity",
        "onset_ts": "2026-12-16T11:20:00+00:00",
        "end_ts": "2026-12-18T11:15:00+00:00",
        "release_ts": "2026-12-18T11:20:00+00:00",
        "detected_ts": "2026-12-16T17:20:00+00:00",
        "duration_seconds": 172500,
        "frozen_sample_count": 576,
        "raw_slot_count": 576,
        "severity": "urgent",
        "is_ongoing": False,
    },
    {
        "channel": "humidity",
        "onset_ts": "2026-12-18T13:00:00+00:00",
        "end_ts": "2026-12-25T08:50:00+00:00",
        "release_ts": "2026-12-25T08:55:00+00:00",
        "detected_ts": "2026-12-18T19:00:00+00:00",
        "duration_seconds": 589800,
        "frozen_sample_count": 1897,
        "raw_slot_count": 1967,
        "severity": "urgent",
        "is_ongoing": False,
    },
)

# The long freeze (item 2-4), addressed by onset so a reordering cannot make
# the wrong row satisfy these assertions.
LONG_FREEZE_ONSET = "2026-12-18T13:00:00+00:00"

UPLOAD_MODES = ("sync", "async")


def _mock_detector() -> MagicMock:
    """Constant sub-threshold LSTM score: stuck detection is model-independent."""
    detector = MagicMock()
    detector.predict_batch_enriched.side_effect = lambda seqs: (
        np.full(len(seqs), 0.05, dtype=np.float32),
        np.zeros((len(seqs), LATENT_DIM), dtype=np.float32),
        np.zeros(len(seqs), dtype=np.float32),
    )
    return detector


def _utc_iso(value: datetime | None) -> str | None:
    """Normalise a column value to a UTC ISO string.

    SQLite hands back naive datetimes for ``DateTime(timezone=True)`` columns,
    so comparing the raw values against the UTC instants the detector wrote
    would fail on the backend only, for no behavioural reason.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _as_dict(row: AnomalyEpisodeRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "greenhouse_id": row.greenhouse_id,
        "channel": row.channel,
        "detector": row.detector,
        "fault_type": row.fault_type,
        "onset_ts": _utc_iso(row.onset_ts),
        "end_ts": _utc_iso(row.end_ts),
        "detected_ts": _utc_iso(row.detected_ts),
        "release_ts": _utc_iso(row.release_ts),
        "duration_seconds": row.duration_seconds,
        "frozen_sample_count": row.frozen_sample_count,
        "raw_slot_count": row.raw_slot_count,
        "severity": row.severity,
        "trigger_value": row.trigger_value,
        "is_ongoing": row.is_ongoing,
        "source": row.source,
    }


async def _read_episodes(db: AsyncSession) -> list[dict[str, Any]]:
    """Every ``anomaly_episodes`` row, oldest onset first."""
    result = await db.execute(
        select(AnomalyEpisodeRecord).order_by(
            AnomalyEpisodeRecord.onset_ts, AnomalyEpisodeRecord.channel
        )
    )
    return [_as_dict(row) for row in result.scalars().all()]


def _episode_at(episodes: list[dict[str, Any]], onset_ts: str) -> dict[str, Any]:
    matches = [ep for ep in episodes if ep["onset_ts"] == onset_ts]
    assert len(matches) == 1, f"expected one episode at {onset_ts}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Upload runners — each owns its own in-memory database
# ---------------------------------------------------------------------------


async def _run_sync_upload(
    db: AsyncSession, fixture: Path, *, persist: bool
) -> None:
    """One pass of the synchronous cold-run path.

    ``persist=False`` is the preview-only upload of item 18: the session
    exists, the caller just does not hand it to ``run_cold_run_upload``, which
    is exactly how the route behaves when the client did not ask for
    persistence.
    """
    adapter = CSVAdapter({"filepath": str(fixture)})
    adapter.connect()
    await run_cold_run_upload(
        tmp_path=str(fixture),
        adapter=adapter,
        detector=_mock_detector(),
        tenant_id=TENANT,
        greenhouse_id=GH_ID,
        chunk_size=CHUNK_ROWS,
        persist_db=db if persist else None,
    )
    await db.commit()


async def _run_async_upload(
    db: AsyncSession, fixture: Path, *, persist: bool
) -> None:
    """One pass of the full async pipeline: produce, consume, finalize."""
    published: list[tuple[str, Any]] = []

    async def _capture_portion(routing_key: str, payload: Any, **kwargs: Any) -> None:
        published.append((routing_key, payload))

    with patch(
        "src.api.services.upload_producer.publish_json", side_effect=_capture_portion
    ):
        adapter = CSVAdapter({"filepath": str(fixture)})
        adapter.connect()
        await produce_upload_portions(
            tmp_path=str(fixture),
            adapter=adapter,
            db=db,
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            checkpoint_path=None,
            persist_data=persist,
            filename=fixture.name,
            portion_rows=CHUNK_ROWS,
        )

    portion_msgs = [msg for key, msg in published if key == "upload.portions"]
    finalize_msgs: list[Any] = []

    async def _capture_finalize(routing_key: str, payload: Any, **kwargs: Any) -> None:
        finalize_msgs.append(payload)

    with patch(
        "src.api.routes.inference.get_cached_detector", return_value=_mock_detector()
    ), patch(
        "src.workers.upload_consumer.publish_json", side_effect=_capture_finalize
    ):
        for msg in portion_msgs:
            await handle_upload_portion(msg, db)

    assert finalize_msgs, "producer/consumer published no finalize message"
    await handle_upload_finalize(finalize_msgs[0], db)
    await db.commit()


_RUNNERS = {"sync": _run_sync_upload, "async": _run_async_upload}


async def _upload_and_read(
    mode: str,
    *,
    fixture: Path = FIXTURE_A,
    persist: bool = True,
    uploads: int = 1,
) -> list[list[dict[str, Any]]]:
    """Upload *fixture* *uploads* times into one fresh DB.

    Returns the table's contents after each upload, so an idempotence check
    compares two snapshots of the SAME database — comparing ids across two
    databases would compare freshly generated UUIDs and could never pass.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        snapshots: list[list[dict[str, Any]]] = []
        async with factory() as db:
            for _ in range(uploads):
                await _RUNNERS[mode](db, fixture, persist=persist)
                snapshots.append(await _read_episodes(db))
        return snapshots
    finally:
        await engine.dispose()


# A single upload of fixture A takes ~1 s and most tests here want the same
# rows. The cache is keyed by the arguments that determine the result and
# holds plain dicts, so no test can observe another's state through it.
_EPISODE_CACHE: dict[tuple[str, str, bool, int], list[list[dict[str, Any]]]] = {}


async def _snapshots_for(
    mode: str,
    *,
    fixture: Path = FIXTURE_A,
    persist: bool = True,
    uploads: int = 1,
) -> list[list[dict[str, Any]]]:
    """Per-upload snapshots of the episode table, cached by argument."""
    key = (mode, fixture.name, persist, uploads)
    if key not in _EPISODE_CACHE:
        _EPISODE_CACHE[key] = await _upload_and_read(
            mode, fixture=fixture, persist=persist, uploads=uploads
        )
    return [[dict(ep) for ep in snapshot] for snapshot in _EPISODE_CACHE[key]]


async def _episodes_for(
    mode: str, *, fixture: Path = FIXTURE_A, persist: bool = True
) -> list[dict[str, Any]]:
    """Episode rows after a single upload of *fixture*."""
    return (await _snapshots_for(mode, fixture=fixture, persist=persist))[0]


# =========================================================================
# Item 1 — four humidity episodes, no temperature, no co2, both paths
# =========================================================================


class TestFixtureAEpisodeSet:
    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_writes_four_episodes(self, mode: str) -> None:
        """Fixture A persists exactly four stuck-fault episodes."""
        episodes = await _episodes_for(mode)
        assert len(episodes) == 4, [ep["onset_ts"] for ep in episodes]

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_every_episode_is_humidity(self, mode: str) -> None:
        """Humidity is the only channel that freezes past the gate here."""
        episodes = await _episodes_for(mode)
        assert {ep["channel"] for ep in episodes} == {"humidity"}

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_no_temperature_episode(self, mode: str) -> None:
        """Temperature opens freezes constantly but none crosses the dual gate."""
        episodes = await _episodes_for(mode)
        assert [ep for ep in episodes if ep["channel"] == "temperature"] == []

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_no_co2_episode(self, mode: str) -> None:
        """co2 is imputed and skipped, so it must not become an episode.

        Fixture A has no co2 column; the channel is filled with a constant, so
        an upload path that failed to pass ``skip_channels`` would persist one
        phantom file-long co2 episode here.
        """
        episodes = await _episodes_for(mode)
        assert [ep for ep in episodes if ep["channel"] == "co2"] == []

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_onsets_are_unchanged(self, mode: str) -> None:
        """Onsets match the four gate crossings documented for fixture A."""
        episodes = await _episodes_for(mode)
        assert [ep["onset_ts"] for ep in episodes] == [
            expected["onset_ts"] for expected in EXPECTED_EPISODES
        ]

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_severity_is_urgent(self, mode: str) -> None:
        episodes = await _episodes_for(mode)
        assert {ep["severity"] for ep in episodes} == {"urgent"}

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_detector_and_fault_type_are_stuck_fault(self, mode: str) -> None:
        """Both labels are written, and both say stuck_fault for this detector."""
        episodes = await _episodes_for(mode)
        assert {ep["detector"] for ep in episodes} == {"stuck_fault"}
        assert {ep["fault_type"] for ep in episodes} == {"stuck_fault"}

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_tenant_and_greenhouse_are_the_uploading_ones(self, mode: str) -> None:
        """Multi-tenant columns carry the upload's own tenant/greenhouse."""
        episodes = await _episodes_for(mode)
        assert {ep["tenant_id"] for ep in episodes} == {TENANT}
        assert {ep["greenhouse_id"] for ep in episodes} == {GH_ID}


# =========================================================================
# Items 2-4 — the long freeze, read back from the database
# =========================================================================


class TestLongFreezeEpisode:
    """The 2026-12-18 13:00 freeze: the one episode with a missing-reading gap."""

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_end_ts_is_the_last_frozen_sample(self, mode: str) -> None:
        """``end_ts`` is the last frozen sample, five minutes before release."""
        episode = _episode_at(await _episodes_for(mode), LONG_FREEZE_ONSET)
        assert episode["end_ts"] == "2026-12-25T08:50:00+00:00"

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_release_ts_is_the_first_normal_sample(self, mode: str) -> None:
        """``release_ts`` is the first NON-frozen sample — not ``end_ts``."""
        episode = _episode_at(await _episodes_for(mode), LONG_FREEZE_ONSET)
        assert episode["release_ts"] == "2026-12-25T08:55:00+00:00"

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_duration_seconds_spans_onset_to_end_ts(self, mode: str) -> None:
        """``duration_seconds`` is ``end_ts - onset_ts``, not the release span."""
        episode = _episode_at(await _episodes_for(mode), LONG_FREEZE_ONSET)
        assert episode["duration_seconds"] == 589_800

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_frozen_sample_count(self, mode: str) -> None:
        """Observed frozen samples over ``[onset_ts, end_ts]``."""
        episode = _episode_at(await _episodes_for(mode), LONG_FREEZE_ONSET)
        assert episode["frozen_sample_count"] == 1_897

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_raw_slot_count(self, mode: str) -> None:
        """Expected slots at the pinned cadence: 589800 / 300 + 1."""
        episode = _episode_at(await _episodes_for(mode), LONG_FREEZE_ONSET)
        assert episode["raw_slot_count"] == 1_967

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_gap_counts_readings_missing_for_the_channel(self, mode: str) -> None:
        """The 70-reading gap is the rows CSVAdapter dropped over a null sensor.

        This is the only episode in the fixture with a gap at all, which is
        what makes it the discriminating case: a ``raw_slot_count`` taken from
        surviving file rows instead of expected slots would report 0 here.
        """
        episode = _episode_at(await _episodes_for(mode), LONG_FREEZE_ONSET)
        assert episode["raw_slot_count"] - episode["frozen_sample_count"] == 70

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_is_ongoing_false_and_release_present(self, mode: str) -> None:
        """A released freeze is closed and carries its release timestamp."""
        episode = _episode_at(await _episodes_for(mode), LONG_FREEZE_ONSET)
        assert episode["is_ongoing"] is False
        assert episode["release_ts"] is not None


# =========================================================================
# Item 3 — detection latency is computed at query time
# =========================================================================


class TestDetectionLatency:
    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_long_freeze_latency_is_six_hours(self, mode: str) -> None:
        """``detected_ts - onset_ts`` for the long freeze is 6.00 h."""
        episode = _episode_at(await _episodes_for(mode), LONG_FREEZE_ONSET)
        latency_hours = (
            datetime.fromisoformat(episode["detected_ts"])
            - datetime.fromisoformat(episode["onset_ts"])
        ).total_seconds() / 3600.0
        assert latency_hours == pytest.approx(6.00)

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_every_latency_equals_the_gate(self, mode: str) -> None:
        """Every episode's latency is the wall-clock gate: the gate stamps it."""
        episodes = await _episodes_for(mode)
        latencies = [
            (
                datetime.fromisoformat(ep["detected_ts"])
                - datetime.fromisoformat(ep["onset_ts"])
            ).total_seconds()
            / 3600.0
            for ep in episodes
        ]
        assert latencies == [pytest.approx(STUCK_WALLCLOCK_MIN_HOURS)] * 4

    async def test_latency_is_queryable_without_a_stored_column(self) -> None:
        """The latency is derivable from the two stored columns alone.

        ``docs/handoff/task3_4_spec.md`` §3 keeps ``detection_latency`` out of
        the schema deliberately; this pins that a reader needs nothing else to
        compute it, selecting just the two columns rather than whole rows.
        """
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(
                bind=engine, class_=AsyncSession, expire_on_commit=False
            )
            async with factory() as db:
                await _run_sync_upload(db, FIXTURE_A, persist=True)
                rows = await db.execute(
                    select(
                        AnomalyEpisodeRecord.onset_ts,
                        AnomalyEpisodeRecord.detected_ts,
                    ).where(
                        AnomalyEpisodeRecord.tenant_id == TENANT,
                        AnomalyEpisodeRecord.detector == "stuck_fault",
                    )
                )
                latencies = {
                    (detected - onset).total_seconds() for onset, detected in rows
                }
            assert latencies == {STUCK_WALLCLOCK_MIN_HOURS * 3600.0}
        finally:
            await engine.dispose()

    def test_schema_has_no_detection_latency_column(self) -> None:
        """Latency stays derived: a stored column would be a second source."""
        assert "detection_latency" not in AnomalyEpisodeRecord.__table__.columns


# =========================================================================
# Item 5 — no phantom episode from the trailing sub-gate freeze
# =========================================================================


class TestNoPhantomEpisode:
    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_trailing_two_sample_freeze_writes_no_row(self, mode: str) -> None:
        """The fixture's trailing 2-sample humidity freeze is below the gate.

        It satisfies neither limb of the dual gate, so it must leave no row —
        asserted by onset date rather than by count alone, so a row that
        replaced one of the four real ones would still be caught.
        """
        episodes = await _episodes_for(mode)
        trailing = [
            ep["onset_ts"]
            for ep in episodes
            if ep["onset_ts"] > EXPECTED_EPISODES[-1]["onset_ts"]
        ]
        assert trailing == []

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_sub_gate_one_hour_freeze_writes_no_row(self, mode: str) -> None:
        """The 1 h freeze on 2026-12-18 11:30 is inside no episode's span.

        It crosses the sample limb but not the hours limb; the episode that
        precedes it ends at 11:15 and the next opens at 13:00.
        """
        episodes = await _episodes_for(mode)
        sub_gate_onset = datetime.fromisoformat("2026-12-18T11:30:00+00:00")
        covering = [
            ep
            for ep in episodes
            if datetime.fromisoformat(ep["onset_ts"])
            <= sub_gate_onset
            <= datetime.fromisoformat(ep["end_ts"])
        ]
        assert covering == []


# =========================================================================
# Item 7 — every column of all four episodes, pinned
# =========================================================================


class TestAllFourSpansPinned:
    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    @pytest.mark.parametrize("index", range(len(EXPECTED_EPISODES)))
    async def test_episode_span(self, mode: str, index: int) -> None:
        """Each episode's full span and counts match the measured ground truth."""
        expected = EXPECTED_EPISODES[index]
        episode = _episode_at(await _episodes_for(mode), expected["onset_ts"])
        assert {key: episode[key] for key in expected} == expected

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_trigger_value_is_the_frozen_reading(self, mode: str) -> None:
        """All four freezes are stuck at the fixture's 100.0 % humidity."""
        episodes = await _episodes_for(mode)
        assert {ep["trigger_value"] for ep in episodes} == {100.0}

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_source_is_null_for_real_detections(self, mode: str) -> None:
        """Fixture A carries no injected rows, so every episode reads real."""
        episodes = await _episodes_for(mode)
        assert {ep["source"] for ep in episodes} == {None}


# =========================================================================
# Item 8 — re-uploading the same file is idempotent
# =========================================================================


class TestReuploadIdempotence:
    """Both snapshots come from one database: upload, read, upload again, read."""

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_same_file_twice_gives_four_rows(self, mode: str) -> None:
        """The upsert key turns a re-upload into an update, not four new rows."""
        _, after_second = await _snapshots_for(mode, uploads=2)
        assert len(after_second) == 4

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_reupload_preserves_episode_ids(self, mode: str) -> None:
        """An update must keep the row's identity: same ids, not replacements."""
        after_first, after_second = await _snapshots_for(mode, uploads=2)
        assert [ep["id"] for ep in after_second] == [ep["id"] for ep in after_first]

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_reupload_leaves_every_column_unchanged(self, mode: str) -> None:
        """Re-detecting the same freezes must not perturb any value."""
        after_first, after_second = await _snapshots_for(mode, uploads=2)
        assert after_second == after_first


# =========================================================================
# Item 9 — sync and async write the same rows
# =========================================================================


class TestSyncAsyncRowParity:
    async def test_all_four_rows_identical_across_paths(self) -> None:
        """Every column but the generated ``id`` matches across both paths."""
        sync_episodes = await _episodes_for("sync")
        async_episodes = await _episodes_for("async")
        for episode in sync_episodes + async_episodes:
            episode.pop("id")
        assert sync_episodes == async_episodes

    async def test_ids_are_distinct_uuids_per_upload(self) -> None:
        """Ids are per-row UUIDs, so the parity above is not id equality."""
        sync_ids = {ep["id"] for ep in await _episodes_for("sync")}
        async_ids = {ep["id"] for ep in await _episodes_for("async")}
        assert len(sync_ids) == len(async_ids) == 4
        assert sync_ids.isdisjoint(async_ids)


# =========================================================================
# Item 18 — a preview-only upload writes nothing
# =========================================================================


class TestPreviewOnlyWritesNoEpisodes:
    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_preview_upload_writes_zero_episodes(self, mode: str) -> None:
        """Episode persistence is gated exactly like the surrounding writes.

        Sync: ``persist_db is None``. Async: the ``persist_data`` payload flag.
        Both run the same detection over the same fixture, so the four
        episodes exist in memory either way — only the write is suppressed.
        """
        episodes = await _episodes_for(mode, persist=False)
        assert episodes == []

    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_persisting_upload_writes_readings_and_episodes(
        self, mode: str
    ) -> None:
        """The gate is not vacuous: with persistence on, both tables fill."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(
                bind=engine, class_=AsyncSession, expire_on_commit=False
            )
            async with factory() as db:
                await _RUNNERS[mode](db, FIXTURE_A, persist=True)
                readings = await db.scalar(
                    select(func.count(SensorReading.id)).where(
                        SensorReading.tenant_id == TENANT
                    )
                )
                episodes = await db.scalar(
                    select(func.count(AnomalyEpisodeRecord.id)).where(
                        AnomalyEpisodeRecord.tenant_id == TENANT
                    )
                )
            assert readings > 0
            assert episodes == 4
        finally:
            await engine.dispose()


# =========================================================================
# Regression — the clear-sky fixture has no stuck fault to persist
# =========================================================================


class TestClearskyRegression:
    @pytest.mark.parametrize("mode", UPLOAD_MODES)
    async def test_clearsky_writes_no_episodes(self, mode: str) -> None:
        """``native_july_clearsky.csv`` has no freeze, so no episode row."""
        episodes = await _episodes_for(mode, fixture=FIXTURE_CLEARSKY)
        assert episodes == []
