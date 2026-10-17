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

### Always run Python through `./venv/bin/python`

```bash
./venv/bin/python -m pytest tests/ -q     # correct
python3 -m pytest tests/ -q               # WRONG — system interpreter
```

Bare `python3` resolves to the system Python, which does not have this project's
dependencies. The failure mode is deceptive rather than obvious: the repo has a
top-level `alembic/` directory, so with the real package absent `import alembic`
still *succeeds* as a namespace package — `alembic.__file__` is `None` and the
error surfaces later as `ModuleNotFoundError: No module named 'alembic.config'`,
which reads like a broken install rather than the wrong interpreter.

This has already cost two sessions: migration tests were twice written off as
"pre-existing broken alembic install" and excluded from runs. Under the venv they
pass. Before reporting any test failure as pre-existing, re-run it with
`./venv/bin/python`.

The only acceptable test gate is the **full** suite — `./venv/bin/python -m pytest tests/ -q`.
Never a subset: `tests/api tests/models tests/dashboard tests/unit` silently excludes
`tests/workers/`, where a real failing assertion once hid for four commits while three
separate reports called the branch green.

### Current baseline — 3785 passed, 22 skipped, 3 xfailed, 0 failed

Measured 2026-10-17 by the orchestrator on `fix/live-feed-survives-redeploy`,
the merge of `origin/main` (`b100b77`, i.e. PRs #80 and #81) into the live-feed
work, full suite in the foreground, 357.27s:
`./venv/bin/python -m pytest tests/ -q`. That branch is PR #82, open at the time
of writing; `origin/main` itself measures `3725/22/3/0` until it lands.

How the figure is built, since three PRs landed in two days and the arithmetic
is the only way to tell a real regression from a merge artefact:
`3721` on `6119302` → `+4` from #80 (BACKLOG-5's `TestRollingDeployCarryCompat`,
pinning that a `StuckCarry` payload serialized before `recovery_channels` was
removed still deserializes after it) → `3725` → `+60` from #82 (27 live-scope
service tests, 33 boot/freshness acceptance tests) → `3785`. #81 is hook and
docs only and adds none.

**This line is the baseline. Numbers in `AGENT_MEMORY.md` are dated snapshots,
not baselines** — update this line when the figure changes, and date it.

The distinction earned its own heading on 2026-10-14. Task 4 briefed two agents
with `3432/18/5/0`, taken from the newest entry in `AGENT_MEMORY.md` — accurate
for 2026-10-07 and stale by three commits, because `593917d` had since added 804
lines of stuck-fault acceptance tests. Both agents measured `3639/22/4/0`,
reported the +207/+4/−1 as a discrepancy rather than explaining it away, and were
right. An agent handed a stale baseline either wastes a round reconciling it or,
worse, tunes toward it.

So: give agents this line, never a figure scavenged from a memory entry. Tell them
to report what they measure, and never hand them a target number — on Task 4 six
briefed premises proved wrong (a dataclass name, this baseline, "both counts exist
on both sides", a line-number reading, and two more), and every one surfaced
because an agent contradicted the brief instead of accommodating it.

## Agent model assignment

| Agent | Model | Reason |
|-------|-------|--------|
| architect | claude-fable-5 | Complex design decisions; co-runs final reviews (user directive) |
| ml-engineer | claude-opus-4-6 | Complex PyTorch, LSTM-VAE |
| backend-engineer | claude-sonnet-4-6 | Well-defined API patterns |
| qa-engineer | claude-opus-4-6 | User directive: stronger test quality — never Sonnet, never Fable inline |
| code-reviewer | claude-opus-4-6 | Deep security/correctness analysis |
| devops-lead | claude-sonnet-4-6 | Well-defined Docker patterns |
| frontend-engineer | claude-sonnet-4-6 | Next.js/React work under web/ per web/AGENTS.md |
| tech-lead | claude-fable-5 | Complex refactoring decisions; co-runs final reviews (user directive) |

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

## Agent reporting rules

**Every agent report must end with the raw output of `git status --short`** —
never a hand-written list of files touched. Agents misreport their own blast
radius: on 2026-10-07 two separate agents wrote to `AGENT_MEMORY.md` and
omitted it from a report that otherwise said "nothing else", and one of those
reports also gave the wrong line count for a file it created. The command
cannot make either mistake. A hand-written list is an agent's recollection; the
command is the repo's answer.

Also require, and reproduce yourself before relaying:

- **Measured values, never adjusted ones.** If a measurement differs from an
  expected figure in the brief, the agent reports the difference. Never tune
  code or an expected value toward a number. Several agents on Task 3 earned
  trust specifically by reporting a discrepancy, and one of those discrepancies
  (`raw_slot_count` 1897 vs a documented 1967) exposed a real design defect.
- **The orchestrator's own measurements are not exempt.** A phantom co2 episode
  that reached a commit message and a committed brief came from an orchestrator
  harness that built a bare `StuckCarry()` and so skipped pipeline setup the
  real upload path performs. Verify a harness reproduces the production path
  before trusting a number it produces — see
  `project_imputed_channel_phantom_freeze` in the session memory.
- **The full suite, never a subset**, run in the FOREGROUND. Agents that
  background the run and wait for a notification stall silently; three did on
  Task 3. A long blocking run is correct behaviour.

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
Target 90% coverage. Run: ./venv/bin/python -m pytest tests/ --cov=src --cov-fail-under=90
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
| backend-engineer | `src/api/`, `src/db/`, `src/workers/`, `src/live/`, `docs/handoff/`, `alembic/`, `alembic.ini`, `requirements*.txt`, root `README.md`, root-level `*.md` docs |
| qa-engineer | `tests/`, `docs/coverage/` |
| code-reviewer | `docs/review/` |
| devops-lead | `docker/`, `.github/`, `.husky/`, `docs/deployment/`, `docs/runbooks/` |
| tech-lead | `docs/refactor/` |
| orchestrator (you) | `AGENT_MEMORY.md`, `CLAUDE.md`, `.gitignore` — never delegate these |

Root-level config and docs had no owner until 2026-10-05, and the gap stalled
two agents in one session: devops-lead declined `requirements-dev.txt` and
`README.md` as outside `docker/`/`.github/`, then backend-engineer paused on
`alembic.ini` and `RAILWAY_HOSTING.md` for the same reason. Both were right to
ask. They now belong to backend-engineer, except `AGENT_MEMORY.md` and this
file, which stay with the orchestrator.

`src/workers/` had the same gap until 2026-10-06, found while planning the
anomaly-episode writer: the async upload path's persistence point is
`src/workers/upload_consumer.py`, which no row covered. It is API-adjacent
service code and already imports only backend-owned modules
(`src/api/services/`, `src/db/`), so it belongs to backend-engineer. Note the
asymmetry that caused the confusion: `tests/workers/` is qa-engineer's, under
`tests/`.

The lesson both times: when a task's real edit point falls outside every row,
say so and get an owner assigned. Do not pick the nearest-looking agent, and do
not quietly widen your own scope to cover it.

`alembic/` (the migration chain itself, not just `alembic.ini`) is
backend-engineer's, assigned 2026-10-10. Only `alembic.ini` had a row, so the
versions directory was uncovered -- found while renumbering the HNSW migration
on `worktree-hnsw-vector-index`. Migrations are database schema and
backend-engineer already owns `src/db/`, so they belong together.

`src/live/` is backend-engineer's, assigned 2026-10-17. It had no row at all,
which stalled the live-detector task: the fix for a live scope with no model
spans `src/live/detector_resolver.py`, `persistence.py` and `scoring.py`, and
every one of them fell outside every row. The reasoning is the same one that
assigned `src/workers/` on 2026-10-06 — it is API-adjacent service code that
already imports only backend-owned modules (`src/api/services/`, `src/db/`) —
so the two belong together.

Note the asymmetry that made the gap easy to miss, and that still holds: the
live path's *scoring* ends in `src/models/inference.py`, which is
**ml-engineer's**. A live task that changes detector loading therefore needs
both agents, and `src/config.py` constants for live behaviour are ml-engineer's
to add. Do not let a backend agent reach into either.

`docs/runbooks/` is devops-lead's, assigned 2026-10-10, with the same
cross-agent exception as `docs/handoff/`: the domain agent may revise the step
runbook covering its own area when its task says so. The directory is
deliberately mixed — `deploy.md` and `STEP-1-worker-sizing.md` are infra, while
`STEP-2`..`STEP-5` and `native-dec-fixture-baseline.md` are detector/ML
material. A single owner for the whole tree would be wrong, so devops-lead owns
the directory and its infra files, and an ML task that needs a detector runbook
says so explicitly rather than reassigning the row. It earned a row because
`deploy.md` is the live Railway apply procedure and was unowned while
`docs/deployment/railway.md` was being corrected against it.

`docs/deployment/` is devops-lead's, assigned 2026-10-10. The gap surfaced when
`docs/deployment/railway.md` was found stale after commit `2f924fa` deleted all four
`railway*.json` files: the doc still instructs setting a per-service "Config File
Path" to files that no longer exist. Deployment docs describe the same services
devops-lead already owns under `docker/`, so they belong together. Note the live
apply procedure lives in `docs/runbooks/deploy.md`, which this row does NOT cover —
if that file needs editing, get an owner assigned first.

`docs/handoff/` is backend-engineer's, with one exception to the usual
one-owner rule: another agent may add to a handoff spec when its own task
explicitly says so (Task 4 does). It is a cross-agent contract, not one
agent's working notes.

`.husky/` is devops-lead's, assigned 2026-10-16. It had no row, which surfaced
when the pre-commit hook was found to fail in every git worktree: `node_modules/`
is gitignored, so `pnpm exec lint-staged` cannot resolve and the hook aborts.
Six commits across two rounds were therefore made with `--no-verify`. The hook is
local developer CI and devops-lead already owns `.github/`, so they belong
together. Note the asymmetry that made the gap easy to miss: the `lint-staged`
*config* lives in root `package.json`, which this row does NOT cover — that was
assigned to frontend-engineer for BACKLOG-2 and still has no standing owner.

Two facts worth keeping with this row, both learned the hard way:

- **`core.hooksPath` is an absolute path into the main checkout**
  (`<main>/.husky/_`), so a worktree commit runs the MAIN checkout's copy of
  `.husky/pre-commit` with cwd set to the worktree. A hook fix is therefore
  inert in worktrees until the main checkout's own branch carries it. Anyone
  testing such a fix from a worktree beforehand will see the old hook and
  conclude it does not work.
- **Never commit with `--no-verify` if the commit touches a file the
  lint-staged glob covers.** If the hook cannot run, stop and report. "It would
  have been a no-op anyway" is a judgement made *instead of* the check, and it
  is exactly the reasoning that lets a real bypass through later.

`.gitignore` is the orchestrator's. It earned a row on 2026-10-06: `docs/*`
had silently ignored `docs/handoff/task3_4_spec.md`, so the Task 3 spec was
written locally in one session and absent from every branch in the next —
Task 3 opened by rebuilding a spec that was never committable. An ignore rule
can make an agent's finished work invisible rather than merely untracked, so
changes to it stay with the orchestrator. Verify any new negation with
`git check-ignore <path>` (no `-v`): it must print nothing and exit 1. With
`-v` it prints the matching negation rule and exits 0, which looks like
failure and is not.

---

## Deployment constraint — the API runs as a SINGLE replica

Do not scale `ai-agronomist-api` beyond one replica, and do not propose it as a
performance fix. Recorded 2026-10-17; see `ADR-009` §0.3 for the full account.

Live readings are deduplicated only by `ON CONFLICT (tenant_id, greenhouse_id,
timestamp) DO NOTHING` (`src/live/persistence.py`). That guard does not work
across replicas, because the timestamp is not a stable function of the bucket:
`ChannelMerger._emit_bucket` stamps the event with the **first raw reading's
timestamp in the bucket** (`src/live/merge.py:222`, `:266`), and the mock source
uses microsecond-precision `datetime.now(timezone.utc)`
(`src/live/sources/mock.py:169`). Two replicas therefore emit *different*
timestamps for the same logical bucket and both rows insert — N replicas means
N× the row rate, silently, with no error anywhere.

Migration `028` (boot resume) made this worse rather than better. Before it, a
multi-replica deploy still required an operator to press Go Live, and that HTTP
request landed on exactly one replica, so only one started streaming — the
invariant was enforced by accident of the request path. Now every replica
self-resumes at boot and nothing enforces it.

The cheapest candidate fix is to floor the bucket timestamp in `_emit_bucket`,
which would make `ON CONFLICT DO NOTHING` deduplicate replicas for free — but it
changes persisted timestamps, so it is deliberately not done. The alternatives
are a Redis leader lock around boot resume, or moving the connectors into
`src/workers/` with a single instance. Tracked in `AGENT_MEMORY.md` under
BACKLOG-7.

`src/live/scope_control.py:1-23` documents the related in-process constraint:
`_suppressed_scopes` is per-worker, so suppress/resume on one worker is
invisible to the others.

## Standard pipeline

```
Phase 1 — dispatch parallel agents → write state → start watcher → EXIT
Phase 2 — [watcher resumes] → read Phase 1 output → dispatch → write state → start watcher → EXIT
Phase N — [watcher resumes] → dispatch code-reviewer → write state → start watcher → EXIT
Done  — [watcher resumes] → read review → final summary → update AGENT_MEMORY.md
```
