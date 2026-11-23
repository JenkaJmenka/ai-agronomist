#!/usr/bin/env python3
"""Backfill drift-watch calibration and scoring for data uploaded before the feature existed.

BACKGROUND
----------
``run_drift_watch_shadow`` is called at the end of every new CSV upload.
Tenants whose data was uploaded before the drift-watch feature was introduced
show "No drift watch inputs found" because no upload ever triggered the shadow
run.

This script replays the full raw-readings history for one or all scopes,
walking forward in weekly chunks and calling the same calibration/scoring path
that a live upload would trigger.  The result is equivalent to what the tenant
would see had they uploaded their data in weekly batches after the feature
shipped.

USAGE
-----
    # Backfill a single scope
    python scripts/backfill_drift_watch.py --tenant acme --greenhouse gh_1

    # Backfill all scopes with raw readings
    python scripts/backfill_drift_watch.py --all

    # Dry run: report span and specs without writing
    python scripts/backfill_drift_watch.py --tenant acme --greenhouse gh_1 --dry-run

RAILWAY
-------
Run via the API service so it uses the production DATABASE_URL:

    railway run --service <api-service> python scripts/backfill_drift_watch.py \\
        --tenant <tenant_id> --greenhouse <greenhouse_id>

SAFETY
------
- Idempotent: existing DriftWatchInput and DriftWatchEpisode rows for each
  scope are deleted before the walk, so running twice produces identical rows.
- Does NOT touch sensor_readings, raw_readings, or any other table.
- Synthetic rows (source LIKE 'synthetic:%') are excluded from all frames,
  matching the existing shadow service's filter.
- --dry-run writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Make the repo root importable when run as ``python scripts/...`` (only the
# script directory is on sys.path by default). Must precede ``src`` imports.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.drift_watch_service import backfill_drift_watch
from src.db.models import Base, RawReading
from src.db.session import _normalize_database_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_drift_watch")


def _engine_kwargs(database_url: str) -> dict:
    """Normalise a provider URL (e.g. ``postgresql://``) for the async drivers.

    Reuses the API's normaliser so the script and the service agree on driver
    selection and on moving ``sslmode`` into asyncpg ``connect_args``.
    """
    url, extra = _normalize_database_url(database_url)
    kwargs: dict = {"url": url, "echo": False}
    if extra:
        kwargs["connect_args"] = extra
    return kwargs


async def _list_scopes(session: AsyncSession) -> list[tuple[str, str]]:
    """Return all (tenant_id, greenhouse_id) pairs that have raw readings."""
    stmt = (
        select(RawReading.tenant_id, RawReading.greenhouse_id)
        .where(
            or_(
                RawReading.source.is_(None),
                ~RawReading.source.startswith("synthetic:"),
            )
        )
        .distinct()
        .order_by(RawReading.tenant_id, RawReading.greenhouse_id)
    )
    result = await session.execute(stmt)
    return [(row.tenant_id, row.greenhouse_id) for row in result.all()]


async def run(
    tenant: Optional[str],
    greenhouse: Optional[str],
    all_scopes: bool,
    dry_run: bool,
    database_url: str,
) -> None:
    engine = create_async_engine(**_engine_kwargs(database_url))
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with factory() as session:
            if all_scopes:
                scopes = await _list_scopes(session)
                if not scopes:
                    logger.info("No scopes with raw readings found.")
                    return
                logger.info("Found %d scope(s) to backfill.", len(scopes))
            else:
                if not tenant or not greenhouse:
                    logger.error("--tenant and --greenhouse are required unless --all is set.")
                    sys.exit(1)
                scopes = [(tenant, greenhouse)]

            overall_episodes = 0
            for t_id, gh_id in scopes:
                logger.info(
                    "Backfilling drift watch for tenant=%r greenhouse=%r%s ...",
                    t_id,
                    gh_id,
                    " [DRY RUN]" if dry_run else "",
                )
                summary = await backfill_drift_watch(
                    session, t_id, gh_id, dry_run=dry_run
                )
                _print_summary(summary)
                overall_episodes += summary.get("episodes_recorded", 0)

            if len(scopes) > 1:
                logger.info(
                    "All scopes done. Total episodes recorded: %d", overall_episodes
                )
    finally:
        await engine.dispose()


def _print_summary(summary: dict) -> None:
    dry = summary.get("dry_run", False)
    prefix = "[DRY RUN] " if dry else ""

    logger.info("%sdata_start:  %s", prefix, summary.get("data_start"))
    logger.info("%sdata_end:    %s", prefix, summary.get("data_end"))
    logger.info("%sspan_weeks:  %s", prefix, summary.get("data_span_weeks"))

    if dry:
        logger.info(
            "%sspecs_would_be_built:        %s",
            prefix,
            summary.get("specs_would_be_built"),
        )
        logger.info(
            "%schunks_would_be_processed:   %d",
            prefix,
            summary.get("chunks_would_be_processed", 0),
        )
    else:
        logger.info("%sspecs_built:         %s", prefix, summary.get("specs_built"))
        logger.info("%schunks_processed:    %d", prefix, summary.get("chunks_processed", 0))
        logger.info("%sepisodes_recorded:   %d", prefix, summary.get("episodes_recorded", 0))
        logger.info("%sinputs_status:       %s", prefix, summary.get("inputs_status"))

    logger.info("%selapsed_seconds:     %.2f", prefix, summary.get("elapsed_seconds", 0.0))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill drift-watch calibration for data uploaded before the feature existed."
    )
    scope_group = parser.add_mutually_exclusive_group(required=True)
    scope_group.add_argument(
        "--tenant",
        metavar="TENANT_ID",
        default=None,
        help="Tenant ID to backfill (requires --greenhouse).",
    )
    scope_group.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Backfill every (tenant, greenhouse) scope that has raw readings.",
    )
    parser.add_argument(
        "--greenhouse",
        metavar="GREENHOUSE_ID",
        default=None,
        help="Greenhouse ID to backfill (required with --tenant).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report the span and specs that would be built; write nothing.",
    )
    parser.add_argument(
        "--database-url",
        metavar="URL",
        default=None,
        help=(
            "Async SQLAlchemy URL (e.g. sqlite+aiosqlite:///./app.db or "
            "postgresql+asyncpg://user:pass@host/db). "
            "Falls back to DATABASE_URL env var, then ./app.db."
        ),
    )
    args = parser.parse_args()

    if args.tenant and not args.greenhouse:
        parser.error("--greenhouse is required when --tenant is specified.")

    db_url = args.database_url or os.environ.get("DATABASE_URL")
    if not db_url:
        db_url = "sqlite+aiosqlite:///./app.db"
        logger.warning("No --database-url or DATABASE_URL set; using default: %s", db_url)

    asyncio.run(
        run(
            tenant=args.tenant,
            greenhouse=args.greenhouse,
            all_scopes=args.all,
            dry_run=args.dry_run,
            database_url=db_url,
        )
    )


if __name__ == "__main__":
    main()
