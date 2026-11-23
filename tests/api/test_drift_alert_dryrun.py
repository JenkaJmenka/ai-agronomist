"""ADR-013 §8 step 9: the read-only drift alert dry-run report.

Reuses the 36-week seeded scope from the alert-writer tests (one +3 C
paired-probe episode, out-of-sample, recent).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import src.config as config
from src.api.services.drift_watch_service import backfill_drift_watch
from src.db.models import (
    AnomalyEpisodeRecord,
    Base,
    DriftWatchEpisode,
    DriftWatchFeedback,
    DriftWatchInput,
)
from tests.api.test_drift_watch_alert_writer import G, T, db  # noqa: F401

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "drift_alert_dryrun.py"


def _load():
    spec = importlib.util.spec_from_file_location("drift_alert_dryrun_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _snapshot(db: AsyncSession) -> dict:
    counts = {}
    for table in Base.metadata.sorted_tables:
        counts[table.name] = (
            await db.execute(select(func.count()).select_from(table))
        ).scalar_one()
    inputs = (
        await db.execute(
            select(
                DriftWatchInput.input_id, DriftWatchInput.alert_state,
                DriftWatchInput.alert_state_reason, DriftWatchInput.status,
                DriftWatchInput.calibration_json, DriftWatchInput.state_json,
            ).order_by(DriftWatchInput.input_id)
        )
    ).all()
    eps = (
        await db.execute(
            select(
                DriftWatchEpisode.episode_id, DriftWatchEpisode.offset_robust,
                DriftWatchEpisode.suppressed_by,
            ).order_by(DriftWatchEpisode.episode_id)
        )
    ).all()
    return {"counts": counts, "inputs": [tuple(r) for r in inputs], "eps": [tuple(r) for r in eps]}


@pytest.mark.asyncio
async def test_report_fields_and_no_writes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", False)
    await backfill_drift_watch(db, T, G)
    mod = _load()
    before = await _snapshot(db)

    report = await mod.run_report(db, T, G)
    assert await _snapshot(db) == before
    assert not db.new and not db.dirty and not db.deleted

    assert report["read_only"] is True
    assert report["flag_DRIFT_WATCH_ALERTS_ENABLED"] is False
    inp = {i["input_id"]: i for i in report["inputs"]}["temp_paired"]
    assert inp["status"] == "armed"
    assert inp["calibration_window_end_known"] is True
    assert inp["oos_weeks"] is not None and inp["oos_weeks"] >= 12
    big = [e for e in inp["episodes"] if e["offset_robust"] is not None and abs(e["offset_robust"]) >= 1.0]
    assert len(big) == 1
    e = big[0]
    assert e["direction"] == "+" and e["out_of_sample"] is True
    assert e["passes_min_offset"] is True and e["inside_recency_window"] is True
    assert e["suppressed_by"] is None and e["label"] is None
    assert e["writer_would_write"] is True
    assert inp["would_write_alerts"] == 1

    hyp, real = inp["gate_hypothetical_enabled"], inp["gate_real_flag"]
    assert hyp["next_state"] == "eligible" and hyp["count"] >= 1
    assert real["next_state"] == "off" and "disabled globally" in real["reason"]
    assert hyp["count"] == real["count"] and hyp["raw_count"] >= hyp["count"]
    assert inp["budget_per_month"] == pytest.approx(1.0, abs=0.01)
    assert inp["oos_rate_per_month"] == pytest.approx(
        inp["oos_alert_eligible_total"] / inp["oos_months"], abs=0.01
    )
    assert inp["needs_backfill_for_exact_figures"] is False
    json.dumps(report, default=str)
    text_out = mod.format_text(report)
    assert "temp_paired" in text_out and "WOULD write" in text_out


@pytest.mark.asyncio
async def test_report_with_real_flag_on_matches_hypothetical(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)
    await backfill_drift_watch(db, T, G)
    report = await _load().run_report(db, T, G)
    inp = {i["input_id"]: i for i in report["inputs"]}["temp_paired"]
    assert inp["gate_real_flag"] == inp["gate_hypothetical_enabled"]
    assert (
        await db.execute(
            select(func.count()).select_from(AnomalyEpisodeRecord).where(
                AnomalyEpisodeRecord.detector == "drift_watch"
            )
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_missing_calibration_window_end_and_offsets(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)
    await backfill_drift_watch(db, T, G)
    row = (
        await db.execute(select(DriftWatchInput).where(DriftWatchInput.input_id == "temp_paired"))
    ).scalar_one()
    cal = json.loads(row.calibration_json)
    cal.pop("calibration_window_end")
    row.calibration_json = json.dumps(cal)
    for ep in (await db.execute(select(DriftWatchEpisode))).scalars():
        ep.offset_robust = None
    await db.commit()
    mod = _load()
    before = await _snapshot(db)

    report = await mod.run_report(db, T, G)
    assert await _snapshot(db) == before
    inp = {i["input_id"]: i for i in report["inputs"]}["temp_paired"]
    assert inp["calibration_window_end"] == "unknown — needs backfill"
    assert inp["oos_weeks"] is None and inp["oos_rate_per_month"] is None
    assert inp["gate_hypothetical_enabled"]["count"] == 0
    assert inp["gate_hypothetical_enabled"]["next_state"] == "off"
    assert inp["needs_backfill_for_exact_figures"] is True
    assert inp["episodes_missing_offset_robust"] == len(inp["episodes"]) >= 1
    assert any("backfill" in n for n in inp["notes"])
    assert all(e["in_sample"] is None for e in inp["episodes"])
    assert inp["would_write_alerts"] == 0
    assert "needs backfill" in mod.format_text(report)


@pytest.mark.asyncio
async def test_feedback_label_and_stuck_suppression_shown_without_writes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import datetime as dt

    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)
    await backfill_drift_watch(db, T, G)
    ep = (
        await db.execute(
            select(DriftWatchEpisode).where(
                DriftWatchEpisode.input_id == "temp_paired",
                DriftWatchEpisode.offset_robust > 1.0,
            )
        )
    ).scalars().one()
    db.add(DriftWatchFeedback(
        tenant_id=T, greenhouse_id=G, input_id="temp_paired", direction="+",
        first_detected_ts=ep.first_detected_ts, label="confirmed_drift",
    ))
    start = ep.last_confirmed_ts - dt.timedelta(days=3)
    db.add(AnomalyEpisodeRecord(
        id="stuck-x", tenant_id=T, greenhouse_id=G, channel="measured_plant_temp",
        detector="stuck_fault", fault_type="stuck_fault", onset_ts=start,
        end_ts=ep.last_confirmed_ts, detected_ts=start, severity="critical",
        is_ongoing=False, frozen_sample_count=10, raw_slot_count=10,
    ))
    await db.commit()
    before = await _snapshot(db)
    report = await _load().run_report(db, T, G)
    assert await _snapshot(db) == before
    inp = {i["input_id"]: i for i in report["inputs"]}["temp_paired"]
    e = [x for x in inp["episodes"] if x["offset_robust"] and x["offset_robust"] > 1.0][0]
    assert e["label"] == "confirmed_drift" and e["suppressed_by"] == "stuck_fault"
    assert e["writer_would_write"] is False and inp["would_write_alerts"] == 0


@pytest.mark.asyncio
async def test_pending_orm_change_aborts_report(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", False)
    await backfill_drift_watch(db, T, G)
    mod = _load()
    row = (await db.execute(select(DriftWatchInput))).scalars().first()
    row.alert_state = "enabled"  # dirty object: the guard must refuse to flush it
    with pytest.raises(RuntimeError):
        await mod.run_report(db, T, G)
    await db.rollback()
    assert (await db.execute(text("select count(*) from drift_watch_inputs where alert_state='enabled'"))).scalar_one() == 0


def test_help_runs_without_pythonpath() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "scripts/drift_alert_dryrun.py", "--help"],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--tenant" in proc.stdout and "--json" in proc.stdout


def test_url_handling_matches_backfill_script() -> None:
    mod = _load()
    assert mod._engine_kwargs("postgresql://u:p@h/db?sslmode=require") == {
        "url": "postgresql+asyncpg://u:p@h/db", "echo": False,
        "connect_args": {"ssl": "require"},
    }
    assert mod._engine_kwargs("sqlite+aiosqlite:///./x.db")["url"] == "sqlite+aiosqlite:///./x.db"
