#!/bin/bash
# agent-watcher.sh — waits for agent done-signals in tasks/done/, then resumes
# the orchestrator session with the results (CLAUDE.md Step 3/4 protocol).
#
# Usage:   agent-watcher.sh <session_id> <agent1> [agent2 ...]
# Signals: tasks/done/<agent>.json — written by each dispatched agent
# Log:     caller redirects stdout/stderr to tasks/watcher.log

set -u

POLL_INTERVAL_SECONDS=30
TIMEOUT_SECONDS=7200

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

SESSION_ID="${1:-}"
shift || true
AGENTS=("$@")

if [[ -z "$SESSION_ID" || ${#AGENTS[@]} -eq 0 ]]; then
  echo "usage: agent-watcher.sh <session_id> <agent1> [agent2 ...]" >&2
  exit 1
fi

echo "[watcher] $(date -u +%FT%TZ) started — session=$SESSION_ID agents=${AGENTS[*]}"

all_agents_done() {
  local agent
  for agent in "${AGENTS[@]}"; do
    [[ -f "tasks/done/$agent.json" ]] || return 1
  done
  return 0
}

ELAPSED_SECONDS=0
IS_TIMED_OUT=false
until all_agents_done; do
  if (( ELAPSED_SECONDS >= TIMEOUT_SECONDS )); then
    IS_TIMED_OUT=true
    break
  fi
  sleep "$POLL_INTERVAL_SECONDS"
  ELAPSED_SECONDS=$(( ELAPSED_SECONDS + POLL_INTERVAL_SECONDS ))
done

AGENT_RESULTS=""
for agent in "${AGENTS[@]}"; do
  if [[ -f "tasks/done/$agent.json" ]]; then
    AGENT_RESULTS+="  $agent: $(cat "tasks/done/$agent.json")"$'\n'
  else
    AGENT_RESULTS+="  $agent: {\"status\":\"timeout\",\"reason\":\"no done signal after ${TIMEOUT_SECONDS}s\"}"$'\n'
  fi
done

if [[ "$IS_TIMED_OUT" == true ]]; then
  PHASE_HEADER="Agent phase TIMED OUT after ${TIMEOUT_SECONDS}s."
else
  PHASE_HEADER="Agent phase complete."
fi

RESUME_MESSAGE="$PHASE_HEADER
Results:
$AGENT_RESULTS
Read tasks/orchestrator_state.json. Proceed to next phase."

echo "[watcher] $(date -u +%FT%TZ) resuming orchestrator session $SESSION_ID"
claude --resume "$SESSION_ID" --print --dangerously-skip-permissions "$RESUME_MESSAGE"
RESUME_EXIT_CODE=$?
echo "[watcher] $(date -u +%FT%TZ) done (claude exit code $RESUME_EXIT_CODE)"
