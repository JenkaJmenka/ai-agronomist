# Anomaly detection refactor — handoff

**Branch:** `feat/anomaly-detection-module-refactoring`  
**Base:** `main` at `4556539` (PR #107)  
**Date:** 2026-11-13  
**Bar:** composite ≥ 0.90, defined as `0.5 × clean specificity + 0.5 × mean detection recall` on spike, noise, and two-channel level shift.  
**Last measurement:** composite **0.9041916167664671** (clean specificity 80.84%, clean FPR 19.16% = 32/167, mean recall 1.0). Upload +6 °C spikes **12/12**, clean upload column hits **0**. Gate file: `tests/models/test_detection_accuracy.py`. Full suite on 2026-11-14 in a Linux/Python 3.11 cloud environment (not the project venv): 3996 passed, 4 failed at `3493a7a` with the gate on; the four failures were LSTM determinism/parity tests with zero alerts to compare. On `main` before this work it was 3997 passed / 35 skipped / 3 xfailed / 0 failed.

Canvases copied from the session artifacts:

- `docs/handoff/anomaly-audit-main.canvas.tsx` — audit of `main`
- `docs/handoff/anomaly-detection-improvements.canvas.tsx` — the improvement plan

The local progress log `tasks/anomaly-detection-refactor-progress.md` is gitignored. This file is the copy that survives a clone.

## What landed

Phase 1, accepted at 90% after the bar was lowered from 95%. The first gate was 59.58% because the real pipeline still voted bands, bias, stuck, and light. Recall on the three injected faults was already 100%.

- Enrichment votes the last timestep. Precedence is dropout/OOB, then BOCPD anywhere in the window, last-step MAD (temperature, humidity, CO₂; light skipped), noise, bias, correlation, diurnal, Mahalanobis, flatline, setpoint. Bands set `is_anomaly` only when the tenant has raw overrides.
- Noise calibration can only loosen: `resolve_noise_baseline` keeps `max(calibrated, fixed)`. Ratio 20 is used only when clean windows are passed.
- Live scorer keeps 32 non-overlapping clean windows and drops a window the fixed noise detector already flags. Empty ring passes `None`.
- Upload MAD scores only `UPLOAD_SPIKE_MAD_FLOOR` (temperature 0.5 °C, c=8). The robust LSTM gate was reverted to off by default on 2026-11-14: with it on, the LSTM raised zero alerts on both the July and December fixtures, breaking four determinism/parity tests. CSV bounds match `OOB_LIMITS`.
- `out_of_bounds` is emitted. Migration `029` and `FaultType.HUMIDITY_FAULT` allow `humidity_fault`. `thermodynamic_contradiction` stays a separate member. Physics-confirmed `psychrometric_impossibility` still maps to `spike_fault`.
- Flatline uses variance ≤ `FLATLINE_VARIANCE_EPS`² (`1e-4`) and drops light. Ensemble midpoint bias pre-check is gone. Stuck pre-check skips light.
- Saved CUSUM and EMA load before the update. The residual is the denormalized last timestep via `for_physical_series()`, and `update_and_reset` records the sensor from the True return. Step 5a is still the only CUSUM vote.
- Mahalanobis `score_last_against_history` is used when the tenant has at least `2 * n_sensors` finite `SensorReading` rows in the diurnal trailing window. A short or failed history keeps the in-window `detect()` vote. The accuracy harness has an empty database, so its 0.904 figure is still the in-window path.
- Catalog refreshed 2026-11-12, including `LIGHT_RANGE` (0, 1200) W/m². Unfixed items were left undescribed.

## Impact

| Slice | Before | After |
|---|---|---|
| Enrichment clean FPR on the held-out July half | 99.7% on the audit path; 80.84% at the first real-pipeline gate | 19.16% (31 `accuracy_decline`, 1 `calibration_drift`) |
| Composite | 59.58% on the first gate | 0.904 |
| Injected spike / noise / level-shift recall and labels | level-shift label was 0/40 | 40/40 each, labels `spike_fault`, `accuracy_decline`, `calibration_drift` |
| Upload +6 °C spikes | 0/12 on rolling z | 12/12, 0 clean column hits |

The prototyped bundle was 97.9% composite and 4.2% FPR. That number scored only spike and noise. It is not the pipeline result, and the constants were not retuned toward it.

## Still open

- **Hour-of-day residual** (`src/models/hour_of_day_residual.py`) is unwired. Defaults are 2.0 °C, 8 consecutive hours, 3 other days. July clearsky: **55** flagged samples in 5 hours. A synthetic +3 °C bias flagged at the 8th hour; a six-hour afternoon flagged 0. Do not wire it while the clean week still flags.
- **Upload CUSUM** is not emitted. Slow drift (0→+4 °C over 5 days) is still undetected on CSV upload. Do not point `for_physical_series()` at normalized residuals.
- **Live climate rules** stay idle. `LiveReadingEvent` has no setpoint, outdoor temperature, or humidity deficit. Do not invent those fields.
- **Clean FPR 19.16%** is inside the accepted 0.90 bar. Another noise-tuning round was declined.
- **Design items not done:** one scoring path for upload, live, and `/api/inference`; full denormalization before every physical rule (BACKLOG-23); a clean-tenant latent gallery; persisting `LearningState` so CUSUM accumulates inside one process rather than only across saved rows. The accuracy harness builds a new pipeline per window, so it does not show multi-call drift.
- **Not re-reviewed** after the architect's ring-admission and variance-floor fixes. Code review before those fixes was PASS WITH NOTES.
- The full pytest suite is the merge gate and has not been run here. Do not scale the API past one replica.

## How to resume

1. Stay on `feat/anomaly-detection-module-refactoring`.
2. Re-measure with `./venv/bin/python -m pytest tests/models/test_detection_accuracy.py -q --tb=line`. Do not lower `_COMPOSITE_BAR`.
3. Run the full suite only when preparing to merge: `./venv/bin/python -m pytest tests/ -q`.
4. Next product work, in order: drop the hour-of-day clean hits before wiring it; persist upload CUSUM on physical units only; add climate fields only when a live source actually has them.
