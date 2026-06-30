# Anomaly Detection Improvement: Cold-Start Accuracy

**Status:** Needs diagnostic work before implementation — this plan is structured as diagnose-then-fix, not a fixed solution, because the root cause isn't confirmed yet.
**Depends on:** Plan 01 (caching) should land first so iteration on this doesn't mean re-running slow inference every time.
**Trigger finding:** Visually obvious drops, spikes, and drift patterns are not being flagged as anomalies by the current model.

---

## Why this finding matters more than a simple threshold problem

"I can see it on the graph but the model doesn't flag it" is a qualitatively different problem from "the model flags too much" or "the model flags too little." It suggests the model may not have learned what these fault signatures look like in the first place, rather than having learned them but set the alarm threshold too high. These need to be distinguished before picking a fix, because the fixes are different:

- If the model has learned the patterns but the threshold is miscalibrated → **threshold/calibration fix**, fast.
- If the model has never seen anything resembling these fault types in training (synthetic data didn't include them, or included them unrealistically) → **training data fix**, slower.
- If the model architecture can't represent the fault duration/shape (e.g., `seq_len=20` is too short to span a multi-day stuck-fault drift) → **architecture/windowing fix**, slower still.
- If the detection method itself (reconstruction error on an LSTM-VAE) is poorly suited to this fault type (e.g., a slow drift might have very low reconstruction error if the model just learns to "drift along" with it) → **method fix**, requires care.

## What we already use (current state, per project knowledge)

- **LSTM-VAE**: `input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20, beta=0.1, dropout=0.2` — flags anomalies via reconstruction error.
- **Isolation Forest**: secondary/ensemble detector, trained on synthetic data, encodes physical-ratio rules (e.g., CO₂ supply failure = low CO₂ + normal sensors).
- **Training data**: synthetic, generated via TimeGAN + physics-based simulation + "failure scenario injection." Cold-start design deliberately avoids requiring real customer data.
- **Known real-world fault pattern already identified in the data**: the stuck-fault pattern in `Measured Relative Humidity` / `Measured Humidity Deficit` (Nov 21–Dec 26, 2026 — multi-day runs at exactly 100% RH / 0.0 g/m³, followed by slow drift). This is a documented real example and should be the first test case for whatever diagnostic is run, since it's a known-positive case with a known signature.

## Diagnostic plan (run before deciding on a fix)

1. **Run the existing model against the known stuck-fault window** (Nov 21–Dec 26) and record the actual reconstruction error / anomaly score over that period, not just whether it crossed the alert threshold. If the score rises even slightly during the fault but stays under threshold → calibration problem. If the score stays flat/normal throughout → the model isn't recognizing the pattern at all, which points to training data or architecture.
2. **Inspect what's actually in the synthetic training data's "failure scenario injection."** Does it include stuck-fault patterns (sensor pinned at a constant value for an extended period) and slow drift patterns? If "failure scenario injection" was designed primarily around sudden spikes (the easier case to simulate), stuck-faults and slow drift may simply be underrepresented or absent.
3. **Check `seq_len=20` against the fault durations actually being missed.** If 20 timesteps is, say, 20 minutes or 20 hours depending on the sampling interval, and the stuck-fault runs for multiple days, the model's input window may never see the "before vs. during" transition in a single pass — it may only ever see steady-state-during-fault, which looks locally "normal" to a model with no longer-range memory.
4. **Separately test spike patterns vs. drift patterns vs. stuck patterns** — these are three different shapes and may have three different failure modes in the current setup. Don't treat "anomalies aren't detected" as one bug; it's likely at least two.
5. **Sanity-check the Isolation Forest's physical-ratio rules** against the same known fault — does it fire at all, even if the LSTM-VAE misses it? If the ensemble's rule-based half is also silent, that's further evidence the issue is in input data realism, not just the neural model's calibration.

## Candidate remediation paths (to be narrowed down after diagnosis)

- **Recalibrate threshold** using percentile-based or statistical (e.g., 3-sigma over a validation window) thresholds instead of whatever fixed value is currently used, if diagnosis shows the signal is present but underweighted.
- **Augment synthetic data generation** to explicitly include stuck-fault and slow-drift scenarios with realistic durations, not just spike-type failures, if diagnosis shows these patterns are absent or unrealistic in training data.
- **Increase `seq_len` or add a secondary longer-window detector** (e.g., a rolling-statistics check for "value hasn't changed in N hours" run alongside the LSTM-VAE, rather than relying on one model to catch every fault shape) if diagnosis shows the window is too short for slow faults.
- **Add explicit rule-based stuck-fault and drift detectors** to the Isolation Forest ensemble (e.g., "value unchanged for >X hours" or "value drifting monotonically for >Y hours outside expected range") as a complement to the learned model, since these particular fault types have simple, describable signatures that don't necessarily need a neural model to catch — this may be the fastest path to closing the gap for these two fault types specifically while the longer-term synthetic-data fix is in progress.

## Deliverables

- [ ] Diagnostic report: reconstruction error / anomaly score plotted against the known stuck-fault window, with a clear conclusion on which failure mode (calibration / training data / architecture / method) is most likely responsible
- [ ] Same diagnostic repeated for at least one spike-type and one pure-drift-type case if available in the data, to confirm whether this is one bug or several
- [ ] A decision, backed by the diagnostic, on which remediation path(s) to pursue — do not implement all candidate fixes speculatively
- [ ] Implementation of the chosen fix(es)
- [ ] Before/after comparison on the known stuck-fault window and any other test cases used
- [ ] Updated documentation noting what fault types the current system is now validated against, and which remain unvalidated

## Success criteria

- The known stuck-fault pattern (Nov 21–Dec 26) is correctly flagged as anomalous by the system (LSTM-VAE and/or Isolation Forest)
- At least one spike-type and one drift-type pattern, if identifiable in the data, are also correctly flagged
- The diagnostic conclusion is documented so future "obvious anomaly not detected" reports can be triaged faster

---

## Orchestrator prompt for Claude Code

```
You are diagnosing and improving cold-start anomaly detection accuracy for the
AI Agronomist platform at ~/Projects/ai-agronomist/. Read CLAUDE.md first for
project rules and agent delegation patterns.

CONTEXT: The current system (LSTM-VAE: input_dim=4, hidden_dims=[64,32],
latent_dim=8, seq_len=20, beta=0.1, dropout=0.2, plus an Isolation Forest
ensemble with physical-ratio rules) is trained on synthetic data and fails to
flag anomalies that are visually obvious on a graph — specifically spikes,
drops, and drift patterns. A known real-world test case exists: a stuck-fault
pattern in Measured Relative Humidity / Measured Humidity Deficit, running
Nov 21 - Dec 26, 2026, with multi-day runs pinned at exactly 100% RH / 0.0
g/m³ followed by slow unexplained drift. Use this as your primary test case.

DO NOT jump to a fix. This task is diagnose-first. Different root causes
require different fixes, and implementing the wrong one wastes time and may
mask the real problem.

STEP 1 — REPRODUCE AND MEASURE:
  - Locate the trained LSTM-VAE model and the dataset containing the known
    stuck-fault window.
  - Run inference and extract the raw reconstruction error / anomaly score
    time series across that window — not just the binary alert/no-alert
    output. Plot or tabulate score-over-time across the fault period and the
    surrounding normal period.
  - Determine: does the score rise during the fault (even slightly) and stay
    under threshold? Or does it stay flat/normal throughout? This distinction
    is the most important output of this step — report it explicitly.
  - Repeat the same measurement for the Isolation Forest's output on the same
    window.

STEP 2 — INSPECT TRAINING DATA:
  - Locate the synthetic data generation code (TimeGAN + physics-based
    simulation + failure scenario injection, per project docs).
  - Determine what failure scenario types are actually being injected.
    Specifically check: are stuck-fault patterns (a sensor value pinned
    constant for an extended duration) and slow-drift patterns represented?
    Or does injection focus mainly on sudden spikes?
  - Report concretely what's present and absent, with code references.

STEP 3 — CHECK WINDOWING:
  - Determine the actual time interval represented by seq_len=20 in the
    dataset being used (i.e., what real-world duration does a 20-step window
    span, given the data's sampling frequency).
  - Compare that to the duration of the known stuck-fault (multi-day). If the
    window is much shorter than the fault duration, explain whether/how this
    would prevent the model from ever seeing a normal-to-fault transition in
    a single input window.

STEP 4 — SYNTHESIZE A DIAGNOSIS:
  - Based on Steps 1-3, state which of these is most likely responsible:
    (a) threshold miscalibration with signal present but underweighted,
    (b) training data missing or unrealistic for this fault type,
    (c) windowing/architecture unable to represent this fault's duration,
    (d) the reconstruction-error method is fundamentally a poor fit for slow
    drift specifically (since a model could learn to "track" a slow drift
    with low error).
  - You may conclude more than one of these applies to different fault types
    (e.g., spikes might be a calibration issue while stuck-faults are a
    training-data issue) — if so, say so explicitly rather than picking one
    answer for everything.
  - STOP HERE and report the diagnosis before implementing any fix. Wait for
    confirmation on which remediation path(s) to pursue.

STEP 5 — IMPLEMENT (only after diagnosis is confirmed):
  Likely remediation paths, to be selected based on Step 4's findings, not
  all implemented speculatively:
    - Recalibrate the anomaly threshold using a statistical method (e.g.,
      percentile-based or rolling z-score) instead of a fixed value, if
      signal-present-but-underweighted was the diagnosis.
    - Extend the synthetic data generator to include realistic stuck-fault
      and slow-drift scenarios with appropriate durations, if training data
      was the diagnosis. This may require new failure-injection logic, not
      just more of the existing kind.
    - Add a complementary rule-based detector for stuck-faults (e.g.,
      flag if a sensor value hasn't changed by more than epsilon over N
      hours) and/or slow drift (e.g., flag sustained monotonic movement
      outside an expected band over N hours) to run alongside the LSTM-VAE,
      if the diagnosis suggests these fault types have simple describable
      signatures better caught by explicit rules than by the learned model.
    - Only adjust seq_len or model architecture if Step 3's diagnosis
      specifically implicates windowing — this is the most expensive fix
      (requires retraining) so confirm before doing it.

STEP 6 — VALIDATE:
  - Re-run against the known stuck-fault window and confirm it is now
    correctly flagged.
  - If other known fault examples exist in the data (check with the user if
    none are obvious from project docs), validate against those too.
  - Report before/after scores, not just pass/fail.

STEP 7 — DOCUMENT:
  - Update or create a doc noting which fault types are now validated as
    correctly detected, and which remain unverified, so future "missed
    anomaly" reports can be triaged against known coverage.

Constraints:
  - Do not change the caching logic from the separate caching task.
  - Do not implement all four candidate remediation paths speculatively —
    pick based on Step 4's diagnosis and confirm before building.
  - If retraining the LSTM-VAE is required, use the existing training script
    /pipeline rather than writing a new one from scratch.
```
