"""Tests for anomaly score backfill (service + route).

Covers:
- Idempotent re-run: second call produces the same scores.
- Non-finite raw score → canonical returns None → anomaly_score stored as NULL.
- Batch boundaries: overlap ensures sequences spanning page boundaries are scored.
- Tenant isolation: backfill for tenant A never touches tenant B rows.
- source='live' rows are excluded from scoring and write-back.
- Route responds correctly (happy path + validation errors).
- Model resolution: registry lookup, db:// artifact resolution, explicit
  checkpoint_path, and the no-active-model error path (all previously
  untested — the route-level fixture used to mock the detector directly,
  bypassing resolution entirely).
"""

from __future__ import annotations

import datetime
import math
import tempfile
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select

from src.api.main import app
from src.api.services.detector_cache import NoActiveModelError, resolve_tenant_detector
from src.api.services.score_backfill import (
    BackfillResult,
    _SCORE_SCALE,
    backfill_scores,
)
from src.config import ANOMALY_SCALING_FACTOR, SEQ_LEN
from src.db.models import Base, SensorReading
from src.db.session import get_db
from src.models.artifact_store import DB_SCHEME, put_checkpoint
from src.models.model_registry import ModelRegistry

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

TENANT = "tenant_backfill_test"
OTHER_TENANT = "tenant_backfill_other"
GH = "gh_test"

_BASE_TS = datetime.datetime(2026, 3, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
_STEP = datetime.timedelta(minutes=5)


def _make_ts(i: int) -> datetime.datetime:
    return _BASE_TS + i * _STEP


def _sensor_row(
    *,
    tenant_id: str,
    greenhouse_id: str,
    index: int,
    source: str | None = None,
    anomaly_score: float | None = None,
) -> SensorReading:
    return SensorReading(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        timestamp=_make_ts(index),
        temperature=20.0 + index * 0.05,
        humidity=55.0,
        co2=400.0,
        light=300.0,
        source=source,
        anomaly_score=anomaly_score,
    )


def _mock_detector_uniform(raw_mse: float = 0.3) -> MagicMock:
    """Detector that returns the same raw MSE for every sequence."""
    det = MagicMock()
    det.predict_batch_enriched.side_effect = lambda seqs: (
        np.full(len(seqs), raw_mse, dtype=np.float32),
        np.zeros((len(seqs), 8), dtype=np.float32),
        np.zeros(len(seqs), dtype=np.float32),
    )
    return det


def _mock_detector(raw_mse: float = 0.3) -> MagicMock:
    """Return a mock AnomalyDetector whose predict_batch_enriched returns a fixed MSE."""
    return _mock_detector_uniform(raw_mse)


def _mock_detector_with_outlier(
    normal_mse: float = 0.6,
    outlier_mse: float = 1.9,
) -> MagicMock:
    """Detector that makes the last sequence of each batch an outlier.

    With ANOMALY_SCALING_FACTOR=2.0:
    - normal_mse=0.6  → scaled=0.30 (below P99 threshold)
    - outlier_mse=1.9 → scaled=0.95 (above P99 threshold)

    This ensures calibrate_threshold finds a threshold of ~0.885 (P99 of
    [0.3]*N + [0.95]) so that the outlier scores qualify.
    """
    det = MagicMock()

    def _side_effect(seqs: np.ndarray) -> tuple:
        n = len(seqs)
        scores = np.full(n, normal_mse, dtype=np.float32)
        if n > 0:
            scores[-1] = outlier_mse  # last sequence in each batch is anomalous
        return (
            scores,
            np.zeros((n, 8), dtype=np.float32),
            np.zeros(n, dtype=np.float32),
        )

    det.predict_batch_enriched.side_effect = _side_effect
    return det


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def populated_db(db_session: AsyncSession) -> AsyncSession:
    """30 CSV rows for TENANT/GH, 5 live rows, 5 rows for OTHER_TENANT."""
    # 30 normal CSV rows
    for i in range(30):
        db_session.add(_sensor_row(tenant_id=TENANT, greenhouse_id=GH, index=i))
    # 5 live mock rows (must be excluded)
    for i in range(30, 35):
        db_session.add(
            _sensor_row(tenant_id=TENANT, greenhouse_id=GH, index=i, source="live")
        )
    # 5 rows for another tenant (must not be touched)
    for i in range(30):
        db_session.add(
            _sensor_row(tenant_id=OTHER_TENANT, greenhouse_id=GH, index=i)
        )
    await db_session.commit()
    return db_session


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestBackfillScoresService:
    @pytest.mark.asyncio
    async def test_scores_above_threshold_are_written(
        self, populated_db: AsyncSession
    ) -> None:
        """Rows scoring above the file-calibrated threshold get anomaly_score set."""
        # Use a bimodal distribution: most sequences score ~0.30, last one ~0.95.
        # calibrate_threshold at P99 of mostly-0.30 values ≈ 0.885 < 0.95
        # → the outlier sequence qualifies and its terminal row gets scored.
        det = _mock_detector_with_outlier()
        start = _BASE_TS
        end = _BASE_TS + 29 * _STEP

        result = await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=start,
            end=end,
        )

        assert result.rows_examined == 30
        assert result.sequences_scored > 0
        assert result.rows_scored > 0
        assert result.threshold_used > 0

        # All scored rows should have anomaly_score set.
        stmt = select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.greenhouse_id == GH,
            SensorReading.anomaly_score.is_not(None),
        )
        scored_rows = (await populated_db.execute(stmt)).scalars().all()
        assert len(scored_rows) == result.rows_scored

        # score_scale must be "upload" on every scored row.
        for row in scored_rows:
            assert row.score_scale == _SCORE_SCALE
            assert row.anomaly_score is not None
            assert 0.0 <= row.anomaly_score <= 1.0

    @pytest.mark.asyncio
    async def test_idempotent_rerun(self, populated_db: AsyncSession) -> None:
        """Running backfill twice produces identical scores."""
        det = _mock_detector_with_outlier()
        start = _BASE_TS
        end = _BASE_TS + 29 * _STEP

        result1 = await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=start,
            end=end,
        )
        # Fetch scores after first run.
        stmt = select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.greenhouse_id == GH,
            SensorReading.anomaly_score.is_not(None),
        ).order_by(SensorReading.timestamp)
        rows_after_first = (await populated_db.execute(stmt)).scalars().all()
        scores_first = {r.timestamp: r.anomaly_score for r in rows_after_first}

        # Second run.
        result2 = await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=start,
            end=end,
        )
        rows_after_second = (await populated_db.execute(stmt)).scalars().all()
        scores_second = {r.timestamp: r.anomaly_score for r in rows_after_second}

        assert result1.rows_scored == result2.rows_scored
        assert scores_first.keys() == scores_second.keys()
        for ts in scores_first:
            # Scores should be the same (same data, same model, same threshold).
            assert math.isclose(
                scores_first[ts], scores_second[ts], rel_tol=1e-9
            ), f"Score mismatch at {ts}: {scores_first[ts]} vs {scores_second[ts]}"

    @pytest.mark.asyncio
    async def test_live_rows_excluded(self, populated_db: AsyncSession) -> None:
        """Rows with source='live' are never scored or written."""
        det = _mock_detector_with_outlier()
        start = _BASE_TS
        # Include timestamps of live rows (indices 30..34).
        end = _BASE_TS + 34 * _STEP

        result = await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=start,
            end=end,
        )

        assert result.live_rows_skipped == 5

        # Live rows must NOT have anomaly_score set.
        live_stmt = select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.greenhouse_id == GH,
            SensorReading.source == "live",
        )
        live_rows = (await populated_db.execute(live_stmt)).scalars().all()
        for row in live_rows:
            assert row.anomaly_score is None, (
                f"Live row at {row.timestamp} should not have anomaly_score"
            )

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, populated_db: AsyncSession) -> None:
        """Backfill for TENANT never writes scores to OTHER_TENANT rows."""
        det = _mock_detector_with_outlier()
        start = _BASE_TS
        end = _BASE_TS + 29 * _STEP

        await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=start,
            end=end,
        )

        other_stmt = select(SensorReading).where(
            SensorReading.tenant_id == OTHER_TENANT,
            SensorReading.anomaly_score.is_not(None),
        )
        other_scored = (await populated_db.execute(other_stmt)).scalars().all()
        assert len(other_scored) == 0, (
            f"OTHER_TENANT rows should not have scores, found {len(other_scored)}"
        )

    @pytest.mark.asyncio
    async def test_nonfinite_score_stored_as_null(
        self, populated_db: AsyncSession
    ) -> None:
        """Non-finite raw score → canonical returns None → anomaly_score is SQL NULL."""
        # Patch canonical to return None for ANY input, simulating a non-finite guard.
        with patch(
            "src.api.services.score_backfill._canonical",
            return_value=None,
        ):
            det = _mock_detector(raw_mse=2.0)
            result = await backfill_scores(
                db=populated_db,
                detector=det,
                tenant_id=TENANT,
                greenhouse_id=GH,
                start=_BASE_TS,
                end=_BASE_TS + 29 * _STEP,
            )

        # rows_scored counts entries where canonical was None (still written for provenance).
        # All scored rows should have anomaly_score=NULL but score_scale='upload'.
        stmt = select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.greenhouse_id == GH,
            SensorReading.score_scale == _SCORE_SCALE,
        )
        written_rows = (await populated_db.execute(stmt)).scalars().all()
        for row in written_rows:
            assert row.anomaly_score is None, (
                f"Row at {row.timestamp}: expected NULL anomaly_score "
                f"when canonical returns None, got {row.anomaly_score}"
            )
            assert row.score_scale == _SCORE_SCALE

    @pytest.mark.asyncio
    async def test_batch_boundary_sequences_scored(self) -> None:
        """Sequences spanning batch-page boundaries are scored without gaps.

        With batch_size << SEQ_LEN the working-set overlap must carry enough
        rows from prior pages so that every possible sliding window is formed
        across page boundaries, yielding n_rows - SEQ_LEN + 1 total sequences.
        """
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )

        # 40 rows, SEQ_LEN=20 → 21 expected sequences.
        # batch_size=10 forces 4 DB pages; sequences straddle every page boundary.
        n_rows = 40
        async with factory() as session:
            for i in range(n_rows):
                session.add(
                    _sensor_row(tenant_id=TENANT, greenhouse_id=GH, index=i)
                )
            await session.commit()

            # Use a uniform detector: the threshold equals the uniform score, so
            # 0 rows are written; we only need to verify sequence count here.
            det = _mock_detector_uniform(raw_mse=0.6)
            result = await backfill_scores(
                db=session,
                detector=det,
                tenant_id=TENANT,
                greenhouse_id=GH,
                start=_BASE_TS,
                end=_BASE_TS + (n_rows - 1) * _STEP,
                batch_size=10,  # Force multiple pages with overlap
            )

        # sequences_scored should equal n_rows - SEQ_LEN + 1 = 21
        expected_sequences = n_rows - SEQ_LEN + 1
        assert result.sequences_scored == expected_sequences, (
            f"Expected {expected_sequences} sequences, got {result.sequences_scored}"
        )
        assert result.batches_processed > 1

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_below_threshold_rows_remain_null(
        self, populated_db: AsyncSession
    ) -> None:
        """Rows scoring below the calibrated threshold keep anomaly_score=NULL."""
        # Raw MSE very low → clipped score near zero → below any threshold.
        det = _mock_detector(raw_mse=0.0001)
        result = await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=_BASE_TS,
            end=_BASE_TS + 29 * _STEP,
        )

        # All above-threshold scores (if any) are written; below-threshold stay NULL.
        stmt = select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.greenhouse_id == GH,
        )
        all_rows = (await populated_db.execute(stmt)).scalars().all()
        scored_count = sum(1 for r in all_rows if r.anomaly_score is not None)
        assert scored_count == result.rows_scored

    @pytest.mark.asyncio
    async def test_empty_window_returns_zero_counts(
        self, db_session: AsyncSession
    ) -> None:
        """An empty time window (no rows) returns a zero-scored result."""
        det = _mock_detector()
        result = await backfill_scores(
            db=db_session,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=_BASE_TS,
            end=_BASE_TS + datetime.timedelta(days=1),
        )

        assert result.rows_examined == 0
        assert result.sequences_scored == 0
        assert result.rows_scored == 0

    @pytest.mark.asyncio
    async def test_score_scale_is_upload(self, populated_db: AsyncSession) -> None:
        """Every written row carries score_scale='upload' (ADR-005 provenance)."""
        det = _mock_detector_with_outlier()
        await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=_BASE_TS,
            end=_BASE_TS + 29 * _STEP,
        )

        stmt = select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.anomaly_score.is_not(None),
        )
        scored = (await populated_db.execute(stmt)).scalars().all()
        assert len(scored) > 0, "Expected at least one scored row"
        for row in scored:
            assert row.score_scale == "upload", (
                f"score_scale should be 'upload', got {row.score_scale!r}"
            )

    @pytest.mark.asyncio
    async def test_naive_datetimes_treated_as_utc(
        self, populated_db: AsyncSession
    ) -> None:
        """Timezone-naïve start/end are accepted and treated as UTC."""
        det = _mock_detector_with_outlier()
        start_naive = _BASE_TS.replace(tzinfo=None)
        end_naive = (_BASE_TS + 29 * _STEP).replace(tzinfo=None)

        result = await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=start_naive,
            end=end_naive,
        )

        # Should succeed and find the 30 rows.
        assert result.rows_examined == 30

    @pytest.mark.asyncio
    async def test_score_raw_stored_on_row(self, populated_db: AsyncSession) -> None:
        """anomaly_score_raw contains the pre-canonical (scaled) score."""
        outlier_mse = 1.9
        expected_scaled = min(outlier_mse / ANOMALY_SCALING_FACTOR, 1.0)  # 0.95

        det = _mock_detector_with_outlier(normal_mse=0.6, outlier_mse=outlier_mse)
        await backfill_scores(
            db=populated_db,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH,
            start=_BASE_TS,
            end=_BASE_TS + 29 * _STEP,
        )

        stmt = select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.anomaly_score.is_not(None),
        )
        scored = (await populated_db.execute(stmt)).scalars().all()
        assert len(scored) > 0
        for row in scored:
            assert row.anomaly_score_raw is not None
            assert math.isclose(
                row.anomaly_score_raw, expected_scaled, rel_tol=1e-6
            ), f"anomaly_score_raw={row.anomaly_score_raw!r}, expected≈{expected_scaled!r}"


# ---------------------------------------------------------------------------
# Route-level tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def route_db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for i in range(30):
            session.add(_sensor_row(tenant_id=TENANT, greenhouse_id=GH, index=i))
        await session.commit()
        # Registry lookup is exercised for real here — the route no longer
        # skips resolution, so TENANT needs an active deployment or every
        # route test would hit the no-active-model error path.
        await ModelRegistry(session).register_model(
            customer_id=TENANT,
            model_type="lstm_vae",
            checkpoint_path="models/checkpoints/fixture_active.pt",
        )
        yield session
    await engine.dispose()


@pytest.fixture
def api_client(route_db: AsyncSession):
    async def override_get_db():
        yield route_db

    app.dependency_overrides[get_db] = override_get_db
    # Patch the detector cache (not resolution) so no checkpoint file needs
    # to exist on disk — resolution itself runs for real against route_db.
    with patch(
        "src.api.services.detector_cache.get_cached_detector",
        return_value=_mock_detector_with_outlier(),
    ):
        with TestClient(app) as client:
            yield client
    app.dependency_overrides.clear()


class TestBackfillRoute:
    def test_happy_path_returns_200(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/backfill/scores",
            json={
                "tenant_id": TENANT,
                "greenhouse_id": GH,
                "start": _BASE_TS.isoformat(),
                "end": (_BASE_TS + 29 * _STEP).isoformat(),
            },
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["tenant_id"] == TENANT
        assert body["greenhouse_id"] == GH
        assert body["rows_examined"] == 30
        assert body["sequences_scored"] >= 0
        assert "threshold_used" in body
        assert "threshold_method" in body
        assert "warnings" in body

    def test_end_before_start_returns_422(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/backfill/scores",
            json={
                "tenant_id": TENANT,
                "greenhouse_id": GH,
                "start": (_BASE_TS + datetime.timedelta(days=1)).isoformat(),
                "end": _BASE_TS.isoformat(),
            },
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_missing_required_fields_returns_422(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/backfill/scores",
            json={"tenant_id": TENANT},  # missing start, end
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_invalid_batch_size_returns_422(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/backfill/scores",
            json={
                "tenant_id": TENANT,
                "greenhouse_id": GH,
                "start": _BASE_TS.isoformat(),
                "end": (_BASE_TS + 29 * _STEP).isoformat(),
                "batch_size": 50,  # below ge=100
            },
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_batches_processed_gt_zero(self, api_client: TestClient) -> None:
        resp = api_client.post(
            "/api/backfill/scores",
            json={
                "tenant_id": TENANT,
                "greenhouse_id": GH,
                "start": _BASE_TS.isoformat(),
                "end": (_BASE_TS + 29 * _STEP).isoformat(),
                "batch_size": 100,  # Valid minimum; 30 rows → 1 batch
            },
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["batches_processed"] >= 1

    def test_no_active_model_returns_503_naming_tenant(
        self, api_client: TestClient
    ) -> None:
        """A tenant with no registry deployment must fail clearly, not with
        a bare 'models/baseline.pt not found' (the original production bug)."""
        unregistered_tenant = "tenant_no_active_model"
        resp = api_client.post(
            "/api/backfill/scores",
            json={
                "tenant_id": unregistered_tenant,
                "greenhouse_id": GH,
                "start": _BASE_TS.isoformat(),
                "end": (_BASE_TS + 29 * _STEP).isoformat(),
            },
        )
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        message = resp.json()["message"]
        assert unregistered_tenant in message
        assert "ModelRegistry" in message


# ---------------------------------------------------------------------------
# Model resolution — src.api.services.detector_cache.resolve_tenant_detector
# ---------------------------------------------------------------------------


class TestResolveTenantDetector:
    """Exercises the resolution logic directly (registry + artifact_store),
    with only the final model load (get_cached_detector) mocked. A test that
    mocks the detector alone would miss this class of bug entirely."""

    @pytest_asyncio.fixture
    async def resolution_db(self) -> AsyncGenerator[AsyncSession, None]:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            yield session
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_no_checkpoint_uses_registry_active_model(
        self, resolution_db: AsyncSession
    ) -> None:
        await ModelRegistry(resolution_db).register_model(
            customer_id=TENANT,
            model_type="lstm_vae",
            checkpoint_path="models/checkpoints/registered.pt",
        )
        with patch(
            "src.api.services.detector_cache.get_cached_detector"
        ) as mock_get_detector:
            mock_get_detector.return_value = _mock_detector()
            await resolve_tenant_detector(resolution_db, TENANT)
        mock_get_detector.assert_called_once_with("models/checkpoints/registered.pt")

    @pytest.mark.asyncio
    async def test_explicit_checkpoint_path_overrides_registry(
        self, resolution_db: AsyncSession
    ) -> None:
        await ModelRegistry(resolution_db).register_model(
            customer_id=TENANT,
            model_type="lstm_vae",
            checkpoint_path="models/checkpoints/registered.pt",
        )
        with patch(
            "src.api.services.detector_cache.get_cached_detector"
        ) as mock_get_detector:
            mock_get_detector.return_value = _mock_detector()
            await resolve_tenant_detector(
                resolution_db, TENANT, "models/checkpoints/explicit.pt"
            )
        mock_get_detector.assert_called_once_with("models/checkpoints/explicit.pt")

    @pytest.mark.asyncio
    async def test_explicit_db_path_is_resolved_through_artifact_store(
        self, resolution_db: AsyncSession
    ) -> None:
        """A caller-supplied db:// path must still be resolved to a local
        cache file, not handed to the detector as-is."""
        with tempfile.NamedTemporaryFile(suffix=".pt") as tmp:
            tmp.write(b"fake-checkpoint-bytes")
            tmp.flush()
            artifact_path = await put_checkpoint(
                resolution_db, "checkpoints/explicit-db", tmp.name
            )
        assert artifact_path.startswith(DB_SCHEME)

        with patch(
            "src.api.services.detector_cache.get_cached_detector"
        ) as mock_get_detector:
            mock_get_detector.return_value = _mock_detector()
            await resolve_tenant_detector(resolution_db, TENANT, artifact_path)

        assert mock_get_detector.call_count == 1
        (resolved_path,), _ = mock_get_detector.call_args
        assert not resolved_path.startswith(DB_SCHEME)
        assert resolved_path.endswith(".pt")

    @pytest.mark.asyncio
    async def test_registry_db_path_is_also_resolved(
        self, resolution_db: AsyncSession
    ) -> None:
        """The active-deployment path (not just an explicit one) must also
        go through db:// resolution — this is exactly the branch the
        production bug skipped."""
        with tempfile.NamedTemporaryFile(suffix=".pt") as tmp:
            tmp.write(b"fake-checkpoint-bytes")
            tmp.flush()
            artifact_path = await put_checkpoint(
                resolution_db, "checkpoints/registry-db", tmp.name
            )
        await ModelRegistry(resolution_db).register_model(
            customer_id=TENANT,
            model_type="lstm_vae",
            checkpoint_path=artifact_path,
        )

        with patch(
            "src.api.services.detector_cache.get_cached_detector"
        ) as mock_get_detector:
            mock_get_detector.return_value = _mock_detector()
            await resolve_tenant_detector(resolution_db, TENANT)

        (resolved_path,), _ = mock_get_detector.call_args
        assert not resolved_path.startswith(DB_SCHEME)
        assert resolved_path.endswith(".pt")

    @pytest.mark.asyncio
    async def test_no_active_model_raises_naming_tenant(
        self, resolution_db: AsyncSession
    ) -> None:
        with pytest.raises(NoActiveModelError, match=TENANT):
            await resolve_tenant_detector(resolution_db, TENANT)

    @pytest.mark.asyncio
    async def test_missing_artifact_raises_no_active_model_error(
        self, resolution_db: AsyncSession
    ) -> None:
        """A db:// path pointing at a deleted/never-uploaded artifact must
        surface as the same actionable error, not a bare FileNotFoundError."""
        with pytest.raises(NoActiveModelError, match=TENANT):
            await resolve_tenant_detector(
                resolution_db, TENANT, f"{DB_SCHEME}checkpoints/does-not-exist"
            )
