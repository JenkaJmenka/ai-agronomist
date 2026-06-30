"""
Tests for exception paths in metrics_persistence.py.

Targets:
  Lines 40-42 – flush_metrics_snapshot() exception + rollback
  Lines 67-69 – load_latest_snapshot() exception + return None
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Lines 40-42 – flush_metrics_snapshot exception path
# ---------------------------------------------------------------------------


class TestFlushMetricsSnapshotException:
    @pytest.mark.asyncio
    async def test_db_failure_does_not_raise(self) -> None:
        """flush_metrics_snapshot swallows DB exceptions (non-critical path)."""
        from src.monitoring.anomaly_metrics import AnomalyMetrics
        from src.monitoring.metrics_persistence import flush_metrics_snapshot

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.add.side_effect = RuntimeError("DB unavailable")
        mock_db.rollback = AsyncMock()

        m = AnomalyMetrics()
        # Should NOT raise even when DB.add() fails
        await flush_metrics_snapshot(mock_db, m)

    @pytest.mark.asyncio
    async def test_db_failure_calls_rollback(self) -> None:
        """flush_metrics_snapshot calls db.rollback() when DB write fails."""
        from src.monitoring.anomaly_metrics import AnomalyMetrics
        from src.monitoring.metrics_persistence import flush_metrics_snapshot

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.add.side_effect = RuntimeError("write error")
        mock_db.rollback = AsyncMock()

        m = AnomalyMetrics()
        await flush_metrics_snapshot(mock_db, m)

        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_commit_failure_calls_rollback(self, test_db: AsyncSession) -> None:
        """When db.commit() fails, rollback is called."""
        from src.monitoring.anomaly_metrics import AnomalyMetrics
        from src.monitoring.metrics_persistence import flush_metrics_snapshot

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.add = MagicMock()
        mock_db.commit.side_effect = RuntimeError("commit failed")
        mock_db.rollback = AsyncMock()

        m = AnomalyMetrics()
        m.record_detection(0.9, 0.8, "spike", "t1")

        await flush_metrics_snapshot(mock_db, m)
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_failure_logged(self, caplog) -> None:
        """flush_metrics_snapshot logs the exception when DB fails."""
        import logging
        from src.monitoring.anomaly_metrics import AnomalyMetrics
        from src.monitoring.metrics_persistence import flush_metrics_snapshot

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.add.side_effect = RuntimeError("boom")
        mock_db.rollback = AsyncMock()

        m = AnomalyMetrics()

        with caplog.at_level(logging.ERROR):
            await flush_metrics_snapshot(mock_db, m)

        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("flush metrics snapshot" in msg.lower() for msg in error_msgs)


# ---------------------------------------------------------------------------
# Lines 67-69 – load_latest_snapshot exception path
# ---------------------------------------------------------------------------


class TestLoadLatestSnapshotException:
    @pytest.mark.asyncio
    async def test_db_exception_propagates(self) -> None:
        """load_latest_snapshot re-raises DB errors so callers can distinguish
        a genuine error from a legitimate empty-snapshot result (None)."""
        from src.monitoring.metrics_persistence import load_latest_snapshot

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute.side_effect = RuntimeError("query failed")

        with pytest.raises(RuntimeError, match="query failed"):
            await load_latest_snapshot(mock_db)

    @pytest.mark.asyncio
    async def test_db_exception_logged(self, caplog) -> None:
        """load_latest_snapshot logs the exception before re-raising."""
        import logging
        from src.monitoring.metrics_persistence import load_latest_snapshot

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute.side_effect = RuntimeError("read error")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await load_latest_snapshot(mock_db)

        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("load metrics snapshot" in msg.lower() for msg in error_msgs)

    @pytest.mark.asyncio
    async def test_db_exception_type_preserved(self) -> None:
        """The original exception type is preserved when re-raised."""
        from src.monitoring.metrics_persistence import load_latest_snapshot

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute.side_effect = ValueError("unexpected")

        with pytest.raises(ValueError, match="unexpected"):
            await load_latest_snapshot(mock_db)
