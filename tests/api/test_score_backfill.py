"""Tests for anomaly score backfill (service + route).

Covers:
- Idempotent re-run: second call produces the same scores.
- Non-finite raw score → canonical returns None → anomaly_score stored as NULL.
- Batch boundaries: overlap ensures sequences spanning page boundaries are scored.
- Tenant isolation: backfill for tenant A never touches tenant B rows.
- source='live' rows are excluded from scoring and write-back.
- Route responds correctly (happy path + validation errors).
"""

from __future__ import annotations

import datetime
import math
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
from src.api.services.score_backfill import (
    BackfillResult,
    _SCORE_SCALE,
    backfill_scores,
)
from src.config import ANOMALY_SCALING_FACTOR, SEQ_LEN
from src.db.models import Base, SensorReading
from src.db.session import get_db

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
        yield session
    await engine.dispose()


@pytest.fixture
def api_client(route_db: AsyncSession):
    async def override_get_db():
        yield route_db

    app.dependency_overrides[get_db] = override_get_db
    # Patch the detector cache so no checkpoint file is needed.
    with patch(
        "src.api.routes.backfill.get_cached_detector",
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
