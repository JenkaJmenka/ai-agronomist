#!/usr/bin/env python3
"""Flow-level performance probe — Layer 1.5 of the performance routine.

`perf-probe.sh` times endpoints in isolation. Users do not experience
endpoints; they experience *journeys*, where one screen fires several requests
and the perceived wait is the critical path through them. A page can be built
entirely from sub-second endpoints and still take four seconds to become
usable. This measures that.

Requests inside a step run concurrently (as a browser would issue them), so a
step costs its slowest request, and a flow costs the sum of its steps.

Traffic goes through the deployed web BFF rather than the API directly, so the
Next.js proxy hop is included and BFF-only regressions are visible.

Usage:  python3 scripts/perf_flows.py [tenant_id] [greenhouse_id]
Exit:   0 all flows within budget · 1 a flow breached · 2 a request failed
"""
from __future__ import annotations

import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

WEB_URL = os.environ.get("PERF_FLOWS_WEB_URL", "https://web-production-cd280.up.railway.app")
REQUEST_TIMEOUT_SECONDS = 120


def _ssl_context() -> ssl.SSLContext:
    """TLS context with a working CA bundle.

    python.org framework builds ship no system CA store, so the stdlib default
    fails with CERTIFICATE_VERIFY_FAILED against any HTTPS host. Prefer
    certifi's bundle when present. Verification is never disabled — a probe
    that silently skips cert checks would be a worse bug than the one it
    is measuring.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()

# A journey should become usable in about the time a user will tolerate before
# it reads as broken. Kept deliberately tighter than the sum of endpoint
# budgets: a flow that is merely "no single slow call" is still a slow page.
DEFAULT_FLOW_BUDGET_SECONDS = 2.5


@dataclass
class Step:
    label: str
    requests: list[tuple[str, dict[str, object]]]


@dataclass
class Flow:
    name: str
    steps: list[Step]
    budget_seconds: float = DEFAULT_FLOW_BUDGET_SECONDS
    note: str = ""
    scope: dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    path: str
    seconds: float
    status: int
    bytes_read: int


def fetch(path: str, params: dict[str, object]) -> Result:
    url = f"{WEB_URL}{path}?{urllib.parse.urlencode(params)}"
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
            body = response.read()
            return Result(path, time.perf_counter() - started, response.status, len(body))
    except urllib.error.HTTPError as exc:
        return Result(path, time.perf_counter() - started, exc.code, 0)
    except Exception:
        return Result(path, time.perf_counter() - started, 0, 0)


def run_step(step: Step) -> list[Result]:
    """Issue a step's requests concurrently, as a browser would."""
    if len(step.requests) == 1:
        return [fetch(*step.requests[0])]
    with ThreadPoolExecutor(max_workers=len(step.requests)) as pool:
        return list(pool.map(lambda r: fetch(*r), step.requests))


def build_flows(tenant: str, greenhouse: str, bounds: tuple[str, str]) -> list[Flow]:
    min_ts, max_ts = bounds
    end = datetime.fromisoformat(max_ts)

    # Jitter every window so each run produces a distinct query string. These
    # endpoints are Redis-cached for 300s keyed on the full query string, so
    # without this the second run measures Redis and every flow looks instant.
    jitter = timedelta(seconds=random.randint(1, 3000))

    def window(days: int | None) -> dict[str, str]:
        start = datetime.fromisoformat(min_ts) if days is None else end - timedelta(days=days)
        return {"start": (start - jitter).isoformat(), "end": max_ts}

    scope = {"tenant_id": tenant, "greenhouse_id": greenhouse}
    full, week = window(None), window(7)
    anomaly_full = {**scope, **full, "threshold": 0.5}
    anomaly_week = {**scope, **week, "threshold": 0.5}

    return [
        Flow(
            name="Anomaly Detection — cold load, NO window selected",
            note="the default first paint; requests the entire data range",
            steps=[
                Step("shell: greenhouse list", [("/api/bff/readings/greenhouses", {"tenant_id": tenant})]),
                Step("chart + count (parallel)", [
                    ("/api/bff/readings/anomalies-in-window", {**anomaly_full, "max_points": 500}),
                    ("/api/bff/readings/anomalies-in-window", {**anomaly_full, "max_points": 100}),
                ]),
                Step("fault type + reasoning", [("/api/bff/anomalies/enriched", anomaly_full)]),
            ],
        ),
        Flow(
            name="Anomaly Detection — switch to TABLE view",
            note="the flow that returned 'Could not determine anomaly count' (HTTP 422)",
            steps=[
                Step("exact count (routing decision)", [
                    ("/api/bff/readings/anomalies-in-window", {**anomaly_full, "max_points": 100}),
                ]),
                Step("server-side groups", [("/api/bff/anomalies/groups", {**anomaly_full, "group_by": "date"})]),
            ],
        ),
        Flow(
            name="Anomaly Detection — cycle group-by date/type/sensor",
            note="each toggle refetches; user clicks these in quick succession",
            steps=[
                Step(f"group_by={mode}", [("/api/bff/anomalies/groups", {**anomaly_full, "group_by": mode})])
                for mode in ("date", "type", "sensor")
            ],
        ),
        Flow(
            name="Anomaly Detection — narrow to 7-day window",
            note="the workaround the error message told users to try",
            steps=[
                Step("chart + count (parallel)", [
                    ("/api/bff/readings/anomalies-in-window", {**anomaly_week, "max_points": 500}),
                    ("/api/bff/readings/anomalies-in-window", {**anomaly_week, "max_points": 100}),
                ]),
                Step("groups + enriched (parallel)", [
                    ("/api/bff/anomalies/groups", {**anomaly_week, "group_by": "date"}),
                    ("/api/bff/anomalies/enriched", anomaly_week),
                ]),
            ],
            budget_seconds=1.5,
        ),
        Flow(
            name="Anomaly Detection — raise score threshold",
            note="re-queries every panel on the page",
            steps=[
                Step("all panels refetch (parallel)", [
                    ("/api/bff/readings/anomalies-in-window", {**scope, **full, "threshold": 0.9, "max_points": 500}),
                    ("/api/bff/anomalies/groups", {**scope, **full, "threshold": 0.9, "group_by": "type"}),
                    ("/api/bff/anomalies/enriched", {**scope, **full, "threshold": 0.9}),
                ]),
            ],
        ),
        Flow(
            name="Active Monitoring — cold load",
            steps=[
                Step("bootstrap (parallel)", [
                    ("/api/bff/readings/greenhouses", {"tenant_id": tenant}),
                    ("/api/bff/readings/bounds", scope),
                ]),
                Step("chart data + live status (parallel)", [
                    ("/api/bff/readings/recent", {**scope, "hours": 24, "max_points": 2000}),
                    ("/api/bff/live/status", scope),
                ]),
            ],
        ),
        Flow(
            name="Active Monitoring — scrub to a historical window",
            steps=[
                Step("window fetch", [("/api/bff/readings/window", {**scope, **week, "max_points": 2000})]),
            ],
            budget_seconds=1.5,
        ),
        Flow(
            name="Metrics page",
            steps=[Step("metrics", [("/api/bff/monitoring/metrics", scope)])],
            budget_seconds=1.5,
        ),
        Flow(
            name="Configuration page",
            steps=[
                Step("config (parallel)", [
                    ("/api/bff/config/operational", scope),
                    ("/api/bff/config/crop-profiles", {}),
                ]),
            ],
            budget_seconds=1.5,
        ),
        Flow(
            name="Jobs page",
            steps=[Step("job list", [("/api/bff/jobs", {"tenant_id": tenant})])],
            budget_seconds=1.5,
        ),
        Flow(
            name="EMPTY TENANT — monitoring cold load + live start",
            note="the silent-hang journey; must be FAST and must be DISTINGUISHABLE",
            scope={"tenant_id": "tenant_b", "greenhouse_id": "gh_A"},
            steps=[
                Step("bootstrap (parallel)", [
                    ("/api/bff/readings/greenhouses", {"tenant_id": "tenant_b"}),
                    ("/api/bff/readings/bounds", {"tenant_id": "tenant_b", "greenhouse_id": "gh_A"}),
                ]),
                Step("live status", [("/api/bff/live/status", {"tenant_id": "tenant_b", "greenhouse_id": "gh_A"})]),
            ],
            budget_seconds=1.5,
        ),
    ]


def main() -> int:
    tenant = sys.argv[1] if len(sys.argv) > 1 else "tenant_1"
    greenhouse = sys.argv[2] if len(sys.argv) > 2 else "gh_A"

    print(f"Flow probe — {datetime.now(timezone.utc).strftime('%FT%TZ')}")
    print(f"WEB: {WEB_URL}   scope: {tenant}/{greenhouse}\n")

    raw = fetch("/api/bff/readings/bounds", {"tenant_id": tenant, "greenhouse_id": greenhouse})
    if raw.status != 200:
        print(f"!! cannot read bounds (http={raw.status}) — is the web service up?")
        return 2
    with urllib.request.urlopen(
        f"{WEB_URL}/api/bff/readings/bounds?"
        + urllib.parse.urlencode({"tenant_id": tenant, "greenhouse_id": greenhouse}),
        timeout=REQUEST_TIMEOUT_SECONDS,
        context=SSL_CONTEXT,
    ) as response:
        payload = json.load(response)
    if not payload.get("min_timestamp"):
        print(f"!! {tenant}/{greenhouse} has no data — nothing to measure")
        return 2
    bounds = (payload["min_timestamp"], payload["max_timestamp"])
    print(f"data range: {bounds[0]} -> {bounds[1]}\n")

    breached = failed = False
    summary: list[tuple[str, float, float]] = []

    for flow in build_flows(tenant, greenhouse, bounds):
        print(f"▸ {flow.name}")
        if flow.note:
            print(f"  ({flow.note})")
        total = 0.0
        for step in flow.steps:
            results = run_step(step)
            critical = max(r.seconds for r in results)
            total += critical
            bad = [r for r in results if r.status != 200]
            detail = "  ".join(f"{r.seconds:.2f}s/{r.status}" for r in results)
            marker = ""
            if bad:
                marker = f"   <== HTTP {', '.join(str(r.status) for r in bad)}"
                failed = True
            print(f"    {step.label:<38} {critical:6.2f}s   [{detail}]{marker}")
        verdict = ""
        if total > flow.budget_seconds:
            verdict = f"   <== OVER BUDGET ({flow.budget_seconds}s)"
            breached = True
        print(f"    {'TOTAL':<38} {total:6.2f}s{verdict}\n")
        summary.append((flow.name, total, flow.budget_seconds))

    print("─" * 96)
    print("SUMMARY (perceived time per journey)")
    for name, total, budget in sorted(summary, key=lambda s: -s[1]):
        flag = "OVER" if total > budget else "ok  "
        print(f"  {flag}  {total:6.2f}s / {budget:.1f}s   {name}")
    print("─" * 96)

    if failed:
        print("RESULT: REQUEST FAILURE — a flow hit a non-200. Contract or availability bug.")
        return 2
    if breached:
        print("RESULT: FLOW OVER BUDGET — journeys above are slower than a user will tolerate.")
        return 1
    print("RESULT: all flows within budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
