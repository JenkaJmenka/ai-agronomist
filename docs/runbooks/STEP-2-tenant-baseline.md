# Step 2 — Give each greenhouse its own idea of "normal"

**Effort:** ~1–2 hours · **Risk:** medium · **Depends on:** nothing
**Blocks:** Step 3 (do not start Step 3 before this is done)

---

## What you are doing and why

Some detectors work by comparison. They hold a statistical picture of what normal
readings look like for this greenhouse, then flag readings that don't fit.

**Right now that picture is fake.** It is 10 randomly generated fake readings,
identical for every customer on the platform. A tomato grower in Portugal and a
cucumber grower in the Netherlands are both compared against the same 10 invented
numbers.

You can see the damage in real measurements. One detector (Mahalanobis) fires on:

- **38.3%** of windows in a week of perfectly healthy July weather
- **9.5%** of windows in a week containing a genuinely broken sensor

It finds healthy data *more* suspicious than broken data. That is what happens
when you compare reality against fiction.

**The fix is mostly plumbing, because the parts already exist:**

- There is already a database table for this (`model_baseline`) that nothing uses.
- There is already a process that extracts clean, sanity-checked readings for each
  customer (the "cold-start bootstrap").

So: feed the second into the first, and have the detectors read from it.

The reasoning is in `docs/architecture/ADR-008-tenant-baseline-provisioning.md`.

### Why this must come before Step 3

Step 3 retunes two broken detectors. Retuning means adjusting numbers until the
detector is quiet on healthy data and loud on broken data. If you retune while the
"normal" picture is still fake, you tune against fiction — and when this step
lands, all of Step 3's work is invalid and must be redone.

**Order matters. Do not swap these.**

---

## Before you start

```bash
cd ~/Projects/ai-agronomist
git rev-parse --abbrev-ref HEAD          # write the branch name down
git status                                # should be clean
```

Baseline:
```bash
./venv/bin/python -m pytest tests/api/ tests/vendors/ tests/db/ tests/services/ tests/fixtures/ -q --timeout=600
```
Expected `1 failed, 719 passed, 8 skipped, 1 xfailed`.

```
My baseline:  ____ passed, ____ failed
```

**Record the "before" behaviour of the broken detector.** You will want this to
prove Step 3 actually helped later:

```bash
cd ~/Projects/ai-agronomist
PYTHONPATH=. ./venv/bin/python -c "
import pandas as pd
from src.vendors.csv_adapter import CSVAdapter, pandas_csv_reader_kwargs
from src.models.multivariate_mahalanobis_detector import MultivariateMahalanobisDetector
from src.config import SENSOR_NAMES, SEQ_LEN
for label, p in [('healthy July','tests/fixtures/native_july_clearsky.csv'),
                 ('Dec with fault','tests/fixtures/native_dec_stuck_fault.csv')]:
    ad=CSVAdapter({'filepath':p}); ad.connect()
    df=ad.normalize(pd.read_csv(p, **pandas_csv_reader_kwargs(ad._csv_props)))
    arr=df[SENSOR_NAMES].to_numpy(float); m=MultivariateMahalanobisDetector()
    n=len(arr)-SEQ_LEN+1
    hits=sum(1 for i in range(n) if any(m.detect(arr[i:i+SEQ_LEN])))
    print(f'  {label:16s}: {hits}/{n} windows = {100*hits/n:.1f}%')
"
```
Expected roughly `healthy July: 38.3%` and `Dec with fault: 9.5%`. Write them down —
those are the numbers Step 3 has to improve.

---

## Run it

```bash
cd ~/Projects/ai-agronomist

AGENT=ml-engineer
PROMPT=docs/runbooks/prompts/step2-tenant-baseline.md

SYSTEM=$(awk '/^---/{c++;next} c==2' ".claude/agents/${AGENT}.md")
claude --model claude-opus-4-6 --print --dangerously-skip-permissions \
  --system-prompt "$SYSTEM" "$(cat $PROMPT)"
```

This one takes a while — 30–60 minutes is normal. If it stops early with a session
limit message, see README §4 ("If the agent stops early"): the work is on disk and
often complete.

When it finishes:
```bash
cat tasks/done/ml-engineer.json
git rev-parse --abbrev-ref HEAD          # same branch?
git status                                # what changed?
```

---

## Verify

**1. It did not add a database migration.** The table already exists; a new
migration means it misunderstood the task.
```bash
git status --porcelain alembic/
```
Should print nothing.

**2. Its tests pass.**
```bash
./venv/bin/python -m pytest tests/models/test_tenant_baseline.py -v --timeout=300
```
Look for tests covering: round-trip, stored-baseline-wins, fallback-to-default,
no cross-tenant leakage, newest-wins, non-finite rejection.

**3. The no-regression case is real.** This is the important one — customers with no
stored baseline must behave exactly as before:
```bash
./venv/bin/python -m pytest tests/models/test_tenant_baseline.py -v --timeout=300 -k "default or fallback"
```
Must pass.

**4. Multi-tenant isolation.** This project serves multiple customers from one
database, and a leak would mean scoring one grower against another's data:
```bash
./venv/bin/python -m pytest tests/models/test_tenant_baseline.py -v --timeout=300 -k "tenant or leak or isolat"
```
Must pass.

**5. The flagship fault still gets caught.** This is the product's headline
capability and must be untouched by this step:
```bash
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py tests/api/test_episode_grouping.py -q --timeout=300
```
All must pass. If anything here fails, this step has broken the core feature —
stop and revert.

**6. Nothing else broke.**
```bash
./venv/bin/python -m pytest tests/ -q --timeout=600 --ignore=tests/load
```
Failed count must still be exactly **1** (the SQLite NaN one).

---

## Definition of done

- [ ] No new file under `alembic/`
- [ ] A tenant's baseline can be derived, stored, and read back
- [ ] A tenant with no stored baseline behaves exactly as before
- [ ] Two tenants never see each other's baseline
- [ ] Newest baseline wins when several exist; old rows are not deleted
- [ ] Which baseline was used is logged at startup/scoring
- [ ] `tests/api/test_stuck_fault_upload.py` and `test_episode_grouping.py` fully pass
- [ ] Full suite: still exactly 1 failure, the SQLite NaN one
- [ ] Branch unchanged
- [ ] **No detector threshold was changed** (`git diff src/config.py` — should show only
      the new baseline-size constant, nothing about noise, spike, or Mahalanobis)

---

## If it goes wrong

**"It changed detector thresholds too"** — that is Step 3's job and doing it now
invalidates it. Check and revert:
```bash
git diff src/config.py
```
Anything touching `SENSOR_NOISE_TARGETS`, `NOISE_*`, `SPIKE_*`, or Mahalanobis
settings should be reverted.

**"It fits the baseline from the uploaded file"** — this is the trap ADR-008
explicitly rejects. The December fixture is a sensor frozen for 163 hours; fitting
"normal" from it teaches the system that a dead sensor is normal. Check for the
scored file being used as its own baseline source and reject that approach.

**"The stuck-fault tests now fail"** — revert immediately. That is the product's
flagship feature.
```bash
git stash          # park the changes so nothing is lost
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py -q
```
If they pass after stashing, the change is the cause. Report with the failure output.

**"It added a second normalisation function"** — readings must be normalised the
same way everywhere or the baseline is measured on a different scale from the data.
It should reuse `src/models/data_utils.py`. Flag it.

---

## Rollback

```bash
git status                # review
git stash                 # park changes recoverably (preferred)
git stash pop             # bring them back
```
Prefer `git stash` over `git checkout -- .` — stash is recoverable, checkout is not.
