# Step 1 — Pin worker CPU threads

**Effort:** ~20 minutes · **Risk:** very low · **Depends on:** nothing

---

## What you are doing and why

The background worker that processes uploaded CSV files uses PyTorch (a machine
learning library). By default PyTorch grabs several CPU cores.

We measured that this buys **nothing** here: with 1 core vs 6 cores, processing a
full year of greenhouse data took the same time (0.043 vs 0.045 milliseconds per
window). The model is very small, so extra cores sit idle.

Meanwhile the app runs on a **budget hosting plan** where the worker and the web
API share the same CPU. PyTorch taking several cores means it fights the web API
for them, making the website feel slow for no benefit.

So: tell PyTorch to use one core. Free performance win for the website, zero cost
to the worker.

The reasoning is written up in `docs/architecture/ADR-007-upload-worker-sizing.md`.

---

## Before you start

```bash
cd ~/Projects/ai-agronomist
git rev-parse --abbrev-ref HEAD          # write this branch name down
git status                                # should be clean; if not, ask before continuing
```

Establish your baseline (see README §3):

```bash
./venv/bin/python -m pytest tests/api/ tests/vendors/ tests/db/ tests/services/ tests/fixtures/ -q --timeout=600
```

Expected: `1 failed, 719 passed, 8 skipped, 1 xfailed`.

```
My baseline:  ____ passed, ____ failed
```

---

## Run it

```bash
cd ~/Projects/ai-agronomist

AGENT=devops-lead
PROMPT=docs/runbooks/prompts/step1-worker-sizing.md

SYSTEM=$(awk '/^---/{c++;next} c==2' ".claude/agents/${AGENT}.md")
claude --model claude-sonnet-4-6 --print --dangerously-skip-permissions \
  --system-prompt "$SYSTEM" "$(cat $PROMPT)"
```

Expect a few minutes. When it finishes:

```bash
cat tasks/done/devops-lead.json
git rev-parse --abbrev-ref HEAD          # same branch as before?
```

---

## Verify

**1. The setting exists and defaults to 1.**

```bash
grep -rn "TORCH_NUM_THREADS" src/
```
You want a line in `src/config.py` reading the environment variable with a default
of `"1"`, and a line in the worker applying it.

**2. Its tests pass.**

```bash
./venv/bin/python -m pytest tests/workers/test_torch_threads.py -v --timeout=120
```
All should pass. You should see four test names covering: default is 1, override
works, `0` disables it, applied once at startup.

**3. Nothing else broke.**

```bash
./venv/bin/python -m pytest tests/api/ tests/vendors/ tests/db/ tests/services/ tests/fixtures/ -q --timeout=600
```
Passed count should be your baseline **plus the new tests**. Failed count must
still be **1** — the same known SQLite failure.

**4. Speed is unchanged** (this is the actual point — the change must cost nothing):

```bash
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py -q --timeout=300
```
Should complete in roughly the same time as before (a few seconds), all passing.

---

## Definition of done

- [ ] `TORCH_NUM_THREADS` exists in `src/config.py`, default `1`
- [ ] Applied once at worker startup, not per chunk
- [ ] `TORCH_NUM_THREADS=0` leaves PyTorch alone
- [ ] The applied value is logged at startup
- [ ] `tests/workers/test_torch_threads.py` passes
- [ ] Full suite: still exactly 1 failure, and it is the SQLite NaN one
- [ ] Branch unchanged from when you started

---

## If it goes wrong

**"It changed `railway.worker.json`"** — revert that file. Replica count must stay
at 1; that is an architectural constraint, not a tuning knob.
```bash
git checkout railway.worker.json
```

**"Tests fail with a thread-count assertion"** — the test is probably asserting on
PyTorch's real global thread count, which other tests may have already changed.
The prompt asks for the setter to be mocked instead. Re-run the agent pointing this
out, or file it.

**"It edited the API as well as the worker"** — out of scope. Check with
`git status`, and revert any file under `src/api/`.

**Something else broke** — get the list and stop:
```bash
./venv/bin/python -m pytest tests/ -q --timeout=600 --ignore=tests/load 2>&1 | tail -20
```

---

## Rollback

Nothing is committed by the agent, so:
```bash
git status                # see what changed
git checkout -- <file>    # revert one file
git checkout -- .         # revert everything uncommitted  (careful: loses all local edits)
```
