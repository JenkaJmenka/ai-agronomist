"""The backfill script must run from the repo root on the production container."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "backfill_drift_watch.py"


def test_help_runs_without_pythonpath() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "scripts/backfill_drift_watch.py", "--help"],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--dry-run" in proc.stdout


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_drift_watch_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_postgresql_url_normalised_to_asyncpg() -> None:
    kw = _load_script()._engine_kwargs("postgresql://u:p@h:5432/db")
    assert kw["url"] == "postgresql+asyncpg://u:p@h:5432/db"
    assert "connect_args" not in kw


def test_sslmode_moved_to_connect_args() -> None:
    kw = _load_script()._engine_kwargs("postgresql://u:p@h/db?sslmode=require")
    assert kw["url"] == "postgresql+asyncpg://u:p@h/db"
    assert kw["connect_args"] == {"ssl": "require"}


def test_sqlite_url_untouched() -> None:
    kw = _load_script()._engine_kwargs("sqlite+aiosqlite:///./x.db")
    assert kw["url"] == "sqlite+aiosqlite:///./x.db"
