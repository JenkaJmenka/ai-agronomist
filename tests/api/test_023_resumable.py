"""Regression test: migration 023 must be resumable after an interrupted run.

023 commits inside its batched backfill (``_backfill_*`` call
``connection.commit()``), which also commits the Step 1 DDL.  A run killed
mid-backfill therefore leaves ``anomaly_score_raw`` / ``score_scale`` in place
with the revision still unstamped.  Before the fix, ``op.add_column`` then
raised ``DuplicateColumnError`` on every subsequent run — in production this
failed the API deploy for two days, so migration 024 never applied and the
upload worker wrote to a ``trigger_channel`` column that did not exist.

Unlike ``test_023_migration.py``, which exercises inline copies of the backfill
helpers, this test drives the real alembic chain so the DDL step itself is
covered.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]

# Columns added by 023 (interrupted run) and 024 (never reached in production).
_COLUMNS_023 = ("anomaly_score_raw", "score_scale")
_COLUMNS_024 = ("trigger_channel", "trigger_detector", "detected_severity", "trigger_value")


def _alembic_config(url: str, monkeypatch) -> Config:
    # alembic/env.py lets DATABASE_URL override sqlalchemy.url, and conftest
    # sets it process-wide — without this the migrations would run against the
    # shared test database instead of the throwaway one.  It must carry the
    # async driver: env.py imports src.db.session, which builds an async engine
    # from DATABASE_URL at import time, then downgrades the URL to sync itself.
    monkeypatch.setenv("DATABASE_URL", url.replace("sqlite://", "sqlite+aiosqlite://"))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _sensor_reading_columns(engine: sa.Engine) -> set[str]:
    with engine.connect() as connection:
        return {col["name"] for col in sa.inspect(connection).get_columns("sensor_readings")}


def test_023_resumes_when_its_columns_already_exist(tmp_path, monkeypatch):
    """Re-running 023 over a half-applied schema must upgrade to head, not raise."""
    db_path = tmp_path / "interrupted.db"
    url = f"sqlite:///{db_path}"
    config = _alembic_config(url, monkeypatch)
    command.upgrade(config, "022")

    # Reproduce the interrupted run: DDL committed, revision never stamped.
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE sensor_readings ADD COLUMN anomaly_score_raw FLOAT")
        )
        connection.execute(
            text("ALTER TABLE sensor_readings ADD COLUMN score_scale VARCHAR(16)")
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "024", "024 must apply once 023 stops failing on its own columns"

    columns = _sensor_reading_columns(engine)
    for column in (*_COLUMNS_023, *_COLUMNS_024):
        assert column in columns, f"{column} missing after resumed upgrade"
    engine.dispose()


def test_023_still_adds_its_columns_on_a_clean_database(tmp_path, monkeypatch):
    """The guard must not skip the DDL when the columns are genuinely absent."""
    db_path = tmp_path / "clean.db"
    url = f"sqlite:///{db_path}"
    engine = sa.create_engine(url)

    command.upgrade(_alembic_config(url, monkeypatch), "head")

    columns = _sensor_reading_columns(engine)
    for column in (*_COLUMNS_023, *_COLUMNS_024):
        assert column in columns, f"{column} missing after clean upgrade"
    engine.dispose()
