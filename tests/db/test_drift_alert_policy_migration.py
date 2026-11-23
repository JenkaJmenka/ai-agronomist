"""Migration 031: alert_state, robust offset, feedback table (SQLite chain)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from tests.api._alembic_chain import alembic_config, ancestry_of, stamped_revision


def _cols(url: str, table: str) -> set[str]:
    eng = sa.create_engine(url)
    try:
        return {c["name"] for c in sa.inspect(eng).get_columns(table)}
    finally:
        eng.dispose()


def test_031_adds_columns_and_feedback_table(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'm031.db'}"
    config = alembic_config(url, monkeypatch)
    command.upgrade(config, "head")
    eng0 = sa.create_engine(url)
    assert "031" in ancestry_of(config, stamped_revision(eng0))
    eng0.dispose()
    assert {"alert_state", "alert_state_reason", "alert_state_changed_at"} <= _cols(url, "drift_watch_inputs")
    assert {"offset_robust", "suppressed_by"} <= _cols(url, "drift_watch_episodes")
    assert {"label", "note", "created_by", "created_at"} <= _cols(url, "drift_watch_feedback")

    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO drift_watch_inputs (tenant_id, greenhouse_id, input_id, spec_json) "
            "VALUES ('t','g','i','{}')"))
        assert conn.execute(sa.text("SELECT alert_state FROM drift_watch_inputs")).scalar_one() == "off"
        ins = ("INSERT INTO drift_watch_feedback (tenant_id, greenhouse_id, input_id, direction,"
               " first_detected_ts, label) VALUES ('t','g','i','+','2026-01-01 00:00:00','%s')")
        conn.execute(sa.text(ins % "false_alarm"))
    for stmt, exc in ((ins % "false_alarm", "UNIQUE"), (ins.replace("'+'", "'-'") % "bogus", "CHECK")):
        try:
            with eng.begin() as conn:
                conn.execute(sa.text(stmt))
        except sa.exc.IntegrityError as e:
            assert exc in str(e).upper()
        else:
            raise AssertionError("expected IntegrityError")

    command.downgrade(config, "030")
    assert "alert_state" not in _cols(url, "drift_watch_inputs")
    assert "offset_robust" not in _cols(url, "drift_watch_episodes")
    command.upgrade(config, "head")
    eng.dispose()
