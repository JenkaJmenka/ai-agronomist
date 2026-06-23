# Working with the Orchestrator via Claude Code

## Setup — one time

```bash
# 1. Copy CLAUDE.md to project root
cp CLAUDE.md ~/Projects/ai-agronomist/

# 2. Confirm agent files are in place
ls ~/Projects/ai-agronomist/.claude/agents/
# architect.md  backend-engineer.md  code-reviewer.md
# devops-lead.md  ml-engineer.md  qa-engineer.md  tech-lead.md

# 3. Confirm guardrails are in place
ls ~/Projects/ai-agronomist/.claude/guardrails/
# ml-guardrails.md  red-flags.md  universal-guardrails.md

# 4. Set your real Anthropic key (orchestrator uses this)
export ANTHROPIC_API_KEY=sk-ant-...

# 5. Do NOT set ANTHROPIC_BASE_URL in your shell
# The orchestrator must talk to Anthropic, not Ollama
# Ollama URL is only used inside the agent Bash calls
unset ANTHROPIC_BASE_URL
```

## Starting the orchestrator

```bash
cd ~/Projects/ai-agronomist
claude
```

Claude Code starts, reads `CLAUDE.md` automatically, and you are now talking
to Claude Sonnet acting as your orchestrator.

## Giving tasks

Just describe what you want in plain English:

```
> Build the complete LSTM-VAE training pipeline.
  I need: VAETrainer class, checkpoint management, TensorBoard logging,
  early stopping, and a training runner script.
```

The orchestrator will:
1. Read `AGENT_MEMORY.md` and relevant existing files
2. Show you an execution plan with phases
3. Wait for your approval
4. Dispatch agents via Bash tool calls (each runs on Gemma)
5. Pass outputs between phases
6. Report when complete

## Approving a plan

When the orchestrator shows the plan, type:
- `y` or `yes` — proceed
- `n` or `no` — cancel, give different instructions
- `auto` — proceed without asking again for this session
- Modify it — e.g. "skip the devops phase, just do ml and qa"

## Watching agents run

Open a second terminal while the orchestrator is running:

```bash
# Watch files appear in real time
watch -n 2 'find ~/Projects/ai-agronomist/src ~/Projects/ai-agronomist/tests \
  -newer ~/Projects/ai-agronomist/AGENT_MEMORY.md 2>/dev/null'

# Watch Ollama GPU utilization on Windows
# (In PowerShell on Windows machine)
nvidia-smi dmon -s u -d 1
```

## Checking agent progress

The orchestrator shows each Bash tool call output inline. You will see the
agent's Write tool calls, Bash commands (running tests), and final output.

If an agent is slow, check Windows:
```powershell
ollama ps   # shows what model is loaded and GPU/CPU split
```

## Common task patterns

### New feature from scratch
```
> Implement CSV upload endpoint for sensor data.
  It should accept a CSV file, validate the schema, store readings in SQLite,
  and return a summary of what was ingested.
```

### Fix a bug
```
> Fix the issue in src/models/lstm_vae.py where the KL loss goes negative.
  Read the current file first, identify the bug, fix it, run the tests.
```

### Review existing code
```
> Run a full code review on everything in src/models/.
  Check against our guardrails and report any issues.
```

### Update existing file
```
> Add checkpoint metadata (epoch, loss, date, hyperparameters) to the
  CheckpointManager in src/models/checkpoint.py.
  Preserve all existing functionality.
```

### Debug across files
```
> The training loop in src/models/training.py is producing NaN losses after
  epoch 3. Read training.py, lstm_vae.py, and config.py and find the root cause.
```

## Handling slow agents

Gemma on the 51/49 GPU/CPU split takes 5-20 minutes per file. This is normal.
The orchestrator shows each Bash call's output as it arrives.

If an agent times out or fails, tell the orchestrator:
```
> The ml-engineer timed out. Retry just that agent with a simpler task:
  only implement the VAETrainer.__init__ and train_epoch methods for now.
```

## Keeping context between sessions

Each `claude` session is independent. At the start of a new session:

```
> /recap
```

Or just say:
```
> Read AGENT_MEMORY.md and tell me where we left off.
```

The orchestrator reads the memory file and resumes from the last task log.

## Switching back to Python orchestrator

If you need programmatic/automated triggering (CI, webhooks, cron):

```bash
# Python script still works for non-interactive use
python3 scripts/orchestrator_v3.py --file tasks/my_task.txt
```

Both approaches share the same `.claude/agents/` definitions and
`.claude/guardrails/` files — they stay in sync automatically.

## Cost breakdown

| Action | Model | Cost |
|--------|-------|------|
| Orchestrator session (planning) | Claude Sonnet | ~$0.02-0.10/task |
| Each agent call | Gemma (Ollama) | $0 |
| Typical 3-agent task | Mixed | ~$0.05-0.15 total |

## Troubleshooting

**"I don't see CLAUDE.md being loaded"**
```bash
ls ~/Projects/ai-agronomist/CLAUDE.md  # must exist at project root
claude --version                        # must be 2.x+
```

**"Agent Bash calls fail with connection refused"**
```bash
# Verify Ollama is running on Windows
curl http://192.168.68.118:11434/api/tags
```

**"Agent wrote Python code instead of a file"**
Tell the orchestrator:
```
> The ml-engineer described the code instead of writing it.
  Re-dispatch with explicit instruction: use the Write tool to create
  src/models/training.py directly.
```

**"Orchestrator is planning but not dispatching"**
The orchestrator waits for approval by default. Type `y` or `auto-proceed`.
