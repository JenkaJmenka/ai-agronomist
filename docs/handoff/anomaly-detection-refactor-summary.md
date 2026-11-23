# Anomaly detection refactor — summary

**Status as of 2026-11-23.** Authoritative detail lives in [`handover-2026-11-15.md`](./handover-2026-11-15.md) (§0 progress board, §§9–12 measurements, §1 pause note). This file is the short scoreboard only.

**Where the code is:** `main` at `7f7d284` (merged **#108–#119**, docs **#120**). No detector code after 2026-11-16. Next: second accuracy fixture. The old branch `feat/anomaly-detection-module-refactoring` is historical.

**Bar / gate:** composite ≥ 0.90 = `0.5 × clean specificity + 0.5 × mean recall` (spike, noise, level-shift) on the empty-DB harness in `tests/models/test_detection_accuracy.py`.

| Metric | Early gate (bar accepted) | Now |
|---|---|---|
| Composite | 0.904 | **0.997** |
| Clean flags / 167 | 32 | **1** |
| Seeded-DB composite | n/a | **0.823** recorded, not gated (§12 of handover) |

Plan canvas: [`anomaly-detection-improvements.canvas.tsx`](./anomaly-detection-improvements.canvas.tsx). Audit canvas: [`anomaly-audit-main.canvas.tsx`](./anomaly-audit-main.canvas.tsx).

## Completion vs initial scope

| Scope | Complete |
|---|---:|
| Original Phase 1 (P1.1–P1.10) | **95%** |
| Never-detected gaps (8) | **80%** |
| Original Phase 2 (P2.1–P2.6) | **55%** |
| Design list (D1–D8) | **45%** |
| Expanded detector program (#108–#119) | **82%** |
| **Whole initiative (equal weight)** | **~71%** |
| **Detector / operator path alone** | **~82%** |

Percentages are effort against the plan, not a claim that field accuracy is finished. “Measured, no ship” counts as finished investigation.

## Done (product)

- Phase 1 musts shipped or deliberately decided (robust LSTM gate **off**).
- Upload MAD spikes → reversion + lower threshold; live lag-1 reversion (#108, #113, #114).
- Noise: cold-start RH baseline, per-sensor calibrated ratios (#111, #112).
- Precedence, BOCPD any-in-window, OOB / humidity_fault labels, flatline eps, light excluded from stuck/flatline/MAD.
- Level-shift → `calibration_drift`; catalog + audit mismatches.
- CUSUM and ensemble **non-voting** after measured false-alarm rates; Mahalanobis history helpers in, vote **off** (re-arm blocked).
- Drift watch: persist, shadow upload, API, Beta UI; ring seed at live start.
- QA harness: seeded mode, amplitude sweep, band-breach pin (#119). Humidity 2 %RH alternatives and ≤2 °C / window-bias spikes: measured, not shipped.

## Left

1. Second accuracy fixture.
2. Drift-watch RH residual model; when shadow may alert; ring-seed first-moments race.
3. Upload LSTM percentile; humidity and CO₂ spikes on upload.
4. Mahalanobis re-arm (blocked on reference that clears ~34 out-of-band clean points).
5. Structural: one scoring path; denormalise everywhere (BACKLOG-23); VAE latent gallery.
6. Live climate rules (blocked — no setpoint/outdoor on live events).
7. Process: code review of merged PRs; band-breach ADR; `CLAUDE.md` baseline under `./venv`; Railway / `AUTH_RESEND_FROM`.

## How to measure

```bash
./venv/bin/python -m pytest tests/models/test_detection_accuracy.py -q --tb=line
# Full suite only when preparing merge / owner remeasure:
./venv/bin/python -m pytest tests/ -q
```

Do not lower `_COMPOSITE_BAR`. Do not invent live climate fields. Do not scale the API past one replica.
