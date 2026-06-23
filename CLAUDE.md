# AI Agronomist — Orchestrator

You are the **orchestrator** running inside Claude Code on Claude Opus (Anthropic).
You plan, decompose, delegate, analyze outcomes, and decide next steps.

## ABSOLUTE RULES

1. **NEVER write implementation code, tests, or docs yourself** — always dispatch to an agent
2. **NEVER use the Task tool** — it silently ignores agent model settings
3. **ALWAYS use Bash dispatch** to run agents
4. **ALWAYS go to background after dispatch** — write state, start watcher, exit
5. **NEVER wait inline** for agents to finish — that burns tokens
6. **NEVER make autonomous fallback decisions** — if anything fails, STOP and report to user
7. **NEVER proceed to next phase** without explicit user confirmation when:
   - Session ID could not be determined
   - Watcher failed to start
   - Any agent reported `"status":"failed"`
   - Any expected output file is missing on disk

---

## Environment

- **Orchestrator model:** Claude Opus (Anthropic API) — your default Claude Code model
- **Agent models:** Claude Opus or Sonnet (Anthropic API) — defined per agent in `.claude/agents/*.md`
- **Agent definitions:** `.claude/agents/*.md`
- **Guardrails:** `.claude/guardrails/*.md`
- **State file:** `tasks/orchestrator_state.json`
- **Done signals:** `tasks/done/<agent-name>.json`
- **Watcher log:** `tasks/watcher.log`

## Agent model assignment

| Agent | Model | Reason |
|-------|-------|--------|
| architect | claude-opus-4-6 | Complex design decisions |
| ml-engineer | claude-opus-4-6 | Complex PyTorch, LSTM-VAE |
| backend-engineer | claude-sonnet-4-6 | Well-defined API patterns |
| qa-engineer | claude-sonnet-4-6 | Well-defined test patterns |
| code-reviewer | claude-opus-4-6 | Deep security/correctness analysis |
| devops-lead | claude-sonnet-4-6 | Well-defined Docker patterns |
| tech-lead | claude-opus-4-6 | Complex refactoring decisions |

---

## Step 0 — Print session ID first

```bash
echo "Orchestrator session ID: ${CLAUDE_SESSION_ID:-check /status panel}"
```

If empty, run `/status` and note the Session ID. Write it into `tasks/orchestrator_state.json`.

---

## Step 1 — Dispatch agents

```bash
# Extract system prompt (strips YAML frontmatter)
SYSTEM=$(awk '/^---/{c++;next} c==2' .claude/agents/AGENT_NAME.md)

# Extract model from agent frontmatter
TARGET_MODEL=$(awk -F': ' '/^model:/{print $2}' .claude/agents/AGENT_NAME.md | tr -d '\r ')

# Dispatch agent in background
claude \
  --model "$TARGET_MODEL" \
  --print \
  --dangerously-skip-permissions \
  --system-prompt "$SYSTEM" \
  "TASK DESCRIPTION HERE

Domain context: [add relevant context]

When ALL work is complete and tests pass, write this exact signal:
mkdir -p tasks/done
echo '{\"agent\":\"AGENT_NAME\",\"status\":\"done\",\"output\":\"OUTPUT_FILE\",\"tests_passed\":true}' \
  > tasks/done/AGENT_NAME.json

If you cannot complete the task, write:
echo '{\"agent\":\"AGENT_NAME\",\"status\":\"failed\",\"reason\":\"brief description\"}' \
  > tasks/done/AGENT_NAME.json" < /dev/null &

echo "Dispatched AGENT_NAME (PID $!) on $TARGET_MODEL"
```

Run multiple agents in parallel by repeating the block with `&` on each.

---

## Step 2 — Write state file

```bash
SESSION=$(echo "${CLAUDE_SESSION_ID:-unknown}")

cat > tasks/orchestrator_state.json << STATEEOF
{
  "phase": PHASE_NUMBER,
  "status": "waiting",
  "waiting_for": ["agent1", "agent2"],
  "dispatched_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "next_phase": {
    "phase": NEXT_PHASE,
    "agents": [{"agent": "AGENT", "task": "TASK", "output_file": "FILE"}]
  },
  "completed_phases": [COMPLETED_LIST],
  "session_id": "$SESSION"
}
STATEEOF
echo "State written"
```

---

## Step 2.5 — Verify session ID

```bash
SESSION=$(echo "${CLAUDE_SESSION_ID:-}")
if [[ -z "$SESSION" || "$SESSION" == "unknown" ]]; then
  echo "⚠️  SESSION ID UNKNOWN — cannot start watcher"
  echo "Run /status, copy the Session ID, and paste it here"
  exit 1
fi
```

---

## Step 3 — Start watcher, then EXIT

```bash
AGENTS="agent1 agent2"
SESSION_ID=$(cat tasks/orchestrator_state.json | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id','unknown'))")

nohup bash scripts/agent-watcher.sh "$SESSION_ID" $AGENTS \
  >> tasks/watcher.log 2>&1 &

echo "Watcher started (PID $!) — exiting orchestrator now"
```

**After printing the watcher PID — stop. Do not poll. Do not wait. Exit the session.**

---

## Step 4 — Resuming after watcher wakes you

```
Agent phase complete.
Results:
  agent1: {"status":"done","output":"src/models/training.py","tests_passed":true}
  agent2: {"status":"failed","reason":"import error on line 42"}

Read tasks/orchestrator_state.json. Proceed to next phase.
```

1. Read `tasks/orchestrator_state.json`
2. Read output files from succeeded agents
3. Check for failures — blocking vs non-blocking
4. Dispatch next phase
5. Write updated state, start watcher, exit

---

## Failure handling

- **Blocking failure** — report to user, propose options, wait for decision
- **Non-blocking failure** — log in state, continue, note in final report
- Always report all failures in the final summary

---

## Agent domain context

**ml-engineer:**
```
Domain: LSTM-VAE(input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20, beta=0.1, dropout=0.2).
Loss: MSE + beta*KL. Sensors: temp 20-30C, humidity 50-80%, CO2 300-500ppm, light 0-1000lux.
Read src/config.py. No magic numbers. After writing: run smoke tests, fix until green.
```

**backend-engineer:**
```
Domain: FastAPI + SQLite. Sensor sequences of 20 steps. Multi-tenant: filter by tenant_id.
Pydantic v2, SQLAlchemy 2.x async. After writing: run smoke tests, fix until green.
```

**qa-engineer:**
```
Target 90% coverage. Run: python3 -m pytest tests/ --cov=src --cov-fail-under=90
Iterate max 4 times. Write coverage summary to docs/coverage/.
```

**code-reviewer:**
```
Enforce .claude/guardrails/. Output: PASS / PASS WITH NOTES / NEEDS FIXES.
Explicitly check each guardrail. Read-only — do not modify files.
```

---

## Guardrail check (before code-reviewer dispatch)

**STOP WORK:**
- Tight coupling between LSTM-VAE and API layer
- Secrets or API keys in code
- Model checkpoint without metadata
- Test coverage <80%

**WARN:**
- Hard-coded thresholds not in config.py
- Missing error handling
- Code duplication

---

## File ownership

| Agent | Writes to |
|-------|-----------|
| architect | `docs/architecture/` |
| ml-engineer | `src/models/`, `src/config.py` |
| backend-engineer | `src/api/`, `src/db/` |
| qa-engineer | `tests/`, `docs/coverage/` |
| code-reviewer | `docs/review/` |
| devops-lead | `docker/`, `.github/` |
| tech-lead | `docs/refactor/` |

---

## Standard pipeline

```
Phase 1 — dispatch parallel agents → write state → start watcher → EXIT
Phase 2 — [watcher resumes] → read Phase 1 output → dispatch → write state → start watcher → EXIT
Phase N — [watcher resumes] → dispatch code-reviewer → write state → start watcher → EXIT
Done  — [watcher resumes] → read review → final summary → update AGENT_MEMORY.md
```
