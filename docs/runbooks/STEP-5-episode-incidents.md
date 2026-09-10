# Step 5 — One incident per event, not one per reading

**Effort:** ~2 hours · **Risk:** medium · **Depends on:** Step 4

---

## What you are doing and why

When the system finds a problem it writes an "incident" record to the database — the
thing an operator reads, and the thing the dashboard lists.

Currently, on the real-time path, it writes **one incident per 20-reading window**.
For a sensor that was broken for a week, that is thousands of near-identical
database rows describing one event. It is also the specific reason the full detector
set was never brought to the upload path: writing thousands of rows per uploaded
file is too slow.

The fix already half-exists. Earlier work taught the system to group alerts into
**episodes** — one continuous real-world event, with a start, an end and a duration.
The frozen-sensor fault is one episode lasting 163.83 hours, not 2,000 alerts.

This step makes incidents follow episodes: **one incident per episode.**

For the December test file the design estimates this cuts incident writes from
**2,000+ down to 4–8**. That estimate is unverified — the step measures the real
numbers.

### The subtle part worth understanding

The web API matches alerts to incidents by looking for an incident within
**15 minutes** of the alert. That works when an incident is a moment in time.

An episode is not a moment — it is a span, and one of ours is a week long. If the
matching logic is left alone, a week-long incident may fail to match its own alerts,
and the dashboard would show alerts with no explanation attached. There are existing
tests ("parity" tests) that catch this. They must keep passing.

---

## Before you start

**Confirm Step 4 is done and healthy:**
```bash
cd ~/Projects/ai-agronomist
./venv/bin/python -m pytest tests/api/test_detector_fanout.py -q --timeout=600
```
Must pass. If it does not exist or fails, go finish Step 4.

Usual baseline:
```bash
git rev-parse --abbrev-ref HEAD          # write it down
git status                                # clean
./venv/bin/python -m pytest tests/ -q --timeout=900 --ignore=tests/load
```
```
My baseline:  ____ passed, ____ failed
```

**Record how many episodes the December file produces** — the incident count should
end up matching it:
```bash
PYTHONPATH=. ./venv/bin/python -c "
import sys; sys.path.insert(0,'tests/api')
from test_episode_grouping import _run_fixture_through_upload
from pathlib import Path
r,_=_run_fixture_through_upload(Path('tests/fixtures/native_dec_stuck_fault.csv'))
print('raw alerts:', len(r.high_anomalies), '  episodes:', len(r.episodes))
"
```
```
Before —  raw alerts: ____   episodes: ____
```
After this step, incidents written should equal the **episode** count, not the alert
count.

---

## Run it

```bash
cd ~/Projects/ai-agronomist

AGENT=backend-engineer
PROMPT=docs/runbooks/prompts/step5-episode-incidents.md

SYSTEM=$(awk '/^---/{c++;next} c==2' ".claude/agents/${AGENT}.md")
claude --model claude-sonnet-4-6 --print --dangerously-skip-permissions \
  --system-prompt "$SYSTEM" "$(cat $PROMPT)"
```

Allow 1–2 hours.
```bash
cat tasks/done/backend-engineer.json
git rev-parse --abbrev-ref HEAD
```

---

## Verify

**1. Incident count matches episode count.**
```bash
./venv/bin/python -m pytest tests/api/test_episode_incidents.py -v --timeout=600
```
There must be a passing test asserting incidents == episodes. Check the done-signal's
`incidents_before` / `incidents_after` and sanity-check them against your recorded
episode count.

**2. The dashboard join still works.** This is the check most likely to catch a real
bug:
```bash
./venv/bin/python -m pytest tests/api/test_anomaly_groups_parity.py tests/api/test_enriched_parity.py -q --timeout=300
```
All 33 must pass. If they fail, alerts have lost their link to incidents and the
dashboard would show unexplained alerts.

**3. The week-long fault matches its own alerts.**
```bash
./venv/bin/python -m pytest tests/api/test_episode_incidents.py -v --timeout=600 -k "long or span or 163"
```
Must pass. This is the 15-minute-window problem described above.

**4. The stuck-fault incident is useful to an operator** — it must name the channel,
the frozen duration, and be marked urgent:
```bash
./venv/bin/python -m pytest tests/api/test_episode_incidents.py -v --timeout=600 -k "stuck or urgent"
```

**5. Core capability unchanged.**
```bash
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py tests/api/test_episode_grouping.py tests/api/test_label_integrity.py -q --timeout=300
```

**6. No surprise migration.**
```bash
git status --porcelain alembic/
```
Should be empty. If not, the done-signal must explain why a column was genuinely
missing.

**7. Nothing else broke.**
```bash
./venv/bin/python -m pytest tests/ -q --timeout=900 --ignore=tests/load
```
Failed count still exactly **1**.

---

## Definition of done

- [ ] Incidents written == episodes, not sequences (test proves it)
- [ ] Before/after incident counts measured and reported
- [ ] Stuck-fault incident carries channel + frozen duration + `urgent` severity
- [ ] Writes batched into one transaction
- [ ] Both parity suites (33 tests) pass
- [ ] The 163.83 h episode matches its own alerts through the incident join
- [ ] Embedding choice recorded in `docs/architecture/episode_grouping.md`, with its
      limitations stated honestly
- [ ] No new migration (or a documented reason for one)
- [ ] Full suite: still exactly 1 failure
- [ ] Branch unchanged

---

## If it goes wrong

**"Parity tests fail"** — the alert↔incident join is broken; this is the predicted
failure mode. The ±15-minute matching window cannot handle a week-long episode.
Report it with the failing output; the fix is in the join, not in the tests.

**"Incidents still number in the thousands"** — the change did not take effect on the
upload path. Confirm which code path wrote them:
```bash
grep -rn "IncidentMemory" src/ --include=*.py | grep -v test
```

**"It added a migration"** — probably unnecessary. Review the done-signal's
`migration_added` reason before accepting it.

**"Stuck-fault tests fail"** — revert; core capability.
```bash
git stash
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py -q
```

---

## Rollback

```bash
git stash              # recoverable
git stash pop          # undo
```

---

## After this step

Phases 3–5 of `docs/architecture/upload_detector_convergence.md` remain — the
stateful detectors (CUSUM, EWMA, online learning) which the design says need
reformulating rather than porting, plus config-dependent detectors. Re-read that
document and re-measure before starting any of them; several of its assumptions have
already been corrected once.
