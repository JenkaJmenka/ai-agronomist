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

SQLite limitation
-----------------
The *cause* of that interruption — ``connection.commit()`` inside the
transaction alembic opens around a migration — is not reproducible here.  On
Postgres the statement after the commit dies with "Can't operate on closed
transaction inside context manager"; SQLite tolerates it and these tests pass
either way.  The Postgres CI job (``.github/workflows/postgres-tests.yml``)
seeds one row per backfill cohort before upgrading past 022, which is what
actually covers it — an empty database never enters the backfill loops at all.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
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


def _seed_backfill_rows(engine: sa.Engine) -> None:
    """Insert one row per 023 cohort so every backfill loop actually executes.

    Without rows the loops break on their first SELECT and the migration is
    never exercised past its DDL — which is how the commit-inside-alembic bug
    reached production despite a green test.
    """
    with engine.begin() as connection:
        for idx, (source, anomaly_type, score) in enumerate(
            [
                ("live", None, 0.21),          # live cohort
                (None, "heat_spike", 0.90),    # synthetic cohort
                (None, None, 0.44),            # legacy upload/batch cohort
            ]
        ):
            connection.execute(
                text(
                    "INSERT INTO sensor_readings "
                    "(tenant_id, greenhouse_id, timestamp, temperature, humidity, "
                    " co2, light, anomaly_score, source, anomaly_type) "
                    "VALUES ('t1', 'gh', :ts, 21.0, 60.0, 420.0, 500.0, "
                    "        :score, :source, :anomaly_type)"
                ),
                {
                    "ts": f"2026-01-0{idx + 1}T00:00:00",
                    "score": score,
                    "source": source,
                    "anomaly_type": anomaly_type,
                },
            )


def test_023_resumes_when_its_columns_already_exist(tmp_path, monkeypatch):
    """Re-running 023 over a half-applied schema must upgrade to head, not raise."""
    db_path = tmp_path / "interrupted.db"
    url = f"sqlite:///{db_path}"
    config = _alembic_config(url, monkeypatch)
    command.upgrade(config, "022")

    # Reproduce the interrupted run: DDL committed, revision never stamped,
    # with rows in every cohort so the backfill loops really run.
    engine = sa.create_engine(url)
    _seed_backfill_rows(engine)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE sensor_readings ADD COLUMN anomaly_score_raw FLOAT")
        )
        connection.execute(
            text("ALTER TABLE sensor_readings ADD COLUMN score_scale VARCHAR(16)")
        )

    command.upgrade(config, "head")

    # Resolved from the script directory rather than hard-coded: the intent is
    # "the chain resumed all the way to head", and a literal silently turns
    # every new migration into a failure of this test.
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == expected_head, (
        f"resumed upgrade stopped at {version!r}, expected head {expected_head!r} — "
        "every migration after 023 is blocked while 023 fails on its own columns"
    )

    columns = _sensor_reading_columns(engine)
    for column in (*_COLUMNS_023, *_COLUMNS_024):
        assert column in columns, f"{column} missing after resumed upgrade"

    # Every seeded row must be classified — an unbackfilled row means a loop
    # bailed out early instead of completing.
    with engine.connect() as connection:
        unscaled = connection.execute(
            text("SELECT count(*) FROM sensor_readings WHERE score_scale IS NULL")
        ).scalar()
    assert unscaled == 0, "backfill did not classify every row"
    engine.dispose()


def test_023_still_adds_its_columns_on_a_clean_database(tmp_path, monkeypatch):
    """The guard must not skip the DDL when the columns are genuinely absent."""
    db_path = tmp_path / "clean.db"
    url = f"sqlite:///{db_path}"
    config = _alembic_config(url, monkeypatch)
    command.upgrade(config, "022")

    engine = sa.create_engine(url)
    _seed_backfill_rows(engine)
    command.upgrade(config, "head")

    columns = _sensor_reading_columns(engine)
    for column in (*_COLUMNS_023, *_COLUMNS_024):
        assert column in columns, f"{column} missing after clean upgrade"
    engine.dispose()
