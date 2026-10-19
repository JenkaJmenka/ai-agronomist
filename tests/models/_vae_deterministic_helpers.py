"""Shared helpers for VAE deterministic-inference tests.

Provides fixture paths, the real-checkpoint loader, LSTM alert extractors,
and the sync/async scoring pipelines used by both the pipeline-level and
unit-level test files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.cold_run_upload import run_cold_run_upload
from src.api.services.upload_producer import produce_upload_portions
from src.db.jobs import get_job
from src.db.models import Base
from src.models.inference import AnomalyDetector
from src.vendors.csv_adapter import CSVAdapter
from src.workers.upload_consumer import handle_upload_finalize, handle_upload_portion


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_DEC = FIXTURE_DIR / "native_dec_stuck_fault.csv"
FIXTURE_JULY = FIXTURE_DIR / "native_july_clearsky.csv"
CHECKPOINT_PATH = FIXTURE_DIR / "epoch_0072_trained_baseline.pt"
TENANT = "det_inv"
GH_ID = "gh_det"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def real_detector() -> AnomalyDetector:
    """Load the real epoch-72 checkpoint. Skip if not present."""
    if not CHECKPOINT_PATH.is_file():
        pytest.skip(f"Real checkpoint not found: {CHECKPOINT_PATH}")
    return AnomalyDetector(str(CHECKPOINT_PATH))


def lstm_alerts_from_response(response: Any) -> list[dict]:
    """Extract LSTM alerts from a sync UploadInferenceResponse."""
    return sorted(
        [
            h.model_dump(mode="json")
            for h in response.high_anomalies
            if h.detector and h.detector.startswith("lstm")
        ],
        key=lambda a: a["timestamp"],
    )


def lstm_alerts_from_dict(result: dict) -> list[dict]:
    """Extract LSTM alerts from an async job result dict."""
    return sorted(
        [
            h
            for h in result.get("high_anomalies", [])
            if h.get("detector", "").startswith("lstm")
        ],
        key=lambda a: a["timestamp"],
    )


async def score_sync(
    fixture_path: Path,
    detector: AnomalyDetector,
    chunk_size: int = 5000,
) -> Any:
    """Run the sync cold_run_upload pipeline with the real detector."""
    adapter = CSVAdapter({"filepath": str(fixture_path)})
    adapter.connect()
    response, _ = await run_cold_run_upload(
        tmp_path=str(fixture_path),
        adapter=adapter,
        detector=detector,
        tenant_id=TENANT,
        greenhouse_id=GH_ID,
        chunk_size=chunk_size,
    )
    return response


async def score_async(
    fixture_path: Path,
    detector: AnomalyDetector,
    portion_rows: int = 500,
) -> dict:
    """Run the async pipeline with the real detector, return job result."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    async with factory() as sess:
        published: list[tuple[str, Any]] = []

        async def _cap_pub(rk: str, payload: Any, **kw: Any) -> None:
            published.append((rk, payload))

        with patch(
            "src.api.services.upload_producer.publish_json",
            side_effect=_cap_pub,
        ):
            adapter = CSVAdapter({"filepath": str(fixture_path)})
            adapter.connect()
            job_id, portion_count = await produce_upload_portions(
                tmp_path=str(fixture_path),
                adapter=adapter,
                db=sess,
                tenant_id=TENANT,
                greenhouse_id=GH_ID,
                checkpoint_path=None,
                persist_data=False,
                filename=fixture_path.name,
                portion_rows=portion_rows,
            )

        portion_msgs = [
            m for key, m in published if key == "upload.portions"
        ]
        assert len(portion_msgs) == portion_count

        finalize_msgs: list[Any] = []

        async def _cap_fin(rk: str, payload: Any, **kw: Any) -> None:
            finalize_msgs.append(payload)

        with patch(
            "src.api.routes.inference.get_cached_detector",
            return_value=detector,
        ), patch(
            "src.workers.upload_consumer.publish_json",
            side_effect=_cap_fin,
        ):
            for msg in portion_msgs:
                await handle_upload_portion(msg, sess)

        if finalize_msgs:
            await handle_upload_finalize(finalize_msgs[0], sess)

        job = await get_job(sess, job_id)
        result = job.result

    await engine.dispose()
    return result
