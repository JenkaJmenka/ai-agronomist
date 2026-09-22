"""Tests for _merge_psychro_contexts: duplicate-timestamp handling.

Gap 2, task 1f phase 3.

Covers:
  a. Identical duplicate values across two portions: merged once, NO warning.
  b. Differing duplicate values: WARNING logged with correct message, lowest
     portion_idx value kept, no exception.
  c. Out-of-order portion_idx input: observe and report actual behavior.
  d. Natural duplicates through the real async pipeline are always identical
     (the "never happens but must be provably safe" case).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.upload_producer import produce_upload_portions
from src.db.jobs import get_job
from src.db.models import Base
from src.vendors.csv_adapter import CSVAdapter
from src.workers.upload_consumer import (
    _merge_psychro_contexts,
    handle_upload_finalize,
    handle_upload_portion,
)

LOGGER_NAME = "src.workers.upload_consumer"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
SYNTH_FIXTURE = FIXTURE_DIR / "synthetic_humidity_freeze_recovery.csv"
NATIVE_FIXTURE_A = FIXTURE_DIR / "native_dec_stuck_fault.csv"
TENANT = "merge_test"
GH_ID = "gh_merge"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_detector() -> MagicMock:
    det = MagicMock()
    det.predict_batch_enriched.side_effect = lambda seqs: (
        np.full(len(seqs), 0.05, dtype=np.float32),
        np.zeros((len(seqs), 8), dtype=np.float32),
        np.zeros(len(seqs), dtype=np.float32),
    )
    return det


async def _run_async_pipeline_with_psychro_capture(
    fixture_path: Path,
    portion_rows: int,
) -> tuple[dict, list[dict]]:
    """Run full async pipeline and also capture each portion's psychro_context
    from the stored portion results so we can inspect overlap duplicates."""
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
            "src.api.services.upload_producer.publish_json", side_effect=_cap_pub
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

        portion_msgs = [m for key, m in published if key == "upload.portions"]
        assert len(portion_msgs) == portion_count

        finalize_msgs: list[Any] = []
        # Capture portion result payloads for psychro_context inspection
        portion_results: list[dict] = []
        original_handle = handle_upload_portion.__wrapped__ if hasattr(handle_upload_portion, "__wrapped__") else None

        async def _cap_fin(rk: str, payload: Any, **kw: Any) -> None:
            finalize_msgs.append(payload)

        with patch(
            "src.api.routes.inference.get_cached_detector",
            return_value=_mock_detector(),
        ), patch(
            "src.workers.upload_consumer.publish_json", side_effect=_cap_fin
        ):
            for msg in portion_msgs:
                await handle_upload_portion(msg, sess)

        if finalize_msgs:
            await handle_upload_finalize(finalize_msgs[0], sess)

        job = await get_job(sess, job_id)
        result = job.result

        # Read back portion results to inspect psychro_context per portion
        from sqlalchemy import select
        from src.db.models import JobPortion
        rows = (await sess.execute(
            select(JobPortion)
            .where(JobPortion.job_id == job_id)
            .order_by(JobPortion.portion_idx)
        )).scalars().all()
        for row in rows:
            if row.result:
                portion_results.append({
                    "portion_idx": row.portion_idx,
                    "psychro_context": row.result.get("psychro_context", {}),
                })

    await engine.dispose()
    return result, portion_results


# =========================================================================
# a. Identical duplicate values across portions: merged once, no warning
# =========================================================================


class TestIdenticalDuplicates:
    """When two portions report the SAME timestamp with IDENTICAL values,
    the merged result has that value once and NO warning is logged."""

    def test_identical_values_no_warning(self, caplog):
        """Two portions, same timestamp, identical [T, RH, hd] -> no warning."""
        ts = "2026-07-01T12:00:00+00:00"
        values = [22.5, 65.0, 4.2]

        portions = [
            (0, {ts: values}),
            (1, {ts: list(values)}),  # same values, fresh list
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            merged = _merge_psychro_contexts(portions, job_id="test-identical")

        # Value present exactly once
        assert ts in merged
        assert merged[ts] == (22.5, 65.0, 4.2)

        # No duplicate-related warning logged
        duplicate_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message and r.levelno >= logging.WARNING
        ]
        assert len(duplicate_warnings) == 0, (
            f"Expected no DIFFERING-values warning for identical duplicates, "
            f"got: {[r.message for r in duplicate_warnings]}"
        )

    def test_three_portions_identical(self, caplog):
        """Three portions with the same timestamp and identical values."""
        ts = "2026-08-15T06:30:00+00:00"
        values = [18.0, 72.0, 3.1]

        portions = [
            (0, {ts: values}),
            (2, {ts: list(values)}),
            (5, {ts: list(values)}),
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            merged = _merge_psychro_contexts(portions, job_id="test-triple")

        assert merged[ts] == (18.0, 72.0, 3.1)
        duplicate_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message
        ]
        assert len(duplicate_warnings) == 0

    def test_non_overlapping_timestamps_no_warning(self, caplog):
        """Distinct timestamps across portions: no overlap, no warning."""
        portions = [
            (0, {
                "2026-07-01T12:00:00+00:00": [20.0, 60.0, 3.0],
                "2026-07-01T12:05:00+00:00": [21.0, 61.0, 3.1],
            }),
            (1, {
                "2026-07-01T12:10:00+00:00": [22.0, 62.0, 3.2],
                "2026-07-01T12:15:00+00:00": [23.0, 63.0, 3.3],
            }),
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            merged = _merge_psychro_contexts(portions, job_id="test-nonoverlap")

        assert len(merged) == 4
        duplicate_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message
        ]
        assert len(duplicate_warnings) == 0


# =========================================================================
# b. Differing duplicate values: WARNING logged, lowest portion_idx wins
# =========================================================================


class TestDifferingDuplicates:
    """When two portions report the SAME timestamp with DIFFERING values,
    a WARNING must be logged and the lowest portion_idx value is kept."""

    def test_differing_values_warning_logged(self, caplog):
        """Differing values -> WARNING with correct message content."""
        ts = "2026-07-01T12:00:00+00:00"
        values_p0 = [22.5, 65.0, 4.2]
        values_p1 = [23.0, 66.0, 4.5]  # different!

        portions = [
            (0, {ts: values_p0}),
            (1, {ts: values_p1}),
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            merged = _merge_psychro_contexts(portions, job_id="test-differ")

        # Lowest portion_idx (0) wins
        assert merged[ts] == (22.5, 65.0, 4.2), (
            f"Expected portion 0 value (22.5, 65.0, 4.2), got {merged[ts]}"
        )

        # WARNING must be logged with the correct message format
        diff_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message and r.levelno >= logging.WARNING
        ]
        assert len(diff_warnings) == 1, (
            f"Expected exactly 1 DIFFERING-values warning, got {len(diff_warnings)}"
        )

        msg = diff_warnings[0].message
        assert ts in msg, f"Warning must name the timestamp: {msg}"
        assert "portions 0" in msg, f"Warning must name winning portion: {msg}"
        assert "and 1" in msg, f"Warning must name losing portion: {msg}"
        assert "test-differ" in msg, f"Warning must include job_id: {msg}"
        assert "keeping portion 0 value" in msg, f"Warning must say which is kept: {msg}"

    def test_differing_no_exception_raised(self):
        """Differing values must NOT raise an exception."""
        ts = "2026-07-01T12:00:00+00:00"
        portions = [
            (0, {ts: [22.5, 65.0, 4.2]}),
            (1, {ts: [99.0, 99.0, 99.0]}),
        ]

        # Must not raise
        merged = _merge_psychro_contexts(portions, job_id="test-no-exc")
        assert ts in merged

    def test_lowest_portion_idx_wins_when_presorted(self, caplog):
        """Portions pre-sorted ascending: portion 2 value wins over portion 5."""
        ts = "2026-07-01T12:00:00+00:00"
        val_2 = [20.0, 60.0, 3.0]
        val_5 = [25.0, 70.0, 5.0]

        portions = [
            (2, {ts: val_2}),
            (5, {ts: val_5}),
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            merged = _merge_psychro_contexts(portions, job_id="test-lowest")

        assert merged[ts] == (20.0, 60.0, 3.0), (
            f"Expected portion 2 value, got {merged[ts]}"
        )
        # Confirm warning mentions portions 2 and 5
        diff_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message
        ]
        assert len(diff_warnings) == 1
        assert "portions 2" in diff_warnings[0].message
        assert "and 5" in diff_warnings[0].message

    def test_multiple_timestamps_only_differing_warns(self, caplog):
        """Mixed: one timestamp identical, one differing. Only differing warns."""
        ts_same = "2026-07-01T12:00:00+00:00"
        ts_diff = "2026-07-01T12:05:00+00:00"

        portions = [
            (0, {
                ts_same: [22.0, 65.0, 4.0],
                ts_diff: [22.0, 65.0, 4.0],
            }),
            (1, {
                ts_same: [22.0, 65.0, 4.0],  # identical
                ts_diff: [99.0, 99.0, 99.0],  # different!
            }),
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            merged = _merge_psychro_contexts(portions, job_id="test-mixed")

        diff_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message
        ]
        assert len(diff_warnings) == 1
        assert ts_diff in diff_warnings[0].message
        # ts_same should NOT appear in any warning
        assert ts_same not in diff_warnings[0].message

    def test_out_of_order_input_observed_behavior(self, caplog):
        """Pass portions out of ascending portion_idx order.

        The function's contract says "MUST be sorted by portion_idx ascending".
        This test documents what actually happens when called out of order
        (which the real caller never does). We report factually, not assert
        a "should" for caller misuse.

        With out-of-order input [portion 5 first, portion 2 second]:
        - The function iterates in list order, so portion 5 is first-seen.
        - On seeing portion 2 with a differing value, it logs a warning saying
          "keeping portion 5 value" (because 5 was seen first).
        - The result keeps portion 5's value -- NOT the lowest portion_idx.

        This is correct given the contract ("MUST be sorted ascending") --
        the function trusts its caller.
        """
        ts = "2026-07-01T12:00:00+00:00"
        val_5 = [25.0, 70.0, 5.0]
        val_2 = [20.0, 60.0, 3.0]

        # Deliberately out of order: portion 5 before portion 2
        portions_out_of_order = [
            (5, {ts: val_5}),
            (2, {ts: val_2}),
        ]

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            merged = _merge_psychro_contexts(
                portions_out_of_order, job_id="test-ooo"
            )

        # First-seen (portion 5) wins because iteration is list-order.
        # This is the observed behavior, not necessarily the "correct" one —
        # but the function doesn't defend against caller misuse.
        assert merged[ts] == (25.0, 70.0, 5.0), (
            f"Out-of-order: expected first-seen (portion 5) value, got {merged[ts]}"
        )

        # A warning IS logged because the values differ
        diff_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message
        ]
        assert len(diff_warnings) == 1, (
            f"Expected 1 warning for out-of-order differing values, "
            f"got {len(diff_warnings)}"
        )
        # The warning says "keeping portion 5" (first seen) not "portion 2"
        assert "keeping portion 5 value" in diff_warnings[0].message


# =========================================================================
# c. Natural duplicates through the real async pipeline
# =========================================================================


class TestNaturalPsychroContextDuplicates:
    """Natural overlap duplicates from the real async pipeline should always
    carry identical psychro_context values for the same timestamp.

    This confirms the "never happens in practice" case is clean.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("portion_rows", [50, 288], ids=["p50", "p288"])
    async def test_natural_overlap_duplicates_are_identical(
        self, portion_rows: int, caplog
    ):
        """All naturally-occurring duplicate timestamps across portions
        have identical psychro_context values (no DIFFERING warning)."""
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result, portion_results = (
                await _run_async_pipeline_with_psychro_capture(
                    SYNTH_FIXTURE, portion_rows
                )
            )

        # Check for any DIFFERING-values warnings during the pipeline
        diff_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message
        ]
        assert len(diff_warnings) == 0, (
            f"Natural pipeline produced {len(diff_warnings)} DIFFERING-values "
            f"warnings — expected 0 (overlaps should be identical):\n"
            + "\n".join(r.message for r in diff_warnings)
        )

        # Also verify by directly inspecting per-portion psychro_contexts
        # for overlapping timestamps
        all_contexts: list[tuple[int, dict]] = []
        for pr in portion_results:
            if pr["psychro_context"]:
                all_contexts.append(
                    (pr["portion_idx"], pr["psychro_context"])
                )

        # Find overlapping timestamps
        seen: dict[str, tuple[int, list]] = {}
        mismatches = []
        for pidx, ctx in all_contexts:
            for ts, vals in ctx.items():
                if ts in seen:
                    prev_pidx, prev_vals = seen[ts]
                    if [float(v) for v in prev_vals] != [float(v) for v in vals]:
                        mismatches.append(
                            f"ts={ts}: portion {prev_pidx}={prev_vals} "
                            f"vs portion {pidx}={vals}"
                        )
                else:
                    seen[ts] = (pidx, vals)

        assert len(mismatches) == 0, (
            f"Found {len(mismatches)} natural psychro_context mismatches:\n"
            + "\n".join(mismatches)
        )

        # Print overlap stats for the report
        overlap_count = 0
        seen2: set[str] = set()
        for pidx, ctx in all_contexts:
            for ts in ctx:
                if ts in seen2:
                    overlap_count += 1
                else:
                    seen2.add(ts)
        print(
            f"\n[SYNTH portion_rows={portion_rows}] "
            f"portions={len(all_contexts)}, "
            f"total_ts={len(seen2)}, "
            f"overlap_duplicates={overlap_count}"
        )

    @pytest.mark.asyncio
    async def test_native_fixture_natural_duplicates_clean(self, caplog):
        """Native fixture A also has clean overlaps (no DIFFERING warnings)."""
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result, portion_results = (
                await _run_async_pipeline_with_psychro_capture(
                    NATIVE_FIXTURE_A, 50
                )
            )

        diff_warnings = [
            r for r in caplog.records
            if "DIFFERING values" in r.message
        ]
        assert len(diff_warnings) == 0, (
            f"Native fixture produced {len(diff_warnings)} DIFFERING-values "
            f"warnings:\n" + "\n".join(r.message for r in diff_warnings)
        )
