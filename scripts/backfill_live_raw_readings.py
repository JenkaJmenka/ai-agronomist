#!/usr/bin/env python3
"""Backfill raw_readings from sensor_readings for live-persisted scopes.

BACKGROUND
----------
Before Phase 11 (dual-write), PersistenceBatcher only wrote to sensor_readings
(the canonical 4 channels).  After Phase 11, each live event is also written to
raw_readings with the full channel dict (source="live").

Because fetch_merged_readings switches to raw_df as the base when any RawReading
rows exist for a scope, scopes that were already receiving live data before Phase 11
will have a gap: canonical-only rows from the pre-Phase-11 window will be absent
from the merged output once raw rows start accumulating (the merge only uses
raw_df when it is non-empty).

This script backfills raw_readings from sensor_readings for those scopes so the
transition is seamless.  The 4 canonical channel values are packed as a JSON data
dict, source is set to "live-backfill", and ON CONFLICT DO NOTHING is used to
skip rows that were already written by the batcher or by CSV upload.

USAGE
-----
    python3 scripts/backfill_live_raw_readings.py [--tenant-id TENANT] [--dry-run]

Without --tenant-id, all tenants in sensor_readings are backfilled.
With --dry-run, only prints the rows that would be inserted (no writes).

SAFETY
------
- Idempotent: ON CONFLICT DO NOTHING on (tenant_id, greenhouse_id, timestamp).
- Does NOT run automatically from any application code path.
- Should only be invoked once per deployment that upgrades to Phase 11.
- Demo scopes (MOCK_SCADA_ENABLED tenants) can be included safely; the
  backfill source="live-backfill" distinguishes them from CSV-uploaded rows.

EXAMPLE
-------
    # Dry run — inspect candidate rows
    python3 scripts/backfill_live_raw_readings.py --dry-run

    # Backfill a single tenant
    python3 scripts/backfill_live_raw_readings.py --tenant-id acme-corp

    # Backfill all tenants
    python3 scripts/backfill_live_raw_readings.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_live_raw_readings")

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Build raw_readings rows from sensor_readings: pack canonical channels as
# a JSON data dict, set source = 'live-backfill', skip conflicts.
_BACKFILL_SQL = """
INSERT INTO raw_readings (tenant_id, greenhouse_id, timestamp, source, data)
SELECT
    sr.tenant_id,
    sr.greenhouse_id,
    sr.timestamp,
    'live-backfill' AS source,
    json_object(
        'temperature', sr.temperature,
        'humidity',    sr.humidity,
        'co2',         sr.co2,
        'light',       sr.light
    ) AS data
FROM sensor_readings sr
{where_clause}
ORDER BY sr.timestamp
ON CONFLICT (tenant_id, greenhouse_id, timestamp) DO NOTHING
"""

_DRY_RUN_SQL = """
SELECT
    sr.tenant_id,
    sr.greenhouse_id,
    sr.timestamp,
    sr.temperature,
    sr.humidity,
    sr.co2,
    sr.light
FROM sensor_readings sr
LEFT JOIN raw_readings rr
    ON  rr.tenant_id     = sr.tenant_id
    AND rr.greenhouse_id = sr.greenhouse_id
    AND rr.timestamp     = sr.timestamp
{where_clause}
WHERE rr.id IS NULL
ORDER BY sr.tenant_id, sr.greenhouse_id, sr.timestamp
LIMIT 20
"""


def _build_where(tenant_id: Optional[str]) -> str:
    if tenant_id:
        return f"WHERE sr.tenant_id = '{tenant_id}'"
    return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def backfill(tenant_id: Optional[str], dry_run: bool, database_url: str) -> None:
    engine = create_async_engine(database_url, echo=False)

    try:
        async with engine.begin() as conn:
            where = _build_where(tenant_id)

            if dry_run:
                sql = _DRY_RUN_SQL.format(where_clause=where)
                result = await conn.execute(text(sql))
                preview = result.fetchall()
                logger.info(
                    "[DRY RUN] %d rows would be backfilled (showing first 20):", len(preview)
                )
                for row in preview:
                    logger.info("  %s", dict(row._mapping))
                return

            sql = _BACKFILL_SQL.format(where_clause=where)
            result = await conn.execute(text(sql))
            rowcount = result.rowcount
            scope = f"tenant={tenant_id!r}" if tenant_id else "all tenants"
            logger.info(
                "Backfill complete: %d raw_readings rows inserted for %s "
                "(existing rows skipped via ON CONFLICT DO NOTHING).",
                rowcount,
                scope,
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill raw_readings from sensor_readings for live-persisted scopes."
    )
    parser.add_argument(
        "--tenant-id",
        metavar="TENANT",
        default=None,
        help="Restrict backfill to a single tenant (default: all tenants).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview rows that would be inserted without writing anything.",
    )
    parser.add_argument(
        "--database-url",
        metavar="URL",
        default=None,
        help=(
            "Async SQLAlchemy database URL (e.g. sqlite+aiosqlite:///./app.db or "
            "postgresql+asyncpg://user:pass@host/db). "
            "Falls back to DATABASE_URL env var, then ./app.db."
        ),
    )
    args = parser.parse_args()

    import os

    db_url = args.database_url or os.environ.get("DATABASE_URL")
    if not db_url:
        # Local default — adjust as needed.
        db_url = "sqlite+aiosqlite:///./app.db"
        logger.warning("No --database-url or DATABASE_URL set; using default: %s", db_url)

    asyncio.run(backfill(tenant_id=args.tenant_id, dry_run=args.dry_run, database_url=db_url))


if __name__ == "__main__":
    main()
