TASK: Recalibrate two detectors that are currently unusable. Both are measured, both are
blocking the detector fan-out.

## Prerequisite — verify before you start

ADR-008 (per-tenant ensemble baseline) must already be implemented. Confirm:

    grep -rn "model_baseline" src/ --include=*.py | grep -v "^src/db/models.py"

If that returns nothing but `model_registry.py`, ADR-008 has NOT landed. **Stop and
report that** — recalibrating against the synthetic 10-sequence default baseline means
tuning to a fictional normal, and the work will have to be redone.

## The measurements

Per-window firing rates, measured 2026-09-10 on the two committed fixtures:

| Detector | `native_july_clearsky.csv` (healthy) | `native_dec_stuck_fault.csv` (real fault) |
|---|---|---|
| **NoiseDetector** | **69.6%** (1389/1997) | 3.1% (106/3368) |
| **MultivariateMahalanobisDetector** | **38.3%** (764/1997) | 9.5% (319/3368) |
| OutOfBoundsDetector | 0% | 0% |
| SpikeMADDetector | 0% | 0% |

Two distinct failures:

**NoiseDetector — false positives.** Fires on 7 of every 10 windows of healthy
clear-sky summer data. Its per-sensor targets (`SENSOR_NOISE_TARGETS`,
`src/config.py`) and `NOISE_RATIO_THRESHOLD` were calibrated for live-path 20-step
windows. Upload data reaches the detector through a different route
(`src/vendors/csv_adapter.py` normalisation, variable real-world cadence), so the
effective noise floor differs. Note the config comment already records that
`NOISE_RATIO_THRESHOLD` was set to 10.0 — "5x more sensitive" than the 50.0 in the
spec — which is a likely contributor.

**Mahalanobis — inverted.** Fires 4x more often on healthy data than on the actual
week-long fault. This is not noise, it is backwards: it finds normality more
surprising than a dead sensor. Diagnose the cause before touching a threshold. Likely
candidates, in order: (a) it was fitted against the synthetic default baseline rather
than real data — which ADR-008 now fixes, so **re-measure after that lands before
assuming anything else**; (b) the covariance estimate is degenerate on a frozen channel
with zero variance; (c) the CO2 channel is a constant `400.0` fill value on these files
and a constant column makes a covariance matrix singular.

That last point is concrete and checkable: `detect_optional_sensor_presence`
(`src/vendors/csv_adapter.py`) reports which channels are imputed fill values, and
`cold_run_upload.py:261` already passes them to the stuck detector as
`StuckCarry(skip_channels=...)`. A multivariate detector fed a constant column is a
known-bad situation.

## What to implement

1. **Re-measure both detectors first**, on both fixtures, with the ADR-008 tenant
   baseline in place. Record the numbers. If Mahalanobis has already improved, the cause
   was the baseline and the remaining work is smaller than it looks. **Report the
   before/after either way.**

2. **Diagnose before tuning.** For each detector, state in a comment what the actual
   mechanism of failure is. Do not adjust a constant until you can say why the old value
   was wrong. This project has a guardrail against magic numbers and a history of
   thresholds tuned by trial and error with no recorded rationale.

3. **Exclude imputed/constant channels from multivariate detection.** A channel that is
   a constant fill value carries no information and makes a covariance matrix singular.
   Reuse the existing `skip_channels` mechanism rather than inventing a second one.

4. **Recalibrate against BOTH fixtures.** The targets:
   - healthy July fixture: **under 5%** of windows firing, for each detector
   - December fault fixture: the detector must still fire inside the known fault window
     (humidity frozen 2026-12-18T13:00Z → 2026-12-25T08:55Z)

   A detector that is silent on both is not fixed, it is disabled. State the achieved
   rates. If you cannot get a detector under 5% on healthy data while keeping it
   sensitive to the fault, say so and recommend it stay OUT of the fan-out — that is a
   legitimate and useful outcome, and better than shipping a broken detector.

5. Every changed constant keeps its `src/config.py` home, gets an env-var override
   following the established `os.environ.get` pattern, and gains a comment recording
   what it was, what it is, and the measurement that justified the change.

## Explicitly out of scope

- Do NOT wire these detectors into `score_portion`. That is Step 4.
- Do NOT touch the stuck-fault detector, episode grouping, the recovery score cap, or the
  psychrometric suppression. All are verified working.
- Do NOT change `SpikeMADDetector` thresholds to make it fire. Its 0% on a fixture
  containing a real fault is unexplained — **investigate and report** what it is doing,
  but do not tune it into firing without understanding why it is silent. A detector made
  loud without a diagnosis is worse than a quiet one.

## Acceptance

Test file: `tests/models/test_detector_calibration.py`

  - NoiseDetector fires on <5% of windows of `native_july_clearsky.csv`
  - Mahalanobis fires on <5% of windows of `native_july_clearsky.csv`
  - each still fires at least once inside the December fault window
  - a constant/imputed channel is excluded from multivariate detection
  - the calibration constants are read from `src/config.py`, not hardcoded at call sites

Assert the rates against measured thresholds, not against "whatever the code now
produces". Pin the fault-window sensitivity explicitly — a test that only checks the
false-positive rate can be passed by disabling the detector.

## Regression

Baseline **719 passed, 1 failed** (plus whatever Steps 1–2 added). Expected failure is
the SQLite NaN one in `test_023_migration.py`. Leave it.

    venv/bin/python -m pytest tests/ -q --timeout=600 --ignore=tests/load

Confirm the core capability is untouched — stuck latency still 6.00 h, frozen durations
still 34.83 / 22.08 / 47.92 / 163.83 h:

    venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py tests/api/test_episode_grouping.py -q --timeout=300

When complete:
mkdir -p tasks/done
echo '{"agent":"ml-engineer","status":"done","output":"src/config.py","tests_passed":true,"noise_july_before":"69.6%","noise_july_after":"REPLACE","mahal_july_before":"38.3%","mahal_july_after":"REPLACE","spikemad_diagnosis":"REPLACE","recommend_out":"REPLACE"}' > tasks/done/ml-engineer.json
On failure:
echo '{"agent":"ml-engineer","status":"failed","reason":"brief"}' > tasks/done/ml-engineer.json
