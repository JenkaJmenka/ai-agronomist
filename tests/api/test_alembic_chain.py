"""Global properties of the alembic migration chain itself.

These assertions used to live inside per-migration test files as
``test_0NN_is_the_head_revision``, which made them wrong by construction: the
newest migration is only head until the next one lands, so each copy failed the
moment the chain moved on.  ``test_023_resumable.py`` broke when 025 landed and
``test_025_migration.py`` broke when 026 landed — the same regression twice, in
files that had nothing to do with either change.

The property being protected is real and worth keeping ("a migration nobody
reaches is inert"), but it is a property of the *chain*, not of any one
revision, so it belongs here once and is stated for every revision at the same
time.  The per-revision half — "this specific migration is still on the applied
path" — stays in the migration's own file, expressed as membership in the
walked chain rather than as an equality against a literal id (see
``test_025_migration.py``).

SQLite limitation
-----------------
The upgrade runs on SQLite against an empty database, so this covers chain
topology and stamping only — not Postgres-specific DDL, and not the backfill
loops, which an empty database never enters.  The Postgres CI job
(``.github/workflows/postgres-tests.yml``) is what covers those.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from tests.api._alembic_chain import (
    alembic_config,
    ancestry_of,
    revisions_on_disk,
    script_head,
    script_heads,
    stamped_revision,
)


def test_the_chain_has_exactly_one_head(tmp_path, monkeypatch):
    """Two heads make ``upgrade head`` fail outright — deploys stop dead.

    A branched chain is almost always an accident: two migrations authored in
    parallel both claiming the same ``down_revision``.  It is also how an
    orphaned migration shows up, since nothing downstream points at it.
    """
    config = alembic_config(f"sqlite:///{tmp_path / 'heads.db'}", monkeypatch)

    heads = script_heads(config)
    assert len(heads) == 1, (
        f"alembic reports {len(heads)} heads {heads!r} — `upgrade head` cannot "
        "resolve a branched chain; merge the branches with `alembic merge`"
    )


def test_upgrade_head_lands_on_the_script_head(tmp_path, monkeypatch):
    """``upgrade head`` must run the chain and stamp the head on disk.

    Compared against ``ScriptDirectory``'s head rather than a literal id: the
    intent is "the chain ran all the way to the end", and a literal turns every
    future migration into a failure of this test.
    """
    url = f"sqlite:///{tmp_path / 'chain.db'}"
    config = alembic_config(url, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
    try:
        landed_on = stamped_revision(engine)
        assert landed_on == script_head(config), (
            f"upgrade head stopped at {landed_on!r}, expected "
            f"{script_head(config)!r} — the tail of the chain never applied"
        )
    finally:
        engine.dispose()


def test_upgrade_head_leaves_no_migration_unapplied(tmp_path, monkeypatch):
    """Every migration file on disk must be on the path ``upgrade head`` walks.

    This is the general form of what each ``test_0NN_is_the_head_revision`` was
    guarding for a single revision, and unlike those it keeps holding as the
    chain grows: a migration outside head's ancestry runs nowhere — not in CI,
    not on deploy — so it is inert however carefully it was written.
    """
    url = f"sqlite:///{tmp_path / 'complete.db'}"
    config = alembic_config(url, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
    try:
        unapplied = revisions_on_disk(config) - ancestry_of(config, stamped_revision(engine))
        assert unapplied == frozenset(), (
            f"migrations {sorted(unapplied)} exist in alembic/versions but are "
            "not ancestors of the revision `upgrade head` reached — nothing "
            "applies them"
        )
    finally:
        engine.dispose()
