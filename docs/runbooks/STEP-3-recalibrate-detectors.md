# Step 3 — Fix two detectors that cry wolf

**Effort:** ~2–3 hours · **Risk:** medium · **Depends on: Step 2 (mandatory)**
**Blocks:** Step 4

---

## What you are doing and why

Two of the detectors we want to switch on are currently worse than useless. We
measured them against two weeks of real greenhouse data:

| Detector | Healthy week | Week with a genuinely broken sensor |
|---|---|---|
| **NoiseDetector** | fires on **69.6%** of the time | 3.1% |
| **Mahalanobis** | fires on **38.3%** | 9.5% |

Read those numbers again. `NoiseDetector` would alert on **7 out of every 10**
measurements from a perfectly healthy greenhouse. And `Mahalanobis` alerts **four
times more often on healthy data than on a dead sensor** — it is not merely noisy,
it is pointing the wrong way.

If we switched these on today, a grower would get thousands of meaningless alerts,
stop reading them, and miss the one that mattered. That is the failure mode this
whole project exists to avoid.

Your job: make them quiet on healthy data while still catching the real fault.

### Why Step 2 must be done first

Both detectors judge readings by comparing them against a statistical picture of
"normal". Until Step 2, that picture was 10 invented numbers. Tuning a detector
against a fake normal produces fake tuning.

There is a real chance Mahalanobis **fixes itself** once Step 2 lands — being fitted
to fiction is the most likely explanation for it being inverted. So:

**The first thing this step does is re-measure. Do not skip that.** You may find
half the work is already done.

### The likely culprit worth knowing about

These greenhouse files have no CO2 sensor. The system fills the missing column with
a constant `400.0` for every single row. A detector that looks at how sensors move
*together* (which is what Mahalanobis does) breaks when one column never moves —
mathematically it makes the calculation unstable. There is already a mechanism for
telling detectors to ignore filled-in columns; this step should reuse it.

---

## Before you start

**Confirm Step 2 is actually done.** Do not take anyone's word for it:

```bash
cd ~/Projects/ai-agronomist
grep -rn "model_baseline" src/ --include=*.py | grep -v "src/db/models.py"
```

If that prints only a line from `model_registry.py`, **Step 2 is not done. Stop
here** and go do Step 2.

Then the usual:
```bash
git rev-parse --abbrev-ref HEAD          # write it down
git status                                # should be clean
./venv/bin/python -m pytest tests/ -q --timeout=600 --ignore=tests/load
```
```
My baseline:  ____ passed, ____ failed
```

**Re-measure the two detectors now, with Step 2 in place.** These are the numbers
the agent has to beat:

```bash
cd ~/Projects/ai-agronomist
PYTHONPATH=. ./venv/bin/python -c "
import pandas as pd
from src.vendors.csv_adapter import CSVAdapter, pandas_csv_reader_kwargs
from src.models.noise_detector import NoiseDetector
from src.models.multivariate_mahalanobis_detector import MultivariateMahalanobisDetector
from src.config import SENSOR_NAMES, SEQ_LEN
for label,p in [('healthy July','tests/fixtures/native_july_clearsky.csv'),
                ('Dec w/ fault','tests/fixtures/native_dec_stuck_fault.csv')]:
    ad=CSVAdapter({'filepath':p}); ad.connect()
    df=ad.normalize(pd.read_csv(p, **pandas_csv_reader_kwargs(ad._csv_props)))
    arr=df[SENSOR_NAMES].to_numpy(float); n=len(arr)-SEQ_LEN+1
    nd=NoiseDetector(); mh=MultivariateMahalanobisDetector()
    noise=mahal=0
    for i in range(n):
        w=arr[i:i+SEQ_LEN]
        for si,s in enumerate(SENSOR_NAMES):
            try:
                if nd.detect(w[:,si], s): noise+=1; break
            except Exception: pass
        try:
            if any(mh.detect(w)): mahal+=1
        except Exception: pass
    print(f'  {label:14s} noise {100*noise/n:5.1f}%   mahalanobis {100*mahal/n:5.1f}%   (n={n})')
"
```

```
After Step 2 —  healthy July:  noise ____%   mahalanobis ____%
                Dec w/ fault:  noise ____%   mahalanobis ____%
```

**Target: both under 5% on healthy July, both still firing inside the December
fault.** If Mahalanobis is already under 5%, note that — Step 2 fixed it.

---

## Run it

```bash
cd ~/Projects/ai-agronomist

AGENT=ml-engineer
PROMPT=docs/runbooks/prompts/step3-recalibrate-detectors.md

SYSTEM=$(awk '/^---/{c++;next} c==2' ".claude/agents/${AGENT}.md")
claude --model claude-opus-4-6 --print --dangerously-skip-permissions \
  --system-prompt "$SYSTEM" "$(cat $PROMPT)"
```

Allow 1–2 hours. When it finishes:
```bash
cat tasks/done/ml-engineer.json          # contains the before/after rates it measured
git rev-parse --abbrev-ref HEAD
```

---

## Verify

**1. Re-run your own measurement** (the block from "Before you start"). Do not trust
the agent's reported numbers — in this project three agent-reported measurements have
turned out to be artifacts. Get them yourself.

Both must be **under 5%** on healthy July.

**2. The detectors are not simply switched off.** This is the failure mode to watch
for: the easiest way to get 0% false positives is to disable a detector.
```bash
./venv/bin/python -m pytest tests/models/test_detector_calibration.py -v --timeout=300
```
There must be passing tests proving each detector **still fires inside the December
fault window**. If those tests are missing, the calibration is not verified — reject
it.

**3. Changed numbers are explained.**
```bash
git diff src/config.py
```
Every changed constant should have a comment saying what it was, what it is now, and
the measurement that justified it. A bare number change with no rationale is not
acceptable in this repo.

**4. The flagship fault still works.**
```bash
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py tests/api/test_episode_grouping.py -q --timeout=300
```
All must pass.

**5. Nothing else broke.**
```bash
./venv/bin/python -m pytest tests/ -q --timeout=600 --ignore=tests/load
```
Failed count still exactly **1**.

---

## Definition of done

- [ ] NoiseDetector under 5% on healthy July (**verified by you**, not just reported)
- [ ] Mahalanobis under 5% on healthy July (verified by you)
- [ ] Both still fire inside the December fault window, with tests proving it
- [ ] Constant/filled-in channels excluded from multivariate detection
- [ ] Every changed constant lives in `src/config.py` with a rationale comment
- [ ] SpikeMAD's silence is **diagnosed and reported** — not tuned into firing
- [ ] `test_stuck_fault_upload.py` and `test_episode_grouping.py` fully pass
- [ ] Full suite: still exactly 1 failure
- [ ] Branch unchanged

---

## If it goes wrong

**"Both detectors now fire 0% everywhere"** — they have been disabled, not fixed.
A detector silent on a week containing a genuinely dead sensor is broken. Reject and
re-run, emphasising the fault-window sensitivity requirement.

**"It says it can't get one under 5% without going blind to the fault"** — that is a
legitimate answer. The right outcome is to recommend that detector stays **out** of
Step 4's fan-out, with the reasoning recorded. A missing detector is much better than
one that trains growers to ignore alerts. Accept it and note it for Step 4.

**"It tuned SpikeMAD into firing"** — the prompt forbids this. SpikeMAD is silent on a
fixture containing a real fault and nobody knows why yet. Making it loud without
understanding why it was quiet just moves the problem. Revert that part.

**"The stuck-fault tests fail"** — revert immediately, this is the core product
feature:
```bash
git stash
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py -q
```

---

## Rollback

```bash
git stash              # recoverable
git stash pop          # undo the rollback
```
