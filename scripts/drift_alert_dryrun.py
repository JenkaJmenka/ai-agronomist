#!/usr/bin/env python3
"""READ-ONLY drift-watch alert dry-run report for one scope (ADR-013 §7, §8 step 9).

For every drift-watch input of one ``(tenant, greenhouse)`` this prints what the
ADR-013 alert policy would do with the stored episodes, without changing
anything: status, alert_state, the immutable calibration window end, the
out-of-sample span, every stored episode with the §5/§6/recency checks, the §3
gate result (hypothetical ``alerts_enabled=True`` and the real flag), the
out-of-sample rate against the 1.0/month budget, and how many
``anomaly_episodes`` rows the writer would write if the input were enabled.

USAGE
-----
    python scripts/drift_alert_dryrun.py --tenant acme --greenhouse gh_1
    python scripts/drift_alert_dryrun.py --tenant acme --greenhouse gh_1 --json

RAILWAY
-------
    railway run --service <api-service> python scripts/drift_alert_dryrun.py \\
        --tenant <tenant_id> --greenhouse <greenhouse_id>

SAFETY
------
- Performs NO writes. The session is never committed; everything runs in one
  transaction that is rolled back at the end. On PostgreSQL the transaction is
  also declared ``READ ONLY``. A ``before_flush`` guard aborts the run if any
  ORM object were ever dirty, new or deleted.
- Does NOT run a backfill. Episodes stored before migration 031 have no
  ``offset_robust``; the report says so and states that a backfill is needed
  for exact figures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# Make the repo root importable when run as ``python scripts/...`` (only the
# script directory is on sys.path by default). Must precede ``src`` imports.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.config as config
from src.api.services.drift_watch_alerts import (
    _spec_channels,
    _stuck_spans,
    _suppressed,
    _utc,
    load_labels,
)
from src.db.models import DriftWatchEpisode, DriftWatchInput
from src.db.session import _normalize_database_url
from src.models.drift_watch import (
    AlertState,
    DriftInputSpec,
    DriftInputState,
    GateEpisode,
    InputKind,
    count_oos_false_alarms,
    episode_natural_key,
    evaluate_alert_gate,
    passes_alert_min_offset,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr
)
logger = logging.getLogger("drift_alert_dryrun")

_WEEKS_PER_MONTH = 52.0 / 12.0
BUDGET_PER_MONTH = config.DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT / (
    config.DRIFT_WATCH_ALERT_RATE_WINDOW_WEEKS / _WEEKS_PER_MONTH
)
NEEDS_BACKFILL = "unknown — needs backfill"


def _engine_kwargs(database_url: str) -> dict:
    """Normalise a provider URL exactly as ``backfill_drift_watch.py`` does."""
    url, extra = _normalize_database_url(database_url)
    kwargs: dict = {"url": url, "echo": False}
    if extra:
        kwargs["connect_args"] = extra
    return kwargs


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else _utc(dt).isoformat()


def _spec_from_row(row: DriftWatchInput) -> DriftInputSpec:
    d = json.loads(row.spec_json)
    return DriftInputSpec(
        input_id=d["input_id"],
        kind=InputKind(d["kind"]),
        target_channel=d["target_channel"],
        channels=list(d["channels"]),
        min_shift=float(d["min_shift"]),
    )


def _gate_dict(decision: Any, raw_count: int) -> dict[str, Any]:
    return {
        "count": decision.count,
        "raw_count": raw_count,
        "next_state": decision.next_state.value,
        "reason": decision.reason,
    }


async def _report_input(
    db: AsyncSession, row: DriftWatchInput, tenant_id: str, greenhouse_id: str,
) -> dict[str, Any]:
    spec = _spec_from_row(row)
    cal = json.loads(row.calibration_json) if row.calibration_json else {}
    cal_end_raw: Optional[str] = cal.get("calibration_window_end")
    state = (
        DriftInputState.from_dict(json.loads(row.state_json))
        if row.state_json else None
    )
    as_of_ts = state.last_processed_ts if state else None

    out: dict[str, Any] = {
        "input_id": spec.input_id,
        "kind": spec.kind.value,
        "target_channel": spec.target_channel,
        "min_shift": spec.min_shift,
        "status": row.status,
        "alert_state": row.alert_state,
        "alert_state_reason": row.alert_state_reason,
        "calibration_window_end": cal_end_raw if cal_end_raw else NEEDS_BACKFILL,
        "calibration_window_end_known": bool(cal_end_raw),
        "last_processed_ts": as_of_ts,
        "oos_weeks": None,
        "episodes": [],
        "episodes_missing_offset_robust": 0,
        "needs_backfill_for_exact_figures": False,
        "gate_hypothetical_enabled": None,
        "gate_real_flag": None,
        "real_flag_value": bool(config.DRIFT_WATCH_ALERTS_ENABLED),
        "oos_alert_eligible_total": None,
        "oos_months": None,
        "oos_rate_per_month": None,
        "budget_per_month": round(BUDGET_PER_MONTH, 3),
        "would_write_alerts": 0,
        "notes": [],
    }

    ep_rows = list(
        (
            await db.execute(
                select(DriftWatchEpisode).where(
                    DriftWatchEpisode.tenant_id == tenant_id,
                    DriftWatchEpisode.greenhouse_id == greenhouse_id,
                    DriftWatchEpisode.input_id == spec.input_id,
                ).order_by(DriftWatchEpisode.first_detected_ts)
            )
        ).scalars().all()
    )
    spans = await _stuck_spans(db, tenant_id, greenhouse_id, _spec_channels(spec))
    labels = await load_labels(db, tenant_id, greenhouse_id, spec.input_id)

    as_of = _utc(pd.Timestamp(as_of_ts).to_pydatetime()) if as_of_ts else None
    cal_end = _utc(pd.Timestamp(cal_end_raw).to_pydatetime()) if cal_end_raw else None
    if as_of is not None and cal_end is not None:
        out["oos_weeks"] = round(max((as_of - cal_end).total_seconds() / 604800.0, 0.0), 2)
    recency_floor = (
        as_of - timedelta(days=config.DRIFT_WATCH_ALERT_RECENCY_DAYS) if as_of else None
    )

    gate_eps: list[GateEpisode] = []
    would_write = 0
    missing = 0
    for ep in ep_rows:
        first = _utc(ep.first_detected_ts)
        last = _utc(ep.last_confirmed_ts)
        suppressed = _suppressed(ep, spans)
        key = episode_natural_key(
            tenant_id, greenhouse_id, spec.input_id, ep.direction, ep.first_detected_ts,
        )
        label = labels.get(key)
        passes = passes_alert_min_offset(ep.offset_robust, spec.min_shift)
        recent = recency_floor is not None and last >= recency_floor
        if ep.offset_robust is None:
            missing += 1
        in_sample: Optional[bool] = None if cal_end is None else first <= cal_end
        # Mirrors write_drift_alerts: not suppressed, robust offset known and
        # >= 2 x min_shift, last confirmed inside the recency window, and
        # out-of-sample (unknown calibration_window_end writes nothing).
        writer_ok = (
            suppressed is None and ep.offset_robust is not None
            and passes and recent and in_sample is False
        )
        if writer_ok:
            would_write += 1
        gate_eps.append(
            GateEpisode(key=key, offset_robust=ep.offset_robust,
                        suppressed=suppressed is not None)
        )
        out["episodes"].append({
            "first_detected_ts": _iso(first),
            "last_confirmed_ts": _iso(last),
            "direction": ep.direction,
            "offset_robust": ep.offset_robust,
            "current_offset_native": ep.offset_estimate,
            "in_sample": in_sample,
            "out_of_sample": None if in_sample is None else not in_sample,
            "suppressed_by": suppressed,
            "stored_suppressed_by": ep.suppressed_by,
            "label": label,
            "passes_min_offset": passes,
            "min_offset_known": ep.offset_robust is not None,
            "inside_recency_window": recent,
            "writer_would_write": writer_ok,
        })

    out["episodes_missing_offset_robust"] = missing
    if missing:
        out["needs_backfill_for_exact_figures"] = True
        out["notes"].append(
            f"{missing} of {len(ep_rows)} stored episode(s) have no offset_robust "
            "(stored before migration 031). An unknown offset counts as passing the "
            "§5 limit, so counts are upper bounds. A backfill is needed for exact "
            "figures; this script does not run one."
        )
    if not cal_end_raw:
        out["needs_backfill_for_exact_figures"] = True
        out["notes"].append(
            "calibration_window_end is unknown, so no episode can be shown to be "
            "out-of-sample: counts are 0 and the input cannot become eligible. "
            "A backfill is needed."
        )
    if as_of is None:
        out["notes"].append("input has no scored state yet (no last_processed_ts).")
        return out

    out["would_write_alerts"] = would_write
    flag = bool(config.DRIFT_WATCH_ALERTS_ENABLED)
    current = AlertState(row.alert_state)
    for name, enabled in (("gate_hypothetical_enabled", True), ("gate_real_flag", flag)):
        gate_kwargs = dict(
            current_state=current, input_status=row.status,
            calibration_window_end=cal_end, as_of=as_of,
            min_shift=spec.min_shift, alerts_enabled=enabled,
        )
        decision = evaluate_alert_gate(gate_eps, labels, **gate_kwargs)
        raw = count_oos_false_alarms(
            gate_eps, labels, calibration_window_end=cal_end, as_of=as_of,
            min_shift=spec.min_shift,
        ).raw_count
        out[name] = _gate_dict(decision, raw)

    # Whole-OOS-span rate: same filters as the gate, window = the full span.
    if cal_end is not None and out["oos_weeks"]:
        whole_weeks = max(int(math.ceil(out["oos_weeks"])) + 1, 1)
        total = count_oos_false_alarms(
            gate_eps, labels, calibration_window_end=cal_end, as_of=as_of,
            min_shift=spec.min_shift, rate_window_weeks=whole_weeks,
        )
        months = out["oos_weeks"] / _WEEKS_PER_MONTH
        out["oos_alert_eligible_total"] = total.count
        out["oos_raw_total"] = total.raw_count
        out["oos_months"] = round(months, 2)
        out["oos_rate_per_month"] = round(total.count / months, 3) if months > 0 else None
    return out


async def build_report(
    db: AsyncSession, tenant_id: str, greenhouse_id: str,
) -> dict[str, Any]:
    """Build the report for one scope using SELECTs only.

    Raises ``RuntimeError`` if the session ever holds pending writes.
    """
    rows = list(
        (
            await db.execute(
                select(DriftWatchInput).where(
                    DriftWatchInput.tenant_id == tenant_id,
                    DriftWatchInput.greenhouse_id == greenhouse_id,
                ).order_by(DriftWatchInput.input_id)
            )
        ).scalars().all()
    )
    inputs = [await _report_input(db, r, tenant_id, greenhouse_id) for r in rows]
    return {
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "read_only": True,
        "flag_DRIFT_WATCH_ALERTS_ENABLED": bool(config.DRIFT_WATCH_ALERTS_ENABLED),
        "budget_per_month": round(BUDGET_PER_MONTH, 3),
        "recency_days": config.DRIFT_WATCH_ALERT_RECENCY_DAYS,
        "inputs": inputs,
    }


def _forbid_flush(session: Any, flush_context: Any, instances: Any) -> None:
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("drift_alert_dryrun is read-only: pending ORM changes found")


async def run_report(db: AsyncSession, tenant_id: str, greenhouse_id: str) -> dict[str, Any]:
    """Run :func:`build_report` inside a transaction that is always rolled back."""
    event.listen(db.sync_session, "before_flush", _forbid_flush)
    try:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            await db.execute(text("SET TRANSACTION READ ONLY"))
        return await build_report(db, tenant_id, greenhouse_id)
    finally:
        await db.rollback()
        event.remove(db.sync_session, "before_flush", _forbid_flush)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def format_text(report: dict[str, Any]) -> str:
    L: list[str] = []
    L.append(
        f"Drift-watch alert DRY RUN (read-only) tenant={report['tenant_id']!r} "
        f"greenhouse={report['greenhouse_id']!r}"
    )
    L.append(
        f"real flag DRIFT_WATCH_ALERTS_ENABLED={report['flag_DRIFT_WATCH_ALERTS_ENABLED']}  "
        f"budget={report['budget_per_month']}/mo  recency window={report['recency_days']} d"
    )
    if not report["inputs"]:
        L.append("No drift-watch inputs found for this scope (run a backfill).")
    for inp in report["inputs"]:
        L.append("")
        L.append(f"== input {inp['input_id']} ({inp['kind']}, target {inp['target_channel']}, "
                 f"min_shift {inp['min_shift']}) ==")
        L.append(f"status: {inp['status']}   alert_state: {inp['alert_state']}"
                 f" ({inp['alert_state_reason'] or 'no reason'})")
        L.append(f"calibration_window_end: {inp['calibration_window_end']}")
        L.append(f"last processed: {_fmt(inp['last_processed_ts'])}   "
                 f"out-of-sample span: {_fmt(inp['oos_weeks'])} weeks")
        L.append(f"stored episodes: {len(inp['episodes'])}")
        for e in inp["episodes"]:
            sample = {True: "in-sample", False: "out-of-sample", None: "sample unknown"}[
                e["in_sample"]
            ]
            L.append(
                f"  - {e['first_detected_ts']} dir {e['direction']} "
                f"offset_robust={_fmt(e['offset_robust'])} "
                f"current_offset_native={_fmt(e['current_offset_native'])} | {sample} | "
                f"suppressed_by={_fmt(e['suppressed_by'])} label={_fmt(e['label'])} | "
                f"passes_min_offset={e['passes_min_offset']}"
                f"{'' if e['min_offset_known'] else ' (offset unknown)'} | "
                f"in_recency_window={e['inside_recency_window']}"
            )
        for name, title in (
            ("gate_hypothetical_enabled", "gate (alerts_enabled=True, hypothetical)"),
            ("gate_real_flag", f"gate (real flag={inp['real_flag_value']})"),
        ):
            g = inp[name]
            if g is None:
                L.append(f"{title}: not evaluated")
            else:
                L.append(
                    f"{title}: count={g['count']} raw_count={g['raw_count']} "
                    f"next_state={g['next_state']} reason: {g['reason']}"
                )
        if inp["oos_rate_per_month"] is None:
            L.append(
                f"OOS rate: not computable (OOS months {_fmt(inp['oos_months'])}) "
                f"vs budget {inp['budget_per_month']}/mo"
            )
        else:
            L.append(
                f"OOS rate: {inp['oos_alert_eligible_total']} alert-eligible OOS episode(s) / "
                f"{inp['oos_months']} months = {inp['oos_rate_per_month']}/mo "
                f"vs budget {inp['budget_per_month']}/mo"
            )
        L.append(f"alerts the writer WOULD write if enabled (no write): {inp['would_write_alerts']}")
        for n in inp["notes"]:
            L.append(f"NOTE: {n}")
    return "\n".join(L)


async def run(tenant: str, greenhouse: str, database_url: str, as_json: bool) -> None:
    engine = create_async_engine(**_engine_kwargs(database_url))
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            report = await run_report(session, tenant, greenhouse)
    finally:
        await engine.dispose()
    print(json.dumps(report, indent=2, default=str) if as_json else format_text(report))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="READ-ONLY ADR-013 drift-watch alert dry-run report for one scope."
    )
    parser.add_argument("--tenant", required=True, metavar="TENANT_ID")
    parser.add_argument("--greenhouse", required=True, metavar="GREENHOUSE_ID")
    parser.add_argument("--json", action="store_true", default=False,
                        help="Machine-readable JSON instead of plain text.")
    parser.add_argument(
        "--database-url", metavar="URL", default=None,
        help=(
            "Async SQLAlchemy URL (e.g. sqlite+aiosqlite:///./app.db or "
            "postgresql+asyncpg://user:pass@host/db). "
            "Falls back to DATABASE_URL env var, then ./app.db."
        ),
    )
    args = parser.parse_args()
    db_url = args.database_url or os.environ.get("DATABASE_URL")
    if not db_url:
        db_url = "sqlite+aiosqlite:///./app.db"
        logger.warning("No --database-url or DATABASE_URL set; using default: %s", db_url)
    asyncio.run(run(args.tenant, args.greenhouse, db_url, args.json))


if __name__ == "__main__":
    main()
