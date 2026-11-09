# SCADA Configuration & Anomaly Detection Engine — Gap Analysis & Phased Plan

**Author:** architect (claude-fable-5) · **Date:** 2026-07-24
**Status:** PROPOSED — awaiting user answers to §5 open questions before Phase 1 dispatch
**Scope:** Requirements analysis, gap matrix, and phased implementation plan for the
operator-facing SCADA Configuration & Anomaly Detection spec. Incorporates the
architect's math review corrections (authoritative). No implementation code here.

---

## 1. Executive summary

The codebase already implements **most of the detection math** in this spec — often
more rigorously than the spec itself (CUSUM already has K and H defined; the spike
velocity+reversal rule is already spec-exact; all "unspecified constants" the spec
leaves open already live in `src/config.py`). The plan below **extends, not
duplicates**.

The real gaps are:

1. **Configuration surface** — crop profiles, month→season mapping, and seasonal
   baselines do not exist anywhere (no DB tables, no API, no UI). Operator setpoints
   exist only as a **frontend localStorage overlay** with no backend and no effect
   on scoring.
2. **~~One confirmed production bug~~** (**FIXED 2026-07**) — the psychrometric HD
   formula used the hPa coefficient (216.7) with kPa inputs, making HD ~10× too
   small, paired with a fixed 0.05 g/m³ tolerance.  Corrected: coefficient →
   2167.0, tolerance → 0.5 g/m³, DEHUMID_HD_CRIT_G_M3 → 5.0. Guard:
   `tests/models/test_framework_spec_vectors.py`.
3. **Seasonal-sigma scaling** — every univariate detector uses static per-sensor
   constants; none can consume an operator-configured seasonal σ.
4. **Pass 1 scrub** — uses physics flags + IsolationForest but lacks the spec's
   MAD robust z-score stage.

---

## 2. Gap matrix

Legend: **DONE** = exists and matches spec (or exceeds it) · **PARTIAL** = exists,
needs extension/correction · **MISSING** = does not exist.

### 2.1 Config matrix (spec §1)

| Spec item | Status | Evidence & gap |
|---|---|---|
| Per-crop setpoint bounds (tomato/cucumber, day temp, VPD) | **MISSING** | No crop entity anywhere. `web/features/monitoring/lib/channelMeta.ts` has static per-channel `optimal` ranges (incl. `vpd: [0.5, 1.5]`); `useSetpoints.ts` + `SetpointsPanel.tsx` let the operator override them — but **localStorage-only** (`gh-setpoints:{tenant}:{gh}`), no backend persistence, no crop dimension, no day/night split. |
| Bound breach ⇒ instant categorical anomaly | **PARTIAL** | `OutOfBoundsDetector` (hard hardware limits, `OOB_LIMITS`) and `SetpointAwareScadaDetector` (tracks climate-computer setpoint **columns** in uploaded CSVs via `SETPOINT_COLUMN_ALIASES`, Δ=`SETPOINT_TEMP_DELTA_C`) both exist and run in `comprehensive_pipeline`. `FaultType.OUT_OF_BOUNDS` exists in `fault_classifier.py`. **Gap:** neither consults operator/crop-configured bounds; the frontend setpoint overlay is display-only and never reaches the scoring path (batch or live). |
| Seasonal mapping UI (month → season) | **MISSING** | No UI, no API, no storage. |
| Seasonal baselines (mean/std/median) from telemetry in mapped months | **MISSING** | Only tangential support: `SeasonalBuffer` in `replay_buffer.py` (season-**tagged** replay sampling — consumes labels, does not produce them) and a `season` field on `anomaly.py`. `BaselineAnalyzer` computes global (not seasonal) ranges. No month-filtered baseline computation exists. |

### 2.2 Two-pass bootstrap (spec §2)

| Spec item | Status | Evidence & gap |
|---|---|---|
| Pass 1 — MAD robust z-score scrub (0.6745·(x−median)/MAD, cull \|z\|>3.5) | **MISSING** (as a Pass-1 stage) | `ColdStartBootstrap.run()` (`src/models/bootstrap.py`) scrubs via **psychrometric physics flags + IsolationForest** — no MAD z-score stage. A rolling-MAD detector exists (`spike_mad_detector.py`) but is a live detector, not the global-median Iglewicz–Hoaglin scrub the spec requires. Architect review: MAD/3.5 cutoff is correct math — add it as an additional Pass-1 stage; keep the physics flags (a strict superset of the spec is fine). |
| Pass 1 — IsolationForest(contamination=0.04) | **DONE** | `BOOTSTRAP_IF_CONTAMINATION = 0.04` in config; `_compute_if_flags()` in `bootstrap.py`. Guard rails (`BOOTSTRAP_MIN_IF_ROWS`, gas-null masking) already exist. |
| Pass 2 — LSTM-AE on scrubbed data, MSE loss, τ = 99th pct of clean training loss | **DONE** (with a documented deviation) | `Pass2BootstrapDetector` trains an **LSTM-VAE** (MSE+βKL training loss) on Pass-1 clean rows only; calibration threshold **is** computed on pure MSE reconstruction error at `BOOTSTRAP_THRESHOLD_PERCENTILE = 99.0` — matching the spec's τ definition exactly. Deviation: VAE vs plain AE. Recommend **keeping the VAE** (shared architecture with the live detector, threshold semantics already spec-conformant) — flagged as open question Q9. |
| Bootstrap API | **DONE** | `POST /api/v1/bootstrap/run` (`src/api/routes/bootstrap.py`), `src/api/services/cold_start_training.py`. |

### 2.3 Five univariate detectors (spec §3)

| Detector | Status | Evidence & gap |
|---|---|---|
| Spike (\|x_t−x_{t−1}\|>Δ_max AND reversal at t+1) | **DONE** — needs seasonal scaling + latency doc | `pattern_scores.score_spike()` primary path is spec-exact: `abs(diffs[t]) > vel_limit and diffs[t]*diffs[t+1] < 0`, with z-score fallback. `SpikeMADDetector` complements it. **Gaps:** (a) Δ_max is static (`SPIKE_DELTA_MAX` / `VELOCITY_LIMITS`), not seasonal-σ-scaled; (b) architect review: detector is **non-causal** (needs x_{t+1}) ⇒ the 1-tick detection latency in live scoring must be documented in `score_spike` and `src/live/scoring.py` docs. |
| Stuck (rolling variance < ε) | **DONE** — needs seasonal scaling | `StuckFaultDetector`: min rolling-window variance < threshold; `STUCK_WINDOW_K=10`, `STUCK_EPSILON`, `STUCK_VARIANCE_THRESHOLD`, `STUCK_FLAT_RANGE_THRESHOLD` all in config. Gap: ε static, not seasonal-σ-scaled. |
| Drift (two-sided CUSUM, slack K) | **DONE** — architect's corrections already satisfied; needs seasonal σ source | `CUSUMDriftDetector` (`cusum_drift_detector.py`): two-sided S+/S−, `CUSUM_K_SIGMA=0.25` (env-tunable; architect suggested ~0.5 — current 0.25 is deliberately more sensitive, ARL₀≈340 documented), alarm at `CUSUM_H_SIGMA=4.0` (in architect's 4–5 range), serialisable per tenant, `EMABaselineEstimator` supplies x̂ and rolling σ. **Gap:** σ comes from the EMA estimator, not the operator-configured seasonal σ; K/H are env vars, not per-tenant operator-tunable via `CustomerModelConfig`. |
| Precision degradation (high-pass → rolling std > σ_target·α) | **DONE** — needs seasonal scaling | `NoiseDetector`: 2nd-order Butterworth high-pass → rolling variance ratio vs `SENSOR_NOISE_TARGETS × NOISE_ALPHA (3.0)`; also a spec-faithful no-filter variant. Gap: σ_target static per sensor. |
| Dropout (all-missing persistence window P) | **DONE** — semantic mismatch to resolve | `DropoutDetector`: P=`DROPOUT_CONSECUTIVE_P=5` consecutive flagged steps. **Mismatch:** implementation flags ψ_t=1 when **ANY** sensor is NaN; this spec says **all-missing**. See open question Q4 (likely both are wanted: per-sensor dropout vs full poll loss). |

**Architect note on "unspecified constants":** ε, P, α, Δ_max, and window lengths
are all **already in `src/config.py`** — the remaining work is seasonal-σ scaling,
not constant extraction.

### 2.4 Physics validation (spec §4)

| Spec item | Status | Evidence & gap |
|---|---|---|
| Magnus/Tetens e_s(T) = 0.61078·exp(17.27T/(T+237.3)) kPa | **DONE** | `psychrometric_validator.calculate_saturation_vapor_pressure()` is character-for-character the spec formula (Tetens ≡ the spec's "Magnus" — same coefficients). Input guards (absolute zero, RH∈[0,100]) already exist. |
| HD_hat = C·(e_s−e_a)/T_K | **DONE** (fixed 2026-07) | `calculate_humidity_deficit()` now uses `2167.0 * (e_s - e_a) / t_kelvin` (= 216.7 × 10 for kPa inputs). Guard: `tests/models/test_framework_spec_vectors.py`. |
| Tolerance comparison vs measured HD | **DONE** (fixed 2026-07) | `PSYCHROMETRIC_HD_TOLERANCE = 0.5` g/m³ (env-overridable). `DEHUMID_HD_CRIT_G_M3` rescaled to 5.0. `_compute_physics_flags()` and `BOOTSTRAP_PHYSICS_HD_MAX` (35.0) both verified on the corrected scale. |
| Legitimate-venting downgrade of AI alerts | **PARTIAL** | `physics_validator.py` has the suppress/confirm `ValidationResult` machinery and `comprehensive_pipeline` sets `physics_validation_status` — the downgrade *mechanism* exists. **Gap:** no **measured HD channel exists anywhere in the data model** (`SensorReading` = temp/humidity/co2/light; `RawReading.data` JSON could carry one but no connector/CSV mapping produces it). Comparing derived-vs-measured HD is impossible today → open question Q1 (blocking for this sub-feature only). |

---

## 3. Target architecture

### 3.1 Data model (new tables, Alembic head 017 → 018)

```
crop_profiles
  id PK · tenant_id (indexed) · greenhouse_id NULL (NULL = tenant default)
  crop_type String(64)            -- 'tomato' | 'cucumber' | free text
  is_active Boolean               -- one active profile per (tenant, greenhouse)
  created_at / updated_at

crop_profile_bounds               -- normalized: one row per channel × period
  id PK · profile_id FK → crop_profiles
  channel String(32)              -- 'temperature' | 'vpd' | 'humidity' | ...
  period String(8)                -- 'day' | 'night' | 'any'
  min_value Float · max_value Float
  UNIQUE (profile_id, channel, period)

season_mappings
  id PK · tenant_id (indexed) · greenhouse_id NULL
  month Integer (1–12) · season_label String(32)
  UNIQUE (tenant_id, greenhouse_id, month)   -- every month maps to ≤1 season

seasonal_baselines                -- computed, never operator-edited
  id PK · tenant_id · greenhouse_id · season_label · channel
  mean Float · std Float · median Float · mad Float
  sample_count Integer · window_start/window_end DateTime · computed_at
  UNIQUE (tenant_id, greenhouse_id, season_label, channel)
```

Design notes:
- **Non-finite guard** on all Float writes (SQLite/Postgres NaN gap — known project
  constraint): baseline computation must reject NaN/inf before insert.
- Seed data: `DEFAULT_CROP_PROFILES` in `src/config.py` carries the spec values
  (tomato day-temp 18–26 °C, VPD 0.7–1.0 kPa; cucumber 24–28 °C, VPD 0.5–0.8 kPa)
  so a tenant with no stored profile still gets categorical bounds.
- Baselines are computed **strictly from telemetry whose timestamp month ∈ mapped
  months** (spec requirement), reading `SensorReading` (+ `RawReading` extras),
  excluding rows with `source='live'`? — no: include all sources, exclude synthetic
  (`anomaly_type IS NOT NULL`) rows. Minimum `SEASONAL_BASELINE_MIN_SAMPLES` guard;
  below it the baseline row is not written and detectors fall back to static config.

### 3.2 API contracts (all under existing auth; tenant-scoped)

```
GET    /api/config/crop-profiles                → [{id, crop_type, greenhouse_id, is_active, bounds: [{channel, period, min, max}]}]
POST   /api/config/crop-profiles                → create (body validated: min<max, known channels/periods)
PUT    /api/config/crop-profiles/{id}           → update bounds / activate
DELETE /api/config/crop-profiles/{id}

GET    /api/config/season-mappings?greenhouse_id=…   → {mappings: [{month, season_label}], unmapped_months: [..]}
PUT    /api/config/season-mappings              → full replace (12-entry idempotent upsert)

POST   /api/config/seasonal-baselines/recompute → 202 {job-style summary: per season/channel sample_count, skipped}
GET    /api/config/seasonal-baselines           → [{season_label, channel, mean, std, median, mad, sample_count, computed_at}]

GET    /api/config/effective-setpoints?greenhouse_id=…&at=…
       → resolved bounds for the scoring path AND the UI:
         {crop_type, period, bounds: {channel: [min, max]}, seasonal_sigma: {channel: std}}
```

Pydantic v2 schemas; SQLAlchemy 2.x async; cache invalidation via existing
`invalidate_response_cache()` after mutations (established pattern).

### 3.3 Detection integration

- **Categorical breach:** new thin `CropBoundsDetector` (or extension of
  `OutOfBoundsDetector` with an injected bounds provider) consulted in
  `comprehensive_pipeline` and in `src/live/scoring.py` enrichment → emits
  `FaultType.SETPOINT_BREACH` (new enum member) **instantly, bypassing the
  ML score** (spec: "instant categorical anomaly"). VPD bound checks require VPD
  in the batch path — currently live-only (`src/live/derived.py`); the derivation
  is a pure function and will be reused, not duplicated.
- **SeasonalScaleProvider** (new, `src/models/`): resolves per-(tenant, greenhouse,
  channel) σ_season from `seasonal_baselines` for the current month's season;
  falls back to static config constants when absent. Detectors take an optional
  per-sensor scale override in their constructors (backward-compatible —
  default `None` keeps today's behavior byte-identical):
  - spike: Δ_max = `SPIKE_SEASONAL_DELTA_MULT · σ_season` (fallback `SPIKE_DELTA_MAX`)
  - stuck: ε = `STUCK_SEASONAL_EPS_MULT · σ_season²` (fallback `STUCK_EPSILON`)
  - CUSUM: K = `CUSUM_K_SIGMA · σ_season`, H = `CUSUM_H_SIGMA · σ_season` — σ_season
    replaces/floors the EMA σ (open question Q7); K/H become per-tenant tunable via
    `CustomerModelConfig` (`CONFIG_KEYS` += `CUSUM_K_SIGMA`, `CUSUM_H_SIGMA`)
  - noise: σ_target = seasonal high-frequency σ where available (fallback
    `SENSOR_NOISE_TARGETS`)
  - dropout: unchanged (time-based, not amplitude-based)

### 3.4 Config keys to add to `src/config.py` (all env-overridable)

| Key | Default | Purpose |
|---|---|---|
| `DEFAULT_CROP_PROFILES` | spec values (tomato/cucumber) | seed bounds when no DB profile |
| `PSYCHROMETRIC_AH_COEFF_KPA` | `2167.0` (done — hardcoded in psychrometric_validator.py) | HD coefficient for e in kPa (fixed 2026-07) |
| `PSYCHROMETRIC_HD_TOLERANCE` | `0.5` (was 0.05; done — env-overridable in config.py) | configurable venting tolerance, g/m³ |
| `BOOTSTRAP_MAD_Z_CUTOFF` | `3.5` | Pass-1 robust z cull threshold |
| `BOOTSTRAP_MAD_SCALE` | `0.6745` | Iglewicz–Hoaglin consistency constant |
| `SEASONAL_BASELINE_MIN_SAMPLES` | `288` (≈1 day @5min) | guard before writing a baseline |
| `SPIKE_SEASONAL_DELTA_MULT` | `4.0` | Δ_max = mult·σ_season |
| `STUCK_SEASONAL_EPS_MULT` | `1e-3` | ε = mult·σ_season² |
| `DAY_PERIOD_START_HOUR` / `DAY_PERIOD_END_HOUR` | `6` / `22` | day/night bound selection (pending Q3) |

(ε, P, α, Δ_max, window lengths already exist — no action.)

---

## 4. Phased implementation plan

Standard pipeline per CLAUDE.md: dispatch → state → watcher → exit. File ownership
per the orchestrator table. **Phases 1 and 2 are independent → dispatch in parallel.**

### Phase 1 — Config foundation: DB + API + baseline service
**Agent:** backend-engineer (claude-sonnet-4-6) · **Writes:** `src/api/`, `src/db/` (+ `alembic/versions/018_*`) · **Depends on:** nothing
- Alembic 018: `crop_profiles`, `crop_profile_bounds`, `season_mappings`,
  `seasonal_baselines` per §3.1 (batch mode for SQLite; up/down consistent).
- Routes + Pydantic schemas per §3.2 under `src/api/routes/config.py` +
  `src/api/services/` (routes I/O-only; logic in services — house rule).
- `seasonal_baseline_service.py`: month-filtered mean/std/median/MAD per channel,
  non-finite guard, min-sample guard, synthetic-row exclusion.
- `src/config.py`: keys from §3.4 (config file is shared; ml-engineer owns it too —
  backend adds only the keys in this phase's rows to avoid merge conflict:
  `DEFAULT_CROP_PROFILES`, `SEASONAL_BASELINE_MIN_SAMPLES`, `DAY_PERIOD_*`).
- Smoke tests green before done signal.

### Phase 2 — Math corrections (independent of Phase 1)
**Agent:** ml-engineer (claude-opus-4-6) · **Writes:** `src/models/`, `src/config.py` · **Depends on:** nothing
- **HD unit fix (DONE 2026-07):** `psychrometric_validator.calculate_humidity_deficit` →
  coefficient `2167.0` (hardcoded); tolerance default → `0.5` g/m³ (env-overridable).
  Guard: `tests/models/test_framework_spec_vectors.py`.
  **Recalibrate downstream consumers of HD** (10× value shift): `_compute_physics_flags`
  in `bootstrap.py`, `DEHUMID_HD_CRIT_G_M3` usage in `setpoint_scada_detector.py` —
  verify each against known psychrometric fixtures (e.g. 25 °C/60 %RH → e_s≈3.17 kPa,
  HD≈9.2 g/m³) and adjust thresholds that implicitly assumed the broken scale.
- **Pass-1 MAD scrub:** add global-median robust z-score stage
  (0.6745·(x−median)/MAD, cull |z|>3.5) to `ColdStartBootstrap.run()` **before**
  IsolationForest; new flags surfaced in `BootstrapPass1Result` (`mad_flags`);
  physics + IF stages retained.
- **Spike latency doc:** document the non-causal 1-tick detection latency in
  `score_spike` docstring and `src/live/scoring.py` module docs.
- Constructor seams for per-sensor scale overrides on spike/stuck/CUSUM/noise
  detectors (default `None` → today's behavior; no seasonal lookup yet).
- Smoke tests green before done signal.

### Phase 3 — Seasonal-σ wiring + categorical breach detection
**Agent:** ml-engineer (claude-opus-4-6) · **Writes:** `src/models/` · **Depends on:** Phases 1 + 2
- `SeasonalScaleProvider` per §3.3 (reads `seasonal_baselines`; static fallback).
- `CropBoundsDetector` (day/night-aware, VPD-capable) + `FaultType.SETPOINT_BREACH`.
- `CustomerModelConfig.CONFIG_KEYS` += CUSUM K/H for per-tenant tuning.

### Phase 4 — Pipeline + live integration
**Agent:** backend-engineer (claude-sonnet-4-6) · **Writes:** `src/api/`, `src/db/` (touchpoints in `comprehensive_pipeline`, live wiring via existing DI seams) · **Depends on:** Phase 3
- Wire `CropBoundsDetector` + `SeasonalScaleProvider` into
  `ComprehensiveInferencePipeline` and the live enrichment path (`EnrichmentRunner`
  / `DetectorResolver` DI seams — no `LiveScorer` contract change).
- VPD derivation reuse in the batch path for VPD bound checks.
- Cache invalidation on config mutations affecting scoring.

### Phase 5 — Seasonal mapping + crop profile UI
**Agent:** frontend-engineer (claude-sonnet-4-6) · **Writes:** `web/` (per `web/AGENTS.md`; note fe worktree layout in memory) · **Depends on:** Phase 1 (API contracts frozen); can start once Phase 1's done signal lands
- Month→season mapping editor (12-month grid, season labels, unmapped-month warning).
- Crop profile selector/editor (tomato/cucumber presets from API, custom bounds,
  day/night periods); baseline viewer (mean/std/median per season).
- Migrate `useSetpoints` from localStorage to `/api/config/effective-setpoints`
  (TanStack Query) with localStorage as offline fallback only; `SetpointsPanel`
  gains crop/season context. Zod validation at the boundary (existing pattern).

### Phase 6 — QA
**Agent:** qa-engineer (claude-opus-4-6, per user directive) · **Writes:** `tests/`, `docs/coverage/` · **Depends on:** Phases 2–5
- 90 % coverage target on new modules; psychrometric fixtures asserting the
  corrected 10× scale; MAD-scrub unit tests (known Iglewicz–Hoaglin vectors);
  seasonal fallback behavior; API CRUD + tenant isolation; migration 018 up/down;
  regression: full suite (baseline 2516 passed) zero regressions.

### Phase 7 — Final review
**Agents:** code-reviewer (claude-opus-4-6) + tech-lead & architect co-review
(claude-fable-5, per user directive) · **Writes:** `docs/review/`, `docs/refactor/`
- Guardrails: no LSTM↔API coupling, no magic numbers, coverage ≥80 % hard floor.

**Dependency graph:** `(P1 ∥ P2) → P3 → P4 → P6 → P7`, with `P5` starting after P1
and merging before P6.

---

## 5. Open product questions (need user decisions)

**Q1 — Measured HD source (blocks the venting-downgrade sub-feature only).**
No HD channel exists in the data model (`SensorReading` = temp/humidity/co2/light;
no connector or CSV alias maps one). The spec's derived-vs-**measured** HD
comparison is impossible today. Options: (a) ingest a measured-HD channel via
`RawReading.data` JSON + CSV/connector column aliases (Priva/Hoogendoorn expose
HD/"vochtdeficit"); (b) approximate venting detection from vent-position or
plant-temp columns (`SetpointAwareScadaDetector` already parses some); (c) defer
the downgrade layer. Recommendation: (a), added to Phase 1 ingestion aliases.

**Q2 — Crop profile granularity.** Per tenant, per greenhouse, or both (tenant
default + greenhouse override, as the schema in §3.1 assumes)? Do crop rotations
need effective-date ranges, or is "one active profile, operator switches it" enough?

**Q3 — Day/night definition for day-temp bounds.** Fixed clock hours (config
default 06–22), light-channel threshold, or sunrise/sunset by site coordinates?
Schema supports `period ∈ {day, night, any}` regardless.

**Q4 — Dropout semantics.** Spec says **all-missing** window P; the shipped
`DropoutDetector` fires on **any** sensor missing. Keep both (per-sensor dropout
= sensor fault; all-missing = polling/comms fault) or align to spec strictly?

**Q5 — Season mapping scope & unmapped months.** Per tenant or per greenhouse?
When a month is unmapped: no seasonal scaling (static fallback — recommended) or
block detector config until fully mapped?

**Q6 — Fate of the localStorage setpoints overlay.** Migrate it into crop profiles
(server-persisted, one source of truth — recommended) or keep it as a separate
personal display layer on top of crop bounds?

**Q7 — CUSUM σ source.** Replace the EMA rolling σ with seasonal σ entirely, or
use seasonal σ as a floor/prior with EMA adapting on top (recommended: floor)?

**Q8 — Tolerance default.** Architect band is 0.3–0.5 g/m³; plan assumes 0.4.
Confirm, and whether it should be per-tenant tunable via `CustomerModelConfig`
(recommended: yes).

**Q9 — LSTM-VAE vs spec's LSTM-AE.** Pass 2 uses the existing LSTM-VAE; the τ
threshold is already computed on pure MSE reconstruction error (spec-conformant).
Keep the VAE (recommended — shared architecture, no retraining-path fork) or add
a plain-AE variant for spec fidelity?

---

## 6. Explicitly out of scope

- Retraining/online-learning changes (Gap 3 pipeline untouched).
- Any change to live SSE transport, hub, or persistence contracts.
- Network anomaly / gas detection modules.
- Multi-worker scope suppression (documented single-worker invariant stands).
