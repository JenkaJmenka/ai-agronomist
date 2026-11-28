"""ADR-014 A8 part 2: ``sensor_readings.fault_type`` (migration 032).

The golden (``golden/a8_fault_labels.json``) was generated from the code
BEFORE the column existed: fixture A uploaded through the real sync path,
then every read endpoint that labels a row. Writing the label at upload
time must not change one byte of that output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

import sqlalchemy as sa
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.main import app
from src.db.session import get_db
from tests.api._alembic_chain import alembic_config
from tests.api._episode_view_fixtures import (  # noqa: F401
    GH_ID,
    QUERY_THRESHOLD,
    TENANT,
    WINDOW_END,
    WINDOW_START,
    BothViews,
    both_views,
)

GOLDEN = Path(__file__).parent / "golden" / "a8_fault_labels.json"


def _snapshot(views: BothViews) -> dict[str, Any]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield views.session

    base = {
        "tenant_id": TENANT,
        "greenhouse_id": GH_ID,
        "start": WINDOW_START.isoformat(),
        "end": WINDOW_END.isoformat(),
        "threshold": QUERY_THRESHOLD,
    }
    calls = {
        "episodes": ("/api/anomalies/episodes", {}),
        "groups_type": ("/api/anomalies/groups", {"group_by": "type"}),
        "groups_sensor": ("/api/anomalies/groups", {"group_by": "sensor"}),
        "groups_date": ("/api/anomalies/groups", {"group_by": "date"}),
        "enriched": ("/api/anomalies/enriched", {}),
    }
    snap: dict[str, Any] = {"upload_response_episodes": views.all_response}
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            for name, (path, extra) in calls.items():
                resp = client.get(path, params={**base, **extra})
                assert resp.status_code == 200, resp.text
                snap[name] = resp.json()
    finally:
        app.dependency_overrides.clear()
    return json.loads(json.dumps(snap, sort_keys=True))




async def test_labels_match_pre_change_golden(both_views: BothViews) -> None:
    snap = _snapshot(both_views)
    if os.environ.get("A8_WRITE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    assert snap == json.loads(GOLDEN.read_text())
    flagged = (await both_views.session.execute(sa.text(
        "SELECT trigger_detector, fault_type FROM sensor_readings "
        "WHERE trigger_detector IS NOT NULL AND trigger_detector != ''"
    ))).all()
    assert flagged, "fixture A must flag rows"
    from src.models.scoring import BATCH_PROFILE
    from src.models.scoring.labels import legacy_upload_fault_label
    for detector, stored in flagged:
        assert stored == legacy_upload_fault_label(detector, BATCH_PROFILE)


async def test_pre_change_null_rows_keep_their_label(both_views: BothViews) -> None:
    await both_views.session.execute(sa.text("UPDATE sensor_readings SET fault_type = NULL"))
    await both_views.session.commit()
    assert _snapshot(both_views) == json.loads(GOLDEN.read_text())


def test_032_up_down_up(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'm032.db'}"
    config = alembic_config(url, monkeypatch)

    def cols() -> dict[str, Any]:
        eng = sa.create_engine(url)
        try:
            return {c["name"]: c for c in sa.inspect(eng).get_columns("sensor_readings")}
        finally:
            eng.dispose()

    command.upgrade(config, "head")
    col = cols()["fault_type"]
    assert col["nullable"] and col["type"].length == 64
    command.downgrade(config, "031")
    assert "fault_type" not in cols()
    command.upgrade(config, "head")
    assert "fault_type" in cols()
