"""Shared helpers for the tests that drive the real alembic chain.

``test_023_resumable.py``, ``test_025_migration.py`` and
``test_alembic_chain.py`` all need the same throwaway-database config, and
every one of them has to answer "what is head?" without writing the answer
down.  A literal revision id in an assertion is a landmine: it passes on the
day the migration lands and fails on the day the *next* migration lands, in a
file that has nothing to do with the change.  That has now happened twice —
``test_023_resumable.py`` broke when 025 landed, ``test_025_migration.py``
broke when 026 landed — so head is resolved from the script directory here,
once, and nowhere else.

``alembic/env.py`` cannot be imported from a test, so these helpers go through
``alembic.command`` exactly as the CLI does.  Nothing here is Postgres-aware:
the callers run SQLite and say so in their own docstrings.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]

# The revision alembic walks down to; "base" means "before the first migration".
_BASE = "base"


def alembic_config(url: str, monkeypatch) -> Config:
    """Build a Config pointed at ``url`` instead of the shared test database.

    ``alembic/env.py`` lets DATABASE_URL override sqlalchemy.url, and
    ``tests/conftest.py`` sets it process-wide — without the monkeypatch the
    migrations would run against the shared test database instead of the
    throwaway one.  It must carry the async driver: env.py imports
    ``src.db.session``, which builds an async engine from DATABASE_URL at
    import time, then downgrades the URL to sync itself.
    """
    monkeypatch.setenv("DATABASE_URL", url.replace("sqlite://", "sqlite+aiosqlite://"))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def script_heads(config: Config) -> tuple[str, ...]:
    """Every head revision the script directory reports, newest-first."""
    return tuple(ScriptDirectory.from_config(config).get_heads())


def script_head(config: Config) -> str:
    """The single head revision, resolved from the migration files on disk.

    Raises if the chain has branched: with two heads ``upgrade head`` fails
    outright, and a caller comparing against "the" head would be asserting on
    whichever one alembic happened to list first.
    """
    return ScriptDirectory.from_config(config).get_current_head()


def revisions_on_disk(config: Config) -> frozenset[str]:
    """Revision ids of every migration script alembic can see."""
    script_directory = ScriptDirectory.from_config(config)
    return frozenset(script.revision for script in script_directory.walk_revisions())


def ancestry_of(config: Config, revision: str) -> frozenset[str]:
    """``revision`` plus every revision ``upgrade <revision>`` would apply.

    This is the set membership that makes a per-migration reachability
    assertion version-agnostic: a migration is reachable exactly when it is an
    ancestor of head, no matter how many migrations land after it.
    """
    script_directory = ScriptDirectory.from_config(config)
    return frozenset(
        script.revision for script in script_directory.iterate_revisions(revision, _BASE)
    )


def stamped_revision(engine: sa.Engine) -> str | None:
    """The revision in ``alembic_version``, or None if nothing is stamped."""
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
