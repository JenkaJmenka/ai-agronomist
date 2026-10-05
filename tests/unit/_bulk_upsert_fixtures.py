"""Shared in-memory session fixture and base timestamp for the upsert tests.

``test_bulk_upsert.py`` and ``test_bulk_upsert_provenance.py`` are one subject
split across two files for the 300-line guardrail, so they need the same
engine fixture and the same conflict-key timestamp.

The fixture lives here rather than in ``tests/unit/conftest.py`` because the
name ``session`` is generic enough that broadcasting it to all 25-odd modules
under ``tests/unit`` would silently satisfy any future test that happens to
take a ``session`` argument meaning something else. Importing it by name keeps
the dependency explicit — pytest still discovers it, since it lands in the
importing module's namespace — and a rename fails loudly at import time.
"""

from __future__ import annotations

import datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base

BASE_TS = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()
