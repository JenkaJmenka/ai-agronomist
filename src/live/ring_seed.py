"""Rehydrate a LiveScorer's clean-window ring from stored SensorReading rows.

Called once at scorer start so enrichment has calibration data immediately,
rather than waiting ~53 hours for the ring to fill through live scored steps.

No new table, no migration.  Reads SensorReading rows that:
  - belong to the scorer's (tenant_id, greenhouse_id) scope
  - are not synthetic (``source`` NOT LIKE ``'synthetic:%'``)
  - are not marked anomalous (``anomaly_score IS NULL`` or ``< 0.5``)

The canonical decision point is 0.5 per the SensorReading.anomaly_score
docstring ("0.5 == the producing detector's own decision point"), so rows
with ``anomaly_score >= 0.5`` are excluded as confirmed anomalous.

Rows with NULL anomaly_score (CSV uploads, legacy rows, unscored warmup
readings) are kept — the absence of a score does not prove an anomaly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import SEQ_LEN
from src.db.models import SensorReading
from src.live.attribution import CHANNEL_ORDER
from src.live.scoring import CLEAN_WINDOW_HISTORY, _fixed_baseline_noise_hit

if TYPE_CHECKING:
    from src.live.scoring import LiveScorer

logger = logging.getLogger("live.ring_seed")

# ── Module-level constants ────────────────────────────────────────────────────

# How many times more rows to query than the minimum needed for a full ring.
# Extra headroom absorbs the rows removed by the synthetic/anomaly filter and
# by the gap check without leaving the ring undersized.
_RING_SEED_QUERY_MULTIPLIER: int = 4

# A window is rejected when any consecutive-row gap within it exceeds this
# multiple of the median inter-row interval across all loaded rows.
# "A few sampling intervals" per the design note.
_RING_SEED_MAX_GAP_MULTIPLIER: int = 3

# Canonical score >= this value → row was stored as anomalous; excluded from
# seed windows so a true anomaly cannot contaminate the noise calibration ring.
# Source: SensorReading.anomaly_score docstring, "0.5 == the producing
# detector's own decision point".
_ANOMALY_SCORE_CLEAN_THRESHOLD: float = 0.5

# ── SQL helpers ───────────────────────────────────────────────────────────────

_CHANNEL_COLUMNS = [
    SensorReading.temperature,
    SensorReading.humidity,
    SensorReading.co2,
    SensorReading.light,
]


async def load_seed_windows(
    session: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    *,
    n_windows: int = CLEAN_WINDOW_HISTORY,
    seq_len: int = SEQ_LEN,
) -> list[np.ndarray]:
    """Return physical ``[seq_len × 4]`` windows for seeding a LiveScorer ring.

    Parameters
    ----------
    session:
        An open async SQLAlchemy session.
    tenant_id, greenhouse_id:
        Scope to query.
    n_windows:
        Target number of windows (default: CLEAN_WINDOW_HISTORY = 32).
    seq_len:
        Length of each window in rows (default: SEQ_LEN = 20).

    Returns
    -------
    list of np.ndarray
        Windows in ascending time order (oldest first).  May be fewer than
        ``n_windows`` if the scope has little history.  Empty list when the
        scope has no rows or the query fails.
    """
    row_limit = n_windows * seq_len * _RING_SEED_QUERY_MULTIPLIER

    stmt = (
        select(
            SensorReading.temperature,
            SensorReading.humidity,
            SensorReading.co2,
            SensorReading.light,
            SensorReading.timestamp,
        )
        .where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
        )
        # Exclude synthetic rows (same rule as _query_physical_history in
        # src/api/services/comprehensive_pipeline.py).
        .where(
            or_(
                SensorReading.source.is_(None),
                ~SensorReading.source.startswith("synthetic:"),
            )
        )
        # Exclude confirmed-anomalous rows so a stored fault episode cannot
        # enter the noise calibration ring.  NULL score (unscored) is kept.
        .where(
            or_(
                SensorReading.anomaly_score.is_(None),
                SensorReading.anomaly_score < _ANOMALY_SCORE_CLEAN_THRESHOLD,
            )
        )
        .order_by(SensorReading.timestamp.desc())
        .limit(row_limit)
    )

    result = await session.execute(stmt)
    raw_rows = result.all()

    if not raw_rows:
        logger.debug(
            "Ring seed: no rows found for %s/%s", tenant_id, greenhouse_id
        )
        return []

    # Reverse to ascending time order (query returns newest-first).
    raw_rows = list(reversed(raw_rows))

    return _cut_windows(raw_rows, seq_len=seq_len, n_windows=n_windows)


def _cut_windows(
    rows: list,
    *,
    seq_len: int,
    n_windows: int,
) -> list[np.ndarray]:
    """Cut *rows* (ascending time order) into non-overlapping ``[seq_len × 4]``
    windows, discarding windows that span a gap larger than
    ``_RING_SEED_MAX_GAP_MULTIPLIER`` times the median inter-row interval.

    Only windows where every cell is finite are kept.
    """
    if len(rows) < seq_len:
        return []

    # Compute median inter-row interval in seconds from all loaded rows.
    # Used as the gap-rejection threshold so no hard-coded cadence constant
    # is needed.
    timestamps = [r.timestamp for r in rows]
    gaps_seconds: list[float] = []
    for i in range(1, len(timestamps)):
        delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if delta > 0:
            gaps_seconds.append(delta)

    median_gap = float(np.median(gaps_seconds)) if gaps_seconds else None
    max_gap: float | None = (
        median_gap * _RING_SEED_MAX_GAP_MULTIPLIER if median_gap is not None else None
    )

    windows: list[np.ndarray] = []
    n = len(rows)
    for start in range(0, n - seq_len + 1, seq_len):
        end = start + seq_len
        slice_rows = rows[start:end]

        # Gap check: reject windows that straddle a data hole.
        if max_gap is not None:
            gap_ok = True
            for j in range(1, seq_len):
                g = (
                    slice_rows[j].timestamp - slice_rows[j - 1].timestamp
                ).total_seconds()
                if g > max_gap:
                    gap_ok = False
                    break
            if not gap_ok:
                continue

        # Build the [seq_len × 4] float matrix in CHANNEL_ORDER.
        # CHANNEL_ORDER = ["temperature", "humidity", "co2", "light"]
        mat = np.array(
            [
                [r.temperature, r.humidity, r.co2, r.light]
                for r in slice_rows
            ],
            dtype=np.float32,
        )

        # Reject windows with any non-finite value.
        if not np.all(np.isfinite(mat)):
            continue

        windows.append(mat)
        if len(windows) >= n_windows:
            break

    return windows


# ── High-level entry point ────────────────────────────────────────────────────


async def seed_scorer(
    scorer: "LiveScorer",
    tenant_id: str,
    greenhouse_id: str,
) -> None:
    """Create a one-shot DB session, load history, and seed *scorer*'s ring.

    Designed for fire-and-forget use inside ``asyncio.create_task``.  A failure
    at any stage is logged at WARNING level; the scorer continues with an empty
    ring, as before this feature existed.
    """
    try:
        from src.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            windows = await load_seed_windows(session, tenant_id, greenhouse_id)

        if windows:
            scorer.seed_clean_windows(windows)
            logger.info(
                "Ring seed: %d window(s) admitted to ring for %s/%s",
                len(scorer._clean_windows),  # actual admitted count after noise filter
                tenant_id,
                greenhouse_id,
            )
        else:
            logger.debug(
                "Ring seed: no eligible windows for %s/%s — ring stays empty",
                tenant_id,
                greenhouse_id,
            )
    except Exception:
        logger.warning(
            "Ring seed failed for %s/%s — ring starts empty",
            tenant_id,
            greenhouse_id,
            exc_info=True,
        )
