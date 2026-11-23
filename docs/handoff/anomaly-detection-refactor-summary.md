# Anomaly detection refactor — summary

**Status as of 2026-11-23, merges through #130.** Authoritative detail lives in [`handover-2026-11-15.md`](./handover-2026-11-15.md) (§0 progress board, §§9–12 measurements, §14 work on 23 November). This file is the short scoreboard only.

**Where the code is:** `main` at `2ee7bfb` (merged **#108–#119** and **#122–#130**, docs **#120**). Next: second accuracy fixture, then the C3 alert policy review. The old branch `feat/anomaly-detection-module-refactoring` is historical.

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
| Design list (D1–D8) | **45%** |
| Expanded detector program (#108–#130) | **86%** |
| **Whole initiative (equal weight)** | **~73%** (was 71%) |
| **Detector / operator path alone** | **~86%** (was 82%) |

Percentages are effort against the plan, not a claim that field accuracy is finished. “Measured, no ship” counts as finished investigation. The change since 2026-11-16 comes from Phase 1 (P1.2 closed by #130) and the expanded program row; the other rows did not move.

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
- Production: drift-watch backfill ran for tenant_1/gh_A (53 chunks, 26 episodes); `AUTH_RESEND_FROM` set.
- QA harness: seeded mode, amplitude sweep, band-breach pin (#119). Humidity 2 %RH alternatives and ≤2 °C / window-bias spikes: measured, not shipped.

## Left

1. Second accuracy fixture.
2. Drift-watch RH residual model (`rh_virtual` shadow, 1.66/month, budget not met).
3. C3 alert policy: 14 armed temperature episodes in 12 months is about 1.2/month, double the 0.55/month calibration rate and above the 1.0/month budget. Review before armed episodes may alert.
4. Humidity and CO₂ spikes on upload.
5. Upload gap: +3 °C hold and +4 °C slow drift not detected; 319 statistical hits remain (pipe temperatures, `measured_hd`).
6. Mahalanobis re-arm (blocked on reference that clears ~34 out-of-band clean points).
7. Structural: one scoring path; denormalise everywhere (BACKLOG-23); VAE latent gallery.
8. Live climate rules (blocked — no setpoint/outdoor on live events).
9. Process: band-breach ADR; `CLAUDE.md` baseline under `./venv`.
10. Explain 25 (local) vs 26 (production) backfill episodes.

## How to measure

```bash
./venv/bin/python -m pytest tests/models/test_detection_accuracy.py -q --tb=line
# Full suite only when preparing merge / owner remeasure:
./venv/bin/python -m pytest tests/ -q
```

Do not lower `_COMPOSITE_BAR`. Do not invent live climate fields. Do not scale the API past one replica.
