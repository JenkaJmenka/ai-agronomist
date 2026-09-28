#!/bin/bash
# perf-probe.sh — Layer 1+2 of the performance investigation routine.
#
#   Layer 1  latency matrix: every read endpoint x window width. Window width
#            is the axis that matters — every regression found so far was fast
#            at 7 days and pathological at full range, so a single-window
#            probe reports all-green on a broken deployment.
#   Layer 2  contract conformance: replays the query params the FE actually
#            builds against the real API. Catches FE/BE parameter-range drift,
#            which the hermetic Playwright suite cannot see by design.
#
# The API is no longer reachable on a public domain — it is private-network
# only. Run this FROM INSIDE RAILWAY, against the API's private domain:
#   railway ssh -s ai-agronomist-api -- scripts/perf-probe.sh
#   railway run --service ai-agronomist-api -- scripts/perf-probe.sh
# with PERF_PROBE_API_URL pointed at e.g.
#   http://ai-agronomist-api.railway.internal:8000
# Never the public domain, and never routed through the web BFF.
#
# Usage:  scripts/perf-probe.sh [tenant_id] [greenhouse_id]
# Exit:   0 all within budget · 1 budget breach · 2 contract violation
set -uo pipefail

API_URL="${PERF_PROBE_API_URL:?PERF_PROBE_API_URL is required (private Railway domain, e.g. http://ai-agronomist-api.railway.internal:8000)}"
# Never print $API_KEY, and never add `set -x` or `curl -v` to this file — both echo headers to stdout/log.
API_KEY="${PERF_PROBE_API_KEY:?PERF_PROBE_API_KEY is required}"
AUTH_HEADER=(-H "X-API-Key: $API_KEY")

TENANT="${1:-tenant_1}"
GREENHOUSE="${2:-gh_A}"
THRESHOLD="0.5"

# Budgets (seconds). A read endpoint that blocks first paint must stay under
# SLOW_BUDGET at ANY window width the UI can request by default.
SLOW_BUDGET="1.0"
WARN_BUDGET="0.5"

# Mirrors DOWNSAMPLE_MAX_POINTS_FLOOR / _CEIL in src/config.py. Kept in sync by
# the contract check below, which fails loudly if the API disagrees.
MAX_POINTS_FLOOR=100
MAX_POINTS_CEIL=10000

EXIT_CODE=0
CONTRACT_FAILED=0

hr() { printf '%.0s─' {1..96}; echo; }

# ---------------------------------------------------------------- discovery
echo "Performance probe — $(date -u +%FT%TZ)"
echo "API: $API_URL"
echo "Scope: $TENANT/$GREENHOUSE"
hr

BOUNDS=$(curl -s "${AUTH_HEADER[@]}" --max-time 30 "$API_URL/api/readings/bounds?tenant_id=$TENANT&greenhouse_id=$GREENHOUSE")
MIN_TS=$(echo "$BOUNDS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('min_timestamp') or '')" 2>/dev/null)
MAX_TS=$(echo "$BOUNDS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('max_timestamp') or '')" 2>/dev/null)

if [[ -z "$MIN_TS" || -z "$MAX_TS" ]]; then
  echo "!! No data for $TENANT/$GREENHOUSE — bounds are null."
  echo "   This is the empty-tenant state. Verify live monitoring reports a"
  echo "   DISTINGUISHABLE status here rather than an empty 200 (silent hang)."
  curl -s "${AUTH_HEADER[@]}" --max-time 30 "$API_URL/api/live/status?tenant_id=$TENANT&greenhouse_id=$GREENHOUSE"; echo
  exit 0
fi
echo "Data range: $MIN_TS -> $MAX_TS"
hr

# These endpoints are Redis-cached for REDIS_CACHE_EXPIRE_SECONDS (default 300)
# and src/api/cache_keys.py keys on the FULL query string. Re-probing the same
# URL therefore measures Redis, not the query — a 33s endpoint reports 0.29s on
# a second run and the probe declares a broken deployment healthy.
#
# So jitter `start` by a per-run offset. The query string changes (guaranteed
# cache miss -> true cold-path timing) while the window shifts by seconds
# against 5-minute-resolution data, so results stay semantically identical.
JITTER_SECONDS=$(( (RANDOM % 3000) + 1 ))

# Window matrix. "full" is the critical one: the Anomaly Detection page
# requests the entire range when no window is selected.
window_start_for() {
  python3 - "$1" "$MAX_TS" "$MIN_TS" "$JITTER_SECONDS" <<'PY'
import sys
from datetime import datetime, timedelta
span, max_ts, min_ts, jitter = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
base = datetime.fromisoformat(min_ts if span == "full" else max_ts)
if span != "full":
    base -= timedelta(days=int(span))
print((base - timedelta(seconds=jitter)).isoformat())
PY
}

enc() { python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$1"; }

probe() {
  local label="$1" url="$2" budget="${3:-$SLOW_BUDGET}"
  local out http total bytes
  out=$(curl -s "${AUTH_HEADER[@]}" -o /dev/null -w "%{http_code} %{time_total} %{size_download}" --max-time 120 "$url")
  http=$(echo "$out" | cut -d' ' -f1)
  total=$(echo "$out" | cut -d' ' -f2)
  bytes=$(echo "$out" | cut -d' ' -f3)

  local flag=""
  if [[ "$http" != "200" ]]; then
    flag="  <== HTTP $http"; EXIT_CODE=1
  elif (( $(echo "$total > $budget" | bc -l) )); then
    flag="  <== OVER BUDGET (${budget}s)"; EXIT_CODE=1
  elif (( $(echo "$total > $WARN_BUDGET" | bc -l) )); then
    flag="  <- warn"
  fi
  printf "  %-38s %6ss  %8s B  http=%s%s\n" "$label" "$total" "$bytes" "$http" "$flag"
}

# ------------------------------------------------------- Layer 1: latency
echo "LAYER 1 — latency matrix (budget ${SLOW_BUDGET}s)"
for span in 1 7 30 full; do
  START=$(window_start_for "$span")
  QS="tenant_id=$TENANT&greenhouse_id=$GREENHOUSE&start=$(enc "$START")&end=$(enc "$MAX_TS")&threshold=$THRESHOLD"
  echo
  echo " window=${span}${span:+$([ "$span" = full ] || echo d)}  ($START)"
  probe "anomalies/groups by=date"   "$API_URL/api/anomalies/groups?$QS&group_by=date"
  probe "anomalies/groups by=type"   "$API_URL/api/anomalies/groups?$QS&group_by=type"
  probe "anomalies/groups by=sensor" "$API_URL/api/anomalies/groups?$QS&group_by=sensor"
  probe "anomalies/enriched"         "$API_URL/api/anomalies/enriched?$QS"
  probe "readings/anomalies-in-window" "$API_URL/api/readings/anomalies-in-window?$QS&max_points=500"
done

echo
echo " scope-independent"
probe "readings/recent hours=24"  "$API_URL/api/readings/recent?tenant_id=$TENANT&greenhouse_id=$GREENHOUSE&hours=24&max_points=2000"
probe "readings/bounds"           "$API_URL/api/readings/bounds?tenant_id=$TENANT&greenhouse_id=$GREENHOUSE"
probe "readings/greenhouses"      "$API_URL/api/readings/greenhouses?tenant_id=$TENANT"
probe "monitoring/metrics"        "$API_URL/api/monitoring/metrics?tenant_id=$TENANT&greenhouse_id=$GREENHOUSE"
probe "live/status"               "$API_URL/api/live/status?tenant_id=$TENANT&greenhouse_id=$GREENHOUSE"
hr

# --------------------------------------------- Layer 2: contract conformance
# Asserts the API still enforces the range the FE builds against. A drift in
# either direction (FE sends out-of-range, or BE tightens the range) shows up
# here as a 422 that no mocked e2e test can ever surface.
echo "LAYER 2 — FE/BE contract conformance"
START=$(window_start_for 7)
QS="tenant_id=$TENANT&greenhouse_id=$GREENHOUSE&start=$(enc "$START")&end=$(enc "$MAX_TS")&threshold=$THRESHOLD"

contract() {
  local label="$1" url="$2" want="$3"
  local got
  got=$(curl -s "${AUTH_HEADER[@]}" -o /dev/null -w "%{http_code}" --max-time 60 "$url")
  if [[ "$got" == "$want" ]]; then
    printf "  ok    %-52s http=%s\n" "$label" "$got"
  else
    printf "  FAIL  %-52s http=%s (want %s)\n" "$label" "$got" "$want"
    CONTRACT_FAILED=1
  fi
}

contract "max_points at floor ($MAX_POINTS_FLOOR) accepted" \
  "$API_URL/api/readings/anomalies-in-window?$QS&max_points=$MAX_POINTS_FLOOR" 200
contract "max_points at ceil ($MAX_POINTS_CEIL) accepted" \
  "$API_URL/api/readings/anomalies-in-window?$QS&max_points=$MAX_POINTS_CEIL" 200
contract "max_points below floor rejected" \
  "$API_URL/api/readings/anomalies-in-window?$QS&max_points=$((MAX_POINTS_FLOOR-1))" 422

# Three-way floor reconciliation. The FE mirrors the floor as a TS literal, the
# BE declares it in config.py — but the DEPLOYED value comes from the
# DOWNSAMPLE_MAX_POINTS_FLOOR env var and can differ from BOTH. Only the live
# API is authoritative, so probe it rather than trusting either constant.
echo
echo "  floor reconciliation (FE literal vs BE source vs deployed API):"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FE_FLOOR=$(grep -rhoE 'DOWNSAMPLE_MAX_POINTS_FLOOR\s*=\s*[0-9]+' "$REPO_ROOT/web" 2>/dev/null \
  | grep -oE '[0-9]+$' | sort -u | head -1)
BE_FLOOR=$(grep -oE 'DOWNSAMPLE_MAX_POINTS_FLOOR.*"([0-9]+)"' "$REPO_ROOT/src/config.py" 2>/dev/null \
  | grep -oE '"[0-9]+"' | tr -d '"' | head -1)
printf "    FE literal      : %s\n" "${FE_FLOOR:-NOT FOUND}"
printf "    BE source default: %s\n" "${BE_FLOOR:-NOT FOUND}"

if [[ -z "$FE_FLOOR" ]]; then
  echo "    FAIL  no FE floor constant found — the FE is sending unguarded literals"
  CONTRACT_FAILED=1
else
  # Authoritative: the deployed API accepts FE_FLOOR and rejects FE_FLOOR-1.
  at_floor=$(curl -s "${AUTH_HEADER[@]}" -o /dev/null -w "%{http_code}" --max-time 60 \
    "$API_URL/api/readings/anomalies-in-window?$QS&max_points=$FE_FLOOR")
  below=$(curl -s "${AUTH_HEADER[@]}" -o /dev/null -w "%{http_code}" --max-time 60 \
    "$API_URL/api/readings/anomalies-in-window?$QS&max_points=$((FE_FLOOR-1))")
  if [[ "$at_floor" == "200" && "$below" == "422" ]]; then
    echo "    ok    deployed API floor is exactly $FE_FLOOR — FE constant is correct"
  elif [[ "$at_floor" != "200" ]]; then
    echo "    FAIL  deployed API REJECTS the FE's floor $FE_FLOOR (http=$at_floor)"
    echo "          the deployed DOWNSAMPLE_MAX_POINTS_FLOOR env var is HIGHER than the FE thinks"
    CONTRACT_FAILED=1
  else
    echo "    warn  deployed floor is BELOW $FE_FLOOR (${FE_FLOOR}-1 -> $below); FE is over-conservative, not broken"
  fi
  [[ -n "$BE_FLOOR" && "$FE_FLOOR" != "$BE_FLOOR" ]] && \
    echo "    warn  FE literal ($FE_FLOOR) != BE source default ($BE_FLOOR) — intentional only if env-overridden"
fi

hr
if (( CONTRACT_FAILED )); then
  echo "RESULT: CONTRACT VIOLATION — FE and BE disagree on a parameter range."
  exit 2
fi
if (( EXIT_CODE )); then
  echo "RESULT: BUDGET BREACH — see rows marked OVER BUDGET above."
  exit 1
fi
echo "RESULT: all endpoints within budget, contract intact."
