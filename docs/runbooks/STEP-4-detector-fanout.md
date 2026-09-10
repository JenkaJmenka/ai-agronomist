# Step 4 — Switch the extra detectors on for uploads

**Effort:** ~3–4 hours · **Risk:** high · **Depends on: Steps 2 and 3 (mandatory)**

---

## What you are doing and why

This is the payoff step. Everything before it was preparation.

The product has **18 detectors**. The path a grower actually uses — uploading a CSV —
runs **3 of them**. This step raises that number.

Why it was not done first: the extra detectors were measured and two of them were
badly broken (Step 3 fixed those), and they were comparing readings against a fake
notion of normal (Step 2 fixed that). Switching them on before those fixes would have
buried growers in false alarms.

### The speed limit you must respect

We measured the current upload path: a **full year** of 5-minute greenhouse readings
processes in **11 seconds** on one CPU core. That is fast, and it needs to stay fast
because the app runs on a budget hosting plan.

The budget for this step is **60 seconds** for a full year. Current 11 s leaves plenty
of room — with one exception.

One detector, the "ensemble", costs **1.05 milliseconds per window**, which works out
to **111 seconds** on its own. That is ten times the entire current pipeline and blows
the budget by itself. It either gets made faster or it gets left out. The prompt tells
the agent this.

> Historical note worth knowing: the design document originally assumed the machine
> learning model was the slow part and guessed the whole job took 17 minutes. When
> actually measured it was 11 seconds — the guess was wrong by **230x**. That is why
> this step insists on measuring rather than estimating.

---

## Before you start

**Confirm Steps 2 and 3 are done.**

```bash
cd ~/Projects/ai-agronomist

# Step 2 check — should print lines outside model_registry.py
grep -rn "model_baseline" src/ --include=*.py | grep -v "src/db/models.py"

# Step 3 check — these must all pass
./venv/bin/python -m pytest tests/models/test_detector_calibration.py -q --timeout=300
```

If either check fails, **stop**. Go back and finish that step.

**Read Step 3's report** — it may have recommended a detector stay out:
```bash
cat tasks/done/ml-engineer.json
```
Look for `recommend_out`. If a detector is listed there, it must NOT be added here.

Usual baseline:
```bash
git rev-parse --abbrev-ref HEAD          # write it down
git status                                # clean
./venv/bin/python -m pytest tests/ -q --timeout=600 --ignore=tests/load
```
```
My baseline:  ____ passed, ____ failed
```

**Record the "before" numbers you will need to judge this step.** Alert count on the
healthy fixture, and full-year runtime:

```bash
cd ~/Projects/ai-agronomist

# how many alerts does a HEALTHY week currently produce?  (lower is better)
PYTHONPATH=. ./venv/bin/python -c "
import sys; sys.path.insert(0,'tests/api')
from test_episode_grouping import _run_fixture_through_upload
from pathlib import Path
r,_=_run_fixture_through_upload(Path('tests/fixtures/native_july_clearsky.csv'))
print('July healthy fixture — raw alerts:', len(r.high_anomalies), ' episodes:', len(r.episodes))
"
```
```
Before —  July alerts: ____   episodes: ____
```

Measured on 2026-09-10 this prints **`raw alerts: 0  episodes: 0`**. A healthy week
is completely silent today. That silence is the thing this step must not destroy.

Also record the December (faulty) fixture, which *should* be noisy:
```bash
PYTHONPATH=. ./venv/bin/python -c "
import sys; sys.path.insert(0,'tests/api')
from test_episode_grouping import _run_fixture_through_upload
from pathlib import Path
r,_=_run_fixture_through_upload(Path('tests/fixtures/native_dec_stuck_fault.csv'))
print('Dec faulty fixture — raw alerts:', len(r.high_anomalies), ' episodes:', len(r.episodes))
"
```
Measured baseline: **115 raw alerts, 16 episodes**.
```
Before —  Dec alerts: ____   episodes: ____
```

---

## Run it

```bash
cd ~/Projects/ai-agronomist

AGENT=ml-engineer
PROMPT=docs/runbooks/prompts/step4-detector-fanout.md

SYSTEM=$(awk '/^---/{c++;next} c==2' ".claude/agents/${AGENT}.md")
claude --model claude-opus-4-6 --print --dangerously-skip-permissions \
  --system-prompt "$SYSTEM" "$(cat $PROMPT)"
```

Allow 2–4 hours; this is the largest step. If it stops on a session limit, check
`git status` — partial work is on disk and may be complete (README §4).

```bash
cat tasks/done/ml-engineer.json
git rev-parse --abbrev-ref HEAD
```

---

## Verify

**1. The healthy week did not get noisy.** This is the single most important check —
re-run the "before" command above.

```
After —  July alerts: ____   episodes: ____
```

**Must be 5 or fewer.** It is `0` today, and a percentage tolerance against zero
means nothing, so the gate is an absolute cap. If alerts on a healthy week jumped
into the tens or hundreds, the step has made the product worse regardless of what
else it achieved — go to "If it goes wrong" below and find the culprit detector.

The December count may legitimately rise as more detectors are added. What must not
change is that the frozen-sensor fault is still caught (check 3 below).

**2. Speed is inside budget.** Measure it yourself:
```bash
cd ~/Projects/ai-agronomist
PYTHONPATH=. ./venv/bin/python -c "
import time, pandas as pd, torch
torch.set_num_threads(1)
from src.vendors.csv_adapter import CSVAdapter, pandas_csv_reader_kwargs, parse_raw_rows
from src.api.services.cold_run_upload import _strip_raw_records
from src.api.services.upload_portions import score_portion, StatCarry
from src.models.stuck_fault_detector import StuckCarry
from src.models.data_utils import SENSOR_RANGES
from src.models.inference import AnomalyDetector
SRC='/Users/eugenesidorov/Downloads/2026_utk_test.csv'
ad=CSVAdapter({'filepath':SRC}); ad.connect()
det=AnomalyDetector(checkpoint_path='tests/fixtures/epoch_0072_trained_baseline.pt')
stat=StatCarry(); stuck=StuckCarry(); prev=pd.DataFrame(); rows=0
t0=time.perf_counter()
for ch in pd.read_csv(SRC, chunksize=5000, **pandas_csv_reader_kwargs(ad._csv_props)):
    if ch.empty: continue
    try: nd=ad.normalize(ch); ad.validate(nd)
    except Exception: nd=None
    rdf,_=parse_raw_rows(ch)
    rr=_strip_raw_records(rdf) if not rdf.empty else []
    p,prev=score_portion(normalized_df=nd, prev_overlap_df=prev, raw_rows=rr,
        stat_carry=stat, adaptive_ranges=SENSOR_RANGES, detector=det,
        seq_len=20, stuck_carry=stuck)
    rows+=len(ch)
el=time.perf_counter()-t0
print(f'{rows} rows in {el:.1f}s   budget 60s   {\"PASS\" if el<60 else \"OVER BUDGET\"}')
"
```
> If the source CSV is not at that path, use any full-year export. The number that
> matters is seconds for ~105,000 rows.

Must print **PASS**.

**3. The flagship fault still works, unchanged.**
```bash
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py tests/api/test_episode_grouping.py -q --timeout=300
```
All pass. The stuck fault must still be found at 6.00 h with a 163.83 h duration.

**4. Alerts name the right channel.** The original bug this whole project fixed was
alerts blaming the wrong sensor. Confirm it has not come back:
```bash
./venv/bin/python -m pytest tests/api/test_label_integrity.py -q --timeout=300
```
All pass.

**5. Filled-in columns produce no alerts.** These files have no CO2 sensor; the column
is a constant `400.0`:
```bash
./venv/bin/python -m pytest tests/api/test_detector_fanout.py -v --timeout=600 -k "imputed or co2 or skip"
```
Must pass.

**6. Nothing else broke.**
```bash
./venv/bin/python -m pytest tests/ -q --timeout=900 --ignore=tests/load
```
Failed count still exactly **1**.

---

## Definition of done

- [ ] July healthy-fixture alert count is 5 or fewer (**your measurement**; it is 0 today)
- [ ] Full-year runtime under 60 s at 1 thread (**your measurement**)
- [ ] Any detector Step 3 recommended excluding was NOT added
- [ ] Imputed/constant channels produce zero alerts
- [ ] Stuck fault: still 6.00 h latency, 163.83 h duration
- [ ] `test_label_integrity.py` passes — alerts still name the real triggering channel
- [ ] New alerts group into episodes, not one row per sample
- [ ] The done-signal reports detectors added, detectors excluded and why, before/after
      July counts, and measured runtime
- [ ] Full suite: still exactly 1 failure
- [ ] Branch unchanged

---

## If it goes wrong

**"July alert count exploded"** — the most likely outcome if something is wrong, and a
hard stop. Find which detector is responsible before doing anything else:
```bash
PYTHONPATH=. ./venv/bin/python -c "
import sys, collections; sys.path.insert(0,'tests/api')
from test_episode_grouping import _run_fixture_through_upload
from pathlib import Path
r,_=_run_fixture_through_upload(Path('tests/fixtures/native_july_clearsky.csv'))
print(collections.Counter(getattr(a,'detector','?') for a in r.high_anomalies))
"
```
That prints alerts per detector. The offender will be obvious. Report it and have that
detector excluded or recalibrated — do not raise thresholds blindly.

**"Over budget on time"** — almost certainly the ensemble detector, which we measured at
111 s on its own. It should have been vectorised or left out. Check whether it was
added:
```bash
grep -n "Ensemble" src/api/services/upload_portions.py
```

**"Stuck-fault tests fail"** — revert. This is the product's flagship capability and no
new detector is worth losing it.
```bash
git stash
./venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py -q
```

**"Alerts blame the wrong sensor again"** — `test_label_integrity.py` failing means
value-based inference has crept back in. That was the original defect: alerts said
"greenhouse temperature" when the real trigger was a heating pipe, in 52% of cases.
Reject and re-run pointing at requirement 4 of the prompt.

---

## Rollback

```bash
git stash              # recoverable
git stash pop          # undo
```

This is the highest-risk step. If in doubt, stash and get a second opinion rather than
pressing on.
