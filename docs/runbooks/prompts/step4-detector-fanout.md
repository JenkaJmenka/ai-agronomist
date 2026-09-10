TASK: Phase 1 of the convergence design — run the stateless detectors on the CSV upload
path, within a measured performance budget.

Read `docs/architecture/upload_detector_convergence.md` (Phase 1 section) first.

## Prerequisites — verify, do not assume

1. ADR-008 tenant baseline implemented (Step 2).
2. NoiseDetector and Mahalanobis recalibrated (Step 3) — each under 5% firing on
   `tests/fixtures/native_july_clearsky.csv` while still firing inside the December
   fault window. Confirm by running `tests/models/test_detector_calibration.py`.

**If Step 3 recommended a detector stay OUT, honour that.** Read its done-signal /
report. Do not add a detector that was measured as unusable.

## Performance budget — this is a hard constraint

Measured 2026-09-10 on the real 105,120-row export, `torch.set_num_threads(1)`:

| Quantity | Measured |
|---|---|
| Current whole upload path, end-to-end | **11 s** |
| — of which LSTM-VAE | 4.5 s (0.043 ms/sequence, batched) |
| Peak RSS | 435 MB |
| `ensemble.detect`, per sequence | **1.05 ms → 111 s for 105k** |
| 4 rule detectors, naive per-sequence loop | 0.33–0.45 ms/window → ~40 s |

**Budget: a full-year upload must stay under 60 s at 1 torch thread.**

Consequences you must respect:
- `EnsembleAnomalyDetector` at 1.05 ms/sequence is **111 s on its own — 10x the entire
  current pipeline and over budget by itself.** Either vectorise it or leave it out of
  Phase 1. Do not add it unvectorised. This reverses the design doc's original guess
  that the LSTM was the bottleneck; the LSTM is 4.5 s of 11 s.
- `OutOfBoundsDetector` is a single NumPy comparison over a column — vectorise it across
  the chunk rather than looping 20-step windows.
- `DropoutDetector` checks null fractions per row — vectorise similarly.
- Measure your result and report it. A design assumption in this project was already
  wrong by 230x; do not add another.

## What to implement

1. Add the qualifying stateless detectors to `score_portion`
   (`src/api/services/upload_portions.py`). Candidates from the design:
   DropoutDetector, NoiseDetector, OutOfBoundsDetector, SpikeMADDetector,
   BiasEMADetector, MultivariateMahalanobisDetector — minus anything Step 3 excluded.
   BOCPD and Ensemble only if you can bring them inside budget.

2. **Skip imputed channels.** A channel that is a constant fill value produces
   guaranteed false positives. `detect_optional_sensor_presence`
   (`src/vendors/csv_adapter.py`) identifies them and
   `cold_run_upload.py:261` already threads them to the stuck detector as
   `StuckCarry(skip_channels=...)`. Extend that same mechanism to the new detectors —
   do not invent a second one. This is a measured failure, not hypothetical: with the
   skip empty, the stuck detector raises a false `co2` stuck alert on the constant
   `400.0` fill.

3. **Preserve bounded memory.** `score_portion` runs per 5,000-row chunk with rolling
   carries so a year never lands in memory at once. Any state your detectors need must
   thread through a carry the way `StatCarry` and `StuckCarry` do. There are already two
   carries; a third bespoke one is a smell — prefer extending or generalising.

4. **Route alerts through the existing label-integrity path.** Every alert must carry the
   real triggering channel, the detector identity, and a severity — the columns added by
   migration 024 (`trigger_channel`, `trigger_detector`, `detected_severity`,
   `trigger_value`). Do not reintroduce value-based inference at render time; that was
   the original defect this whole phase fixed. Reasoning text must name only the channel
   that actually fired.

5. **Feed episode grouping.** New alerts must group into episodes like existing ones
   (`src/models/episode_grouper.py`) rather than emitting one row per sample.

## Explicitly out of scope

- Do NOT wire in `ComprehensiveInferencePipeline` wholesale. Phase 1 is a shared-detector
  fan-out, not adopting the other orchestrator.
- Do NOT add BandBreachDetector or DiurnalProfileDetector — both need config/DB
  provisioning and are Phase 2 in the design.
- Do NOT add the stateful detectors (CUSUM, EWMA, online learner). The design classifies
  them as needing reformulation.
- Do NOT change the stuck-fault gate, the 6.00 h measured latency, episode semantics, the
  recovery score cap, the light unit, or the psychrometric suppression.

## Acceptance

Test file: `tests/api/test_detector_fanout.py`

  - each newly added detector can fire on the December fixture and is attributed to the
    correct `trigger_channel` and `trigger_detector`
  - **`native_july_clearsky.csv` produces AT MOST 5 raw alerts in total.**
    Measured 2026-09-10, that fixture currently produces **0 alerts and 0 episodes** —
    a healthy week is completely silent today, and that is the property to protect. A
    percentage tolerance is meaningless against zero, so this is an absolute cap. Report
    the actual count. If a newly added detector pushes it above 5, that detector is not
    ready — exclude it and say so rather than raising the cap.
  - `native_dec_stuck_fault.csv` still produces its fault episodes. Measured baseline:
    **115 raw alerts, 16 episodes**. The count may legitimately rise as detectors are
    added; what must NOT change is that the humidity stuck fault is still present with
    6.00 h latency and 163.83 h duration.
  - imputed channels (e.g. `co2` on these files) produce zero alerts from any detector
  - a full-year run stays under the 60 s budget — measure it, mark the test slow so it
    can be deselected, and report the measured seconds
  - the December stuck fault is still detected at 6.00 h latency with 163.83 h duration
  - alerts still group into episodes; one continuous event is not one row per sample

## Regression

Expected failure remains the SQLite NaN one in `test_023_migration.py`. Leave it.

    venv/bin/python -m pytest tests/ -q --timeout=900 --ignore=tests/load

Report in your done signal: which detectors you added, which you left out and why, the
July alert count before and after, and the measured full-year runtime.

When complete:
mkdir -p tasks/done
echo '{"agent":"ml-engineer","status":"done","output":"src/api/services/upload_portions.py","tests_passed":true,"detectors_added":"REPLACE","detectors_excluded":"REPLACE","july_alerts_before":"REPLACE","july_alerts_after":"REPLACE","fullyear_seconds":"REPLACE"}' > tasks/done/ml-engineer.json
On failure:
echo '{"agent":"ml-engineer","status":"failed","reason":"brief"}' > tasks/done/ml-engineer.json
