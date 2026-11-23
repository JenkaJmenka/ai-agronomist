# Tenant accuracy fixture: measured results, 2026-11-23

Module: `tests/models/test_detection_accuracy_tenant.py`. Gate: `PASS1_TENANT_CSV` (the same variable as `tests/models/test_pass1_tenant_corpus.py`). CI never sets it. The corpus is customer data. It stays outside the repository. This note gives aggregates only.

Branch `test/tenant-accuracy-smoke`, base `origin/main` at `2ee7bfb`. Production code was not changed. `_COMPOSITE_BAR` is still 0.90. Detectors were not retuned.

## Corpus

| Stage | Rows |
|---|---:|
| Raw data lines | 105,121 |
| After `CSVAdapter.normalize` (the July gate's ingest path) | 104,922 |

The corpus has no CO2 column. The adapter fills a constant (span 0), so the harness puts in its plausible CO2 trace, as it does for July.

## Week selection rule

The rule was fixed before any week was scored. No detector output was used.

- 7-day blocks, aligned to 2026-01-01 00:00 UTC.
- Exactly 2016 rows, regular 5-minute cadence, no NaN in temperature, humidity or light.
- Temperature, humidity and light inside `OOB_LIMITS`.
- Humidity never at 100 %RH.
- Longest flat run (`STUCK_WALLCLOCK_EPSILON`) on temperature and on humidity shorter than 36 samples (half the 6 h wall-clock stuck gate).
- The first qualifying block in each season window: winter Jan–Feb, spring Mar–May, autumn Sep–Nov. Summer is the July gate.

| Season | Week (UTC) | Rows | Longest flat run T / RH (samples) | Temperature range (°C) | RH range (%) |
|---|---|---:|---|---|---|
| winter | 2026-01-08 → 01-15 | 2016 | 6 / 16 | 13.74 – 29.38 | 57.13 – 88.88 |
| spring | 2026-04-02 → 04-09 | 2016 | 12 / 20 | 13.80 – 31.02 | 50.00 – 88.00 |
| autumn | 2026-09-03 → 09-10 | 2016 | 10 / 19 | 13.20 – 28.36 | 60.46 – 90.83 |

Blocks rejected before the chosen one in each window: winter 1 (01-01, cadence gap); spring 4 (03-05, 03-12, 03-26 for cadence gaps; 03-19 for a humidity flat run of 36 samples or more); autumn 0. A block that straddles a window edge (for example 02-26 → 03-05) is in neither window. Every block from 26 Nov to 24 Dec fails because it contains saturated humidity.

## Composite per week (empty database, same harness as July)

333 windows per week (stride 6, sequence 20): 166 calibration, 167 evaluation. 40 trials for each injected fault.

| Week | Clean flagged | Clean specificity | Spike +6 °C | Noise σ 1 °C | Level shift | Composite | Bar | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| July (reference, `main`) | 1/167 | 0.994012 | 40/40 | 40/40 | 40/40 | 0.997006 | 0.90 | pass |
| winter 01-08 | 3/167 | 0.982036 | 40/40 | 40/40 | 40/40 | **0.991018** | 0.90 | pass |
| spring 04-02 | 0/167 | 1.000000 | 40/40 | 40/40 | 40/40 | **1.000000** | 0.90 | pass |
| autumn 09-03 | 2/167 | 0.988024 | 40/40 | 40/40 | 40/40 | **0.994012** | 0.90 | pass |

All clean flags are `calibration_drift` (winter 3, autumn 2). No week needed an xfail.

Labels on the injected faults are the same in all three weeks: spike 40 `spike_fault`; noise 39 `accuracy_decline` and 1 `spike_fault` (the known #114 mix-up); level shift 40 `calibration_drift`. Labels are recorded and are not part of the bar.

The injected half scores the harness's synthetic quiet windows at its fixed July noon. A tenant week changes that half only through its calibration windows. That is why it is 1.0 everywhere. The clean half is where the weeks really differ.

### Recorded, not gated

| Week | Real-window +6 °C spike (12) | Real-window level shift (12) | Sweep +2 / +3 / +4 / +6 °C (of 40) |
|---|---|---|---|
| winter | 12/12 (8 `spike_fault`, 3 `calibration_drift`, 1 `accuracy_decline`) | 4/12 (4 `calibration_drift`) | 1 / 40 / 40 / 40 |
| spring | 12/12 (8 `spike_fault`, 3 `accuracy_decline`, 1 `calibration_drift`) | 5/12 (4 `calibration_drift`, 1 `accuracy_decline`) | 1 / 40 / 40 / 40 |
| autumn | 12/12 (11 `spike_fault`, 1 `accuracy_decline`) | 3/12 (3 `calibration_drift`) | 1 / 40 / 40 / 40 |

July on `main`: real-window spike 12/12, level shift 4/12.

## December humidity freeze

Path: `run_cold_run_upload` on the corpus file in place (no copy), mock LSTM as in `tests/api/test_fixture_a_stuck_contract.py`. The ground truth is measured from the adapter frame on its own: flat humidity runs that cross `STUCK_WALLCLOCK_MIN_HOURS` (6 h) and `STUCK_WALLCLOCK_MIN_SAMPLES`.

| Measure | Value |
|---|---:|
| Gate-crossing humidity runs, whole corpus | 18 |
| Of those, runs that overlap December | 12 |
| Of those, runs with a December onset | 11 |
| Humidity stuck alerts, whole corpus | 18 |
| December runs with an alert at their exact onset | 12/12 (recall 1.0) |
| December-onset alerts (all `urgent`) | 11 |
| Stuck alerts before 1 Nov | 0 |
| Stuck alerts on a channel other than humidity | 0 |

Finding: the freeze does not begin in December. The first gate-crossing run starts on 21 Nov. Saturated humidity first shows in the 19–26 Nov block. That first run is flat below 100 %RH. Every run after it is at 100 %RH.

## Elapsed

The harness took 350 s, 696 s and 550 s per week on a loaded two-core host. It was 197 s for July in the same session. The module is opt-in and CI does not run it.

## Reproduction (second session, same day)

A second session ran the measurements again on the same tree and got the same values:

- Tenant module with `PASS1_TENANT_CSV` set: 12 passed in 538 s.
- Per-week figures (composite, clean flags, injected recalls and labels, sweep, real-window rows): the same as the tables above. Harness time was 158 s, 148 s and 147 s per week on an idle host.
- July gate, `ACCURACY_SEEDED=1` (empty-database and seeded runs), on this branch and on `origin/main` (`2ee7bfb`): composite 0.997006 in all four runs, 1/167 clean flags, 40/40 for each injected fault. The two recorded reports are the same in every field except elapsed time. The refactor of `test_detection_accuracy.py` does not change the July gate.
