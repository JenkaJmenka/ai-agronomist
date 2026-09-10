# Runbooks — Upload Detector Convergence

These runbooks describe five pieces of work, in order. Each one has a paired
**agent prompt** (a text file you hand to an AI coding agent) and a **human
procedure** (the exact commands you run, and how to tell whether it worked).

**You do not need to understand this project to follow them.** Everything you
need is below or in the individual step file.

---

## 1. What this project is, in one page

**AI Agronomist** monitors greenhouse sensors and raises alerts when something
is wrong with the *equipment* — not just when a reading looks high or low.

A greenhouse climate computer (brands: Priva, Hoogendoorn, Connext) logs
temperature, humidity, CO2 and light every 5 minutes. A grower exports a CSV and
uploads it here. The system scores it and lists anomalies.

The product's whole reason to exist is catching faults that a normal threshold
alarm cannot. The flagship example, which is real and lives in this repo's test
fixtures: a humidity sensor **froze at exactly 100.0% for 163.83 hours**
(2026-12-18 13:00 → 2026-12-25 08:55 UTC). 100% humidity is a perfectly
plausible greenhouse reading, so no min/max alarm would ever fire — but the
sensor was dead for a week, and every ventilation and dehumidification decision
made from it was wrong.

### The two code paths that matter

| Path | What it is | Detectors it runs |
|---|---|---|
| **Upload path** | A grower uploads a CSV. Streams in 5,000-row chunks. | 3 |
| **Live path** | Real-time feed from a climate computer, one reading at a time. | 18 |

The upload path is the one growers actually use, and it runs **3 of the
product's 18 detectors**. Closing that gap is what these five steps are about.

Key entry point, if you want to look at one thing:
`src/api/services/upload_portions.py` → the `score_portion` function. That is
where upload scoring happens.

### Two test fixtures you will see constantly

| File | What it contains |
|---|---|
| `tests/fixtures/native_dec_stuck_fault.csv` | 12 days containing the real 163.83 h frozen-sensor fault. **Detectors SHOULD fire here.** |
| `tests/fixtures/native_july_clearsky.csv` | 7 days of healthy summer weather, no fault. **Detectors should stay SILENT here.** |

Both were cut from a genuine year-long greenhouse export. Their exact measured
properties are recorded in `tests/fixtures/NATIVE_FIXTURES.md`. **Never change a
number in that file to make a test pass** — it is the ground truth everything
else is checked against.

---

## 2. One-time setup

Run these once. macOS or Linux. You need Python 3.11 and git.

```bash
cd ~/Projects/ai-agronomist          # or wherever the repo lives

# Python environment
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Point the app at a local SQLite database and create the tables
export DATABASE_URL=sqlite+aiosqlite:///./greenhouse.db
./venv/bin/alembic upgrade head
```

Check it worked — this must print a version and then `OK`:

```bash
./venv/bin/python --version
./venv/bin/python -c "import torch, pandas; print('OK')"
```

> **Note on `./venv/bin/python`.** Every command in these runbooks uses that
> exact path instead of plain `python`. This is deliberate — it guarantees you
> are using this project's Python and not the system one. Do not substitute
> `python` or `python3`.

You do **not** need Docker, Postgres, RabbitMQ or Redis for any of these five
steps. Everything runs against local SQLite.

---

## 3. Establish your baseline before you change anything

**Do this every time, before starting a step.** You cannot tell whether you
broke something if you never knew what passing looked like.

```bash
cd ~/Projects/ai-agronomist
./venv/bin/python -m pytest tests/api/ tests/vendors/ tests/db/ tests/services/ tests/fixtures/ -q --timeout=600
```

Takes about 35 seconds. As of 2026-09-10 the expected result is:

```
1 failed, 719 passed, 8 skipped, 1 xfailed
```

**That one failure is expected and is not your problem.** It is
`test_023_migration.py::TestNonFiniteGuard::test_nan_in_live_row_becomes_null_with_unknown_scale`.
SQLite silently converts the special float value `NaN` to `NULL` when writing to
a numeric column, so the situation the test tries to create cannot exist on
SQLite. It needs PostgreSQL to run properly. Leave it alone.

Write your numbers down:

```
Before I started:  ____ passed, ____ failed
```

Any failure **other** than that one, appearing after your change, means your
change broke something.

---

## 4. How to run an agent

Each step has a prompt file under `docs/runbooks/prompts/`. To hand it to an agent:

```bash
cd ~/Projects/ai-agronomist

AGENT=ml-engineer                                  # the step file tells you which
PROMPT=docs/runbooks/prompts/step3-recalibrate-detectors.md   # the step file tells you which

SYSTEM=$(awk '/^---/{c++;next} c==2' ".claude/agents/${AGENT}.md")
claude --model claude-opus-4-6 --print --dangerously-skip-permissions \
  --system-prompt "$SYSTEM" "$(cat $PROMPT)"
```

What that does, line by line:
- `AGENT=` picks a role. Each role is allowed to touch different folders.
- The `awk` line pulls the agent's job description out of `.claude/agents/`.
- `claude ...` runs it. It edits files directly in the repo.

**Before you start, and again after it finishes, run this:**

```bash
git rev-parse --abbrev-ref HEAD
```

Write the branch name down. If it is different afterwards, **stop and read
§6 "Branch moved"** — this has happened before and silently sent four commits
to the wrong branch.

### If the agent stops early

You may see `You've hit your session limit · resets 4:30am` or
`You're out of usage credits`. This is normal and does not mean the work is lost.
The agent edits files as it goes, so partial work is on disk.

Do this:
1. `git status` — see what it changed.
2. Run the step's verification commands. Sometimes it finished the work and only
   failed to report.
3. If verification passes, you are done. If not, wait for the reset and run the
   same command again.

---

## 5. The five steps, in order

Do them in this order. The dependencies are real, not bureaucratic.

| # | Step | Runbook | Depends on |
|---|---|---|---|
| 1 | Pin worker CPU threads | `STEP-1-worker-sizing.md` | nothing |
| 2 | Per-tenant baseline | `STEP-2-tenant-baseline.md` | nothing |
| 3 | Recalibrate two broken detectors | `STEP-3-recalibrate-detectors.md` | **Step 2** |
| 4 | Add the new detectors to uploads | `STEP-4-detector-fanout.md` | **Step 3** |
| 5 | One incident per event, not per row | `STEP-5-episode-incidents.md` | Step 4 |

Each runbook names the agent prompt it uses; all five live in
`docs/runbooks/prompts/`. The runbook is for you; the prompt is for the agent.

**Why step 3 needs step 2.** Two detectors are currently broken — one fires on
70% of *healthy* data. Fixing them means retuning their thresholds. But they are
tuned against a statistical "picture of normal" which today is 10 randomly
generated fake readings. Retuning against a fake normal means doing the work
twice. Step 2 replaces the fake normal with the greenhouse's real one.

Steps 1 and 2 are independent — either order, or at the same time.

---

## 6. Troubleshooting

### Branch moved
```bash
git rev-parse --abbrev-ref HEAD     # not the branch you started on?
git reflog -10                      # find where you were
git branch -v | grep ahead          # a branch showing "[ahead N]" holds your commits
```
Nothing is lost — commits still exist. Get help before running any `git reset`,
`git checkout -f`, or `git branch -f`. Those are the commands that *do* lose work.

### Tests suddenly much slower
A full run is ~35 seconds. If it takes many minutes, something is looping.
Ctrl-C and report it. Do not wait it out — this exact symptom was a real
infinite loop in a database migration (fixed 2026-09-09, took 1250 s).

### `ModuleNotFoundError: No module named 'src'`
You are running a script from the wrong directory, or without the path set.
Always run from the repo root, and for standalone scripts prefix with
`PYTHONPATH=.`:
```bash
cd ~/Projects/ai-agronomist
PYTHONPATH=. ./venv/bin/python some_script.py
```

### A test fails and you are told the expected value is wrong
It usually is not. Ground-truth numbers live in
`tests/fixtures/NATIVE_FIXTURES.md` and were measured from real data. If code
disagrees with them, fix the code. Changing a fixture number to make a test pass
has already hidden two real bugs in this project.

---

## 7. Glossary

| Term | Meaning |
|---|---|
| **Detector** | One piece of code that decides "is this reading abnormal". There are 18. |
| **Sequence / window** | 20 consecutive readings (100 minutes at 5-min cadence). Most detectors judge a window, not a single reading. |
| **Chunk** | 5,000 rows. Uploads are processed a chunk at a time so a year of data never has to fit in memory at once. |
| **Carry** | State handed from one chunk to the next, so a fault spanning a chunk boundary is not lost. Look for `StatCarry`, `StuckCarry`. |
| **Episode** | One continuous real-world event, grouped from many individual alerts. A week-long frozen sensor is one episode, not 2,000 alerts. |
| **Baseline** | A statistical picture of "normal" that some detectors compare against. Currently fake — Step 2 fixes it. |
| **Stuck / flatline fault** | A sensor reporting the same value forever because it has died. The flagship fault this product catches. |
| **False positive (FP)** | An alert on healthy data. The number to drive down. |
| **Fixture** | A small, committed data file used by tests so results are reproducible. |
| **ADR** | Architecture Decision Record — a short doc in `docs/architecture/` saying what was decided and why. |
