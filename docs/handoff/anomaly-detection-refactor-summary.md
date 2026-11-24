# Anomaly detection refactor — summary

**Status as of 2026-11-24, merges through #145.** Authoritative detail lives in [`handover-2026-11-15.md`](./handover-2026-11-15.md) (§0 progress board, §§9–12 measurements, §14 and §15 work on 23–24 November). This file is the short scoreboard only.

**Where the code is:** `main` at `91335bf` (merged **#108–#119** and **#122–#143**, ADRs 013 and 014, docs **#120**). Next: grower labels and the per-scope enable decision (C3), then ADR-014 Phase A. The old branch `feat/anomaly-detection-module-refactoring` is historical.

**Bar / gate:** composite ≥ 0.90 = `0.5 × clean specificity + 0.5 × mean recall` (spike, noise, level-shift) on the empty-DB harness in `tests/models/test_detection_accuracy.py`.

| Metric | Early gate (bar accepted) | Now |
|---|---|---|
| Composite | 0.904 | **0.997** |
| Clean flags / 167 | 32 | **1** |
| Seeded-DB composite | n/a | **0.997**, gated at 0.90 (#125, #126; was 0.823 in §12 of handover) |

Plan canvas: [`anomaly-detection-improvements.canvas.tsx`](./anomaly-detection-improvements.canvas.tsx). Audit canvas: [`anomaly-audit-main.canvas.tsx`](./anomaly-audit-main.canvas.tsx).

## Completion vs initial scope

| Scope | Complete |
|---|---:|
| Original Phase 1 (P1.1–P1.10) | **100%** |
| Never-detected gaps (8) | **80%** |
| Original Phase 2 (P2.1–P2.6) | **55%** |
| Design list (D1–D8) | **48%** |
| Expanded detector program (#108–#145) | **90%** |
| **Whole initiative (equal weight)** | **~75%** (was 73%) |
| **Detector / operator path alone** | **~90%** (was 86%) |

Percentages are effort against the plan, not a claim that field accuracy is finished. “Measured, no ship” counts as finished investigation. The change since 2026-11-23 comes from two rows: the design list (D1 designed in ADR-014, not implemented) and the expanded program (second fixture and RH·CO₂ measurement closed; C3 mostly built but not enabled). The other rows did not move.

## Done (product)

- Phase 1 musts shipped; P1.2 robust LSTM gate **on** by default (#130, reverses the earlier “off” decision: LSTM gives scores, not upload alerts).
- Upload MAD spikes → reversion + lower threshold; live lag-1 reversion (#108, #113, #114).
- Noise: cold-start RH baseline, per-sensor calibrated ratios (#111, #112).
- Precedence, BOCPD any-in-window, OOB / humidity_fault labels, flatline eps, light excluded from stuck/flatline/MAD.
- Level-shift → `calibration_drift`; catalog + audit mismatches.
- CUSUM and ensemble **non-voting** after measured false-alarm rates; Mahalanobis history helpers in, vote **off** (re-arm blocked).
- Drift watch: persist, shadow upload, API, Beta UI, backfill (#124, #128), display fix (#129); ring seed at live start, first-episode race fixed (#122).
- Seeded false alarms fixed (#125: composite 0.823 → 0.997, clean flags 59 → 1) and the seeded run gated in CI (#126). Review findings (#127), physics check on reading time (#123).
- Upload false alarms cut (#130): tenant year 478 → 346 alerts, July clean 2 → 0.
- Production: drift-watch backfill ran for tenant_1/gh_A (53 chunks, 26 episodes; run twice, second after migration 031); `AUTH_RESEND_FROM` set.
- Second accuracy fixture from tenant clean weeks, gated and local only (#133). Humidity/CO₂ upload spikes measured, no ship (#132); CSV optional-column gap fix (#143).
- C3 drift-watch alert policy built, flag off: ADR-013 (#134), gate and config (#136), writer, migration 031 and feedback (#137), page (#138), admin route (#139), dry-run script (#140), tests (#141), in-sample fix (#142). Dry run on production: `temp_paired` stays off (6 episodes in the trailing 13 weeks, gate allows 3); 1 alert would be written if enabled.
- ADR-014 one scoring path (#144), design only. `CLAUDE.md` baseline 4203 passed, 48 skipped, 3 xfailed, 0 failed (#145).
- 25 vs 26 episodes explained: 104,921 production rows vs 105,120 CSV timestamps; the 199 missing rows have empty values and the upload drops them.
- QA harness: seeded mode, amplitude sweep, band-breach pin (#119). Humidity 2 %RH alternatives and ≤2 °C / window-bias spikes: measured, not shipped.

## Left

1. Grower labels and per-scope enable for drift-watch alerts (C3). Waiting on labels; no admin-scoped API key exists yet; alerts flag off.
2. Anomaly-feed drift card (ADR-013 step 7, second part).
3. C2: drift-watch RH residual model (`rh_virtual` shadow, 1.66/month, budget not met).
4. Upload gap: +3 °C hold and +4 °C slow drift not detected; 319 statistical hits remain (pipe temperatures, `measured_hd`).
5. ADR-014 Phase A (byte-identical move into `src/models/scoring/`, OQ1 decided), then Phase B.
6. ADR-014 open questions OQ2–OQ4 (LSTM as alert source; checkpoint normalisation ranges; live score is raw/2 against 0.05 while the config comment says raw; unmeasured, not changed).
7. Design list D2 (live side) and D3 (denormalise everywhere, BACKLOG-23).
8. Blocked: Mahalanobis re-arm (no reference that clears ~34 out-of-band clean points); live climate rules (no setpoint/outdoor on live events); VAE latent gallery.
9. Process: band-breach ADR (needs a new number; ADR-013 and ADR-014 are taken).

## How to measure

```bash
./venv/bin/python -m pytest tests/models/test_detection_accuracy.py -q --tb=line
# Full suite only when preparing merge / owner remeasure:
./venv/bin/python -m pytest tests/ -q
```

Do not lower `_COMPOSITE_BAR`. Do not invent live climate fields. Do not scale the API past one replica.
