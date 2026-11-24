# ADR-014: One Scoring Path for Upload, Live and the Inference API

Status: **Proposed**
Date: 2026-11-24
Plan item: D1 (Whiteboard "Structural"); handover `docs/handoff/handover-2026-11-15.md` §0 item 8, §14
Scope: `src/api/services/comprehensive_pipeline.py`, `upload_portions.py`, `cold_run_upload.py`,
`fault_classification.py`, `sample_enrichment.py`, `drift_watch_service.py`, `src/workers/upload_consumer.py`,
`src/live/{scoring,enrichment,attribution,ring_seed,runtime,persistence}.py`,
`src/api/routes/{inference,inference_integrated}.py`, `src/models/*` detectors, `src/config.py`

## Problem

The same sensor window gets a different verdict depending on how it enters the system. There are three
entry points and they share less code than their names suggest:

- **Upload** (sync `/api/inference/upload` and the async worker) never calls
  `ComprehensiveInferencePipeline`. It runs its own stack: `score_portion` / `finalize_scores`
  (`upload_portions.py:764-1023`) with file-adaptive normalisation, a per-file LSTM threshold, rolling-z,
  wall-clock stuck, upload MAD and OOB-with-clamp. Labels are derived at **read time** from
  `trigger_detector` (`fault_classification.py:148-158`).
- **Live** scores every tick with its own LSTM forward pass (`attribution.py:28-50`) and a fixed threshold.
  Only when that LSTM score crosses `LIVE_ANOMALY_THRESHOLD` does it build a **new** pipeline and run the
  rule detectors once, asynchronously, without a timestamp (`enrichment.py:118-133`).
- **Inference API** has two routes. `/api/inference` is LSTM-only with `ANOMALY_THRESHOLD`
  (`inference.py:57-183`). `/api/inference/comprehensive` and `/batch` run the full pipeline; `/batch` passes
  no timestamp (`inference_integrated.py:178-185`, `comprehensive_pipeline.py:947-981`).

Defects already traced to this split: #123 (physics check on wall clock), #122 (live ring-seed race), #130
(upload-only LSTM gate), upload MAD horizon 3 vs live lag 1, diurnal/CUSUM caches per path, drift watch on
upload only. The table below shows that these are symptoms of one cause, and that more remain.

## 1. Step x path inventory (`main` at `2ee7bfb`)

Key: **S** same code, **D** different code, **-** absent. Verdict: **I** intentional, **A** accidental,
**?** needs a decision. File paths are relative to `src/`; `cp` = `api/services/comprehensive_pipeline.py`,
`up` = `api/services/upload_portions.py`, `cru` = `api/services/cold_run_upload.py`.

| # | Step | Upload (sync + async) | Live | Inference API | Verdict |
|---|---|---|---|---|---|
| 1 | Normalisation | D: file-adaptive ranges from head sample, `cru:201-249`, `up:848-849` | D: fixed `SENSOR_RANGES`, `live/scoring.py:179`; re-normalised in `enrichment.py:116` | D: caller sends normalised window; pipeline denormalises with config ranges, `cp:599-603,1048` | **?** adaptive was added for real CSVs; live/API never got it; the checkpoint does not record its training ranges |
| 2 | Out-of-bounds | D: per-row hit, clamp, keep scoring, `up:290-376,792-798` | D: pipeline gate returns early, `cp:1160-1177`, only if LSTM fired | D: same gate, `cp:1160-1177` | **I** (batch keeps other rows) for clamp; **A** that live checks OOB only behind the LSTM |
| 3 | Dropout | - (adapter drops NaN rows) | - (forward-fill + staleness, `scoring.py:256-281`) | S: `cp:1017-1036` | **I** belongs to the I/O adapter |
| 4 | LSTM forward | S: `predict_batch_enriched`, `up:851-854` | D: own MSE copy `attribution.py:28-50`; enrichment runs a 2nd pass `cp:1376` | S: `models/inference.py:99-151` | **A** duplicate maths |
| 5 | LSTM decision | D: per-file P99.9 + robust gate, `up:929`, `config.py:240-266` | D: fixed 0.05, `scoring.py:296`, `config.py:1806` | D: `/inference` fixed 0.8 (`inference.py:97`); `/comprehensive` max(EWMA, POT), `cp:1460-1468,2186` | **?** three policies for one model |
| 6 | Setpoint / climate rules | - | D: runs, but `climate_context` is never passed (`enrichment.py:127-133`) | S: `cp:1053-1057` | live: **I** (P2.5 blocked on data); upload: **A** (CSV has setpoint/plant/outdoor columns) |
| 7 | Band breach + promotion | - | S (behind LSTM) `cp:1099-1120,1557-1572` | S | **A** upload never sees bands |
| 8 | Noise | - | D: ring-calibrated baseline, `scoring.py:319`, `cp:1132` | D: fixed baseline only | calibration source **I**; upload absence **A** |
| 9 | MAD spike | D: every row, look-ahead horizon 3, temperature only, no seasonal scaling, `up:152-231`, `config.py:1331-1337` | D: one vote at t-1 (lag 1), temperature + humidity pairs, seasonal-scaled floor, `cp:1192-1232`, `config.py:1390-1401` | same as live | horizon **I** (batch can look ahead); humidity **I** (B2 measured); seasonal scaling on one path **A** |
| 10 | Bias EMA | - | dead: needs 288 rows, window is 20, `cp:1242` | dead | effectively S (absent) |
| 11 | Dual-sensor bias / correlation | - | - | S: only with `sensor_pairs`, `cp:1261-1283` | **?** upload has paired columns |
| 12 | Diurnal profile | - | D: wall-clock anchor (no timestamp); new pipeline per episode, so cold cache every time | D: `/comprehensive` anchored to timestamp; `/batch` wall clock; cache `(tenant, gh, date)` `cp:1986-1987` | **A** |
| 13 | BOCPD change-point | - | S (behind LSTM) `cp:1359-1371` | S | **A** upload level-shift gap (§14 +3 °C hold) |
| 14 | Mahalanobis | non-voting | non-voting | non-voting | S |
| 15 | CUSUM / EMA | - | D: restored per episode, updated only on LSTM-flagged windows, `cp:1388-1419` | D: updated every call | **A** (non-voting today, so no label impact) |
| 16 | Physics / psychrometric | D: stat-pair suppression with `measured_hd`, `up:508-579` | D: `PhysicsValidator` on wall clock (no timestamp), `cp:999-1007,1425-1427` | D: `/comprehensive` timestamp; `/batch` wall clock | **A**: #123 is fixed only where a timestamp is passed |
| 17 | Rolling-z (288, all raw columns) | S: `up:379-450` | - | - | **?** needs history; stream stage candidate |
| 18 | Stuck, wall-clock 6 h | S: `up:804-806`, `StuckCarry` | - (only 20-sample setpoint flatline, behind LSTM) | - (flatline only) | **A** live cannot see a long freeze |
| 19 | Ensemble | - | non-voting | non-voting | harmless |
| 20 | Vote / precedence | D: none; per-timestamp `anomaly_rank_key` trigger (`cru:128-166`), LSTM+stat merge (`cru:399-456`) | D: `_RULE_PRECEDENCE` + profiles, band last, `cp:168-182,1505-1572` | same as live | **A** two precedence models |
| 21 | Labelling | D: read-time `trigger_detector` map, lstm/statistical -> `unknown`, `fault_classification.py:148-158`, `sample_enrichment.py:213-276` | D: tick event has attribution only (`scoring.py:361-394`); `fault_type` arrives later on `anomaly-detail` (`enrichment.py:143-156`) | D: `fault_type` in response | **A** |
| 22 | Episode grouping | D: `group_alerts_into_episodes` `up:997-1001` + persisted stuck episodes (`anomaly_episode_writer.py`) | D: in-memory LSTM episode, uuid, monotonic debounce, `scoring.py:287-339`, not persisted | - | **A** |
| 23 | Persistence | sparse canonical scores `score_scale=upload`, trigger columns, episodes; no `Incident` (`cru:312-371`, `upload_consumer.py:540-568`) | every scored row `score_scale=live`, `ON CONFLICT DO NOTHING` (`persistence.py:116-169,380-393`); one `Incident` per episode | `/inference` opt-in `score_scale=batch` (`inference.py:109-169`); `/comprehensive` `Incident`, `LearningState`, online learner (`cp:1604-1611,1706-1718`) | **I** per I/O; ADR-005 scales stay |
| 24 | Drift watch | S: on-upload hook, sync `cru:374-380`, async `upload_consumer.py:570-580` | - | - | **A** a live-only scope gets drift episodes only from the manual backfill |
| 25 | Caches / state | per-upload carries only | new pipeline per episode, all caches cold (`enrichment.py:124`) | one cached pipeline, `MAX_PIPELINE_CACHE_SIZE=1` keyed by checkpoint (`inference_integrated.py:41-59`, `config.py:325`); EWMA/POT cache keyed by **tenant only** (`cp:2407`); `LearningState` per tenant, last greenhouse wins (`cp:2441-2456`) | **A** |
| 26 | Clock | data timestamps | wall clock inside pipeline; monotonic for staleness/debounce (adapter, fine) | `/inference` `high_anomalies[].timestamp = now()` (`inference.py:99`); `/batch` wall clock | **A** |

**Summary.** Of 26 steps, only Mahalanobis, the ensemble and the dead bias-EMA agree on all three paths,
and they agree only because none of them reaches a label. The intentional differences are: the batch look-ahead horizon,
the humidity spike exclusion on upload, OOB clamp-and-continue, dropout in the adapter, persistence shape, and
live climate rules (blocked on data). Everything else is accidental, or it is a policy nobody chose (rows 1, 5,
11, 17).

The two accidental differences with the largest effect:

1. **Live gates every rule detector behind the LSTM** (rows 2, 6-9, 12-13, 18). On upload, #130 decided that
   the LSTM gives scores and not alerts. Live does the opposite: a spike, freeze or band breach that the LSTM
   scores under 0.05 is never checked. This is #130 inverted.
2. **The clock is still wall-clock on live and `/batch`** (rows 12, 16, 26). #123 fixed the physics check only
   for callers that pass `sequence_timestamp`. Live enrichment and `run_batch` do not pass it, so day/night
   bands, diurnal hour, season and the nocturnal-light check read `datetime.now()`.

## 2. Decision

Build **one scoring core** and make each path a **thin adapter**. Where a difference is intentional, it
becomes a **named field of a scoring profile**, not a code fork.

### 2.1 The core

A pure, synchronous library with no DB, no clock, no network and no `src/api` / `src/db` / `src/live`
import (this enforces the "tight coupling" STOP-WORK guardrail):

```
ScoringCore.score(rows, carry, ctx, history, model) -> (ScoringResult, carry')
```

- `rows`: physical-unit samples (°C, %RH, ppm, W/m²) with their **own timestamps**, plus optional extra
  raw columns. The core normalises only inside `model`, using the checkpoint's recorded ranges (closes
  BACKLOG-23 as a side effect: physical rules never see normalised data).
- `carry`: explicit stream state (spike reversion tail, `StuckCarry`, rolling-z tail, CUSUM/EMA,
  EWMA/POT). The adapter holds and persists it; the core returns the next value. Live passes one row per
  call, upload passes a portion, the API passes one window with an empty carry.
- `ctx`: `ScoringContext(tenant_id, greenhouse_id, profile, climate, sensor_pairs)`. There is no "now":
  the clock is the timestamp of each row, and a row with no timestamp is rejected.
- `history`: a `HistorySnapshot` the adapter prefetches (diurnal profiles for the anchor date, noise clean
  windows, operational config and overrides, seasonal scaler). The core never queries.
- `model`: a `ModelScorer` protocol over `AnomalyDetector` that returns score, per-channel error,
  embedding and uncertainty from one forward pass (removes row 4's duplicate).
- `ScoringResult`: per timestamp `{is_anomaly, fault_type, rule_votes, affected_sensors, severity,
  lstm_score, detector_hits}`, plus episode open/close events. One precedence table (today's
  `_RULE_PRECEDENCE`, extended with `stuck_fault`, `out_of_bounds`, `statistical`) and one label taxonomy
  replace rows 20-21. Read-time mapping in `fault_classification.py` stays only as the legacy-row fallback.

The expensive **explain** stage (attribution, pattern, root cause, recommendations, `Incident`, online
learner) is a second core function, `explain(result, window)`. It runs only for flagged timestamps, and
the profile decides when (each flag, or episode start only).

### 2.2 Adapters

| Adapter | Owns | Must not do |
|---|---|---|
| Upload (`up`, `cru`, `upload_consumer`) | CSV parse, portions, MQ, carry hand-off, two-phase finalise, row/score/episode persistence, drift-watch hook | detector logic, labels |
| Live (`live/scoring.py`) | forward-fill, staleness and debounce (monotonic is fine here), one long-lived carry per scope, ring seed as the noise history source, hub publish, batcher | build a new pipeline per episode, gate rules on the LSTM |
| API (`inference*.py`) | auth, request parsing, optional persistence | read the wall clock for scoring |

### 2.3 Profiles

`ScoringProfile` is a frozen dataclass in `src/config.py` built only from existing constants. There are three
instances: `BATCH_PROFILE` (upload), `STREAM_PROFILE` (live), `API_PROFILE`. Every intentional difference is a
field:

| Field | Batch | Stream / API | Source today |
|---|---|---|---|
| `spike_confirm` | look-ahead, horizon 3 | lag 1 | `UPLOAD_SPIKE_REVERT_HORIZON`, `ENRICHMENT_SPIKE_CONFIRM_LAG` |
| `spike_pairs` | temperature | temperature, humidity | `UPLOAD_SPIKE_CONFIRMED_*`, `ENRICHMENT_SPIKE_CONFIRMED_PAIRS` |
| `lstm_decision` | `file_percentile_robust` (two-phase) | `fixed(0.05)` / `adaptive_ewma_pot` | row 5, pending OQ2 |
| `oob_policy` | `hit_and_clamp` | `gate` | row 2 |
| `explain_policy` | none | `episode_start` / `every_call` | row 23 |

During migration, each accidental difference also becomes a temporary field named `legacy_*` (for example
`legacy_rules_behind_lstm`, `legacy_wallclock`, `legacy_tracker_key_tenant_only`) that holds today's value.
Each convergence PR flips one `legacy_*` field and deletes it. When none remain, the core has no path forks.

## 3. Invariants (tests, owned by qa-engineer)

1. **Golden-window parity.** A fixed corpus (`native_july_clearsky.csv` windows, the accuracy-gate
   injections at their voted index, `native_dec_stuck_fault.csv` slices, an OOB row, a night band breach)
   is scored through all three adapters **with the same profile**. Labels, `rule_votes` and
   `affected_sensors` must be identical. A second test scores it with the three production profiles and
   asserts that every difference is explained by a profile field. Known divergences are
   `xfail(strict=True)` with a row id from §1, so a characterisation test never makes a bug look correct.
2. **No wall clock in scoring.** A test fails if code under the core package calls `datetime.now`,
   `datetime.utcnow`, `time.time` or `time.monotonic` (AST scan plus a monkeypatch that raises). It also
   scores a historical window (2026-07-02 02:00) on a later machine date and asserts night semantics.
3. **Cache and state keys are `(tenant_id, greenhouse_id)`.** Two greenhouses of one tenant with different
   histories give different verdicts, with no bleed in diurnal, CUSUM/EMA, EWMA/POT or seasonal state.
   Evicting one key does not touch the other.
4. **Determinism and replay.** The same rows and carry give the same result twice. Scoring a file in one
   portion or in N portions (any order, async path) gives the same labels.
5. **Units at the boundary.** The core accepts only physical units. A normalised input fails validation.
6. **Full suite and accuracy gate on every step:** `./venv/bin/python -m pytest tests/ -q`, empty-DB and
   seeded composites. Report measured values, never targets.

## 4. Migration (small PRs that can merge one at a time)

Phase A **moves code without changing behaviour**: on every PR, the parity corpus and the accuracy gate give
byte-identical output to the PR before it. Phase B **changes behaviour**: one accidental difference per PR,
with before/after measurements.

| Step | Owner | Change | Done when |
|---|---|---|---|
| A0 | architect | Accept this ADR; resolve OQ1 (core location and owner) | user decision |
| A1 | qa-engineer | Golden corpus + parity harness, recording today's per-path labels; `xfail(strict)` per §1 row | harness green on `main` |
| A2 | ml-engineer | `ScoringContext`, `ScoringProfile`, three profiles and `legacy_*` fields in `src/config.py`; core types with no callers | no output change |
| A3 | ml-engineer | `ModelScorer.predict_with_channel_errors` in `models/inference.py`; core detector-vote functions copied out of `cp:1009-1373` as pure code, with unit tests that compare against the pipeline on the corpus | equality on the corpus |
| A4 | backend-engineer | `cp._run_with_components` calls the core votes; inline copies deleted; precedence and profiles move with them (OQ1) | parity unchanged |
| A5 | backend-engineer | `HistoryProvider` adapter (`api/services/scoring_history.py`) takes over the diurnal, overrides, seasonal and `LearningState` queries from `cp`; keys kept as today through `legacy_tracker_key_tenant_only` | parity unchanged |
| A6 | backend-engineer | Live: `scoring.py` uses `ModelScorer` (drops `attribution.py`'s MSE copy); enrichment calls the core through the adapter | parity unchanged |
| A7 | ml-engineer + backend-engineer | Upload stream stages (MAD reversion, wall-clock stuck, rolling-z, OOB clamp) become core stages with carries; `score_portion` / `finalize_scores` become the upload adapter; async carry hand-off unchanged | sync == async == before |
| A8 | backend-engineer | Upload labels come from the core table at write time; read-time map kept for legacy rows | API responses unchanged |
| B1 | backend-engineer | Clock: live enrichment and `/batch` pass row timestamps; core rejects a missing timestamp (rows 12, 16, 26) | night-window test passes; gate measured |
| B2 | backend-engineer (+ ml for thresholds) | Live: one long-lived carry/state per scope; no pipeline per episode (rows 15, 25) | CUSUM continuity test |
| B3 | backend-engineer | EWMA/POT and `LearningState` keyed per `(tenant, gh)` inside `state_json` (row 25); no schema migration | invariant 3 green |
| B4 | backend-engineer | Live runs core rules on every tick; LSTM no longer gates them (rows 2, 6-9, 12-13, 18); explain only on flag | live clean-flag rate measured on mock + replayed July |
| B5 | ml-engineer | Checkpoint stores normalisation ranges; every path uses them (row 1) | parity: one LSTM score per window |
| B6 | backend-engineer | Drift watch for live scopes: weekly/hourly job in `src/workers/` (single consumer), same `_run_shadow`, idempotent natural keys (row 24) | episodes match backfill on the same data |
| B7 | ml-engineer, then backend-engineer | Upload gains window rules (noise, BOCPD, diurnal, band, setpoint) as stages, **each behind a measured decision** (rows 6-8, 12-13); may stay off | accuracy gate + tenant-year clean hits reported |
| B8 | ml-engineer | Seasonal scaling decided once for MAD floors on both paths (row 9) | measured |
| B9 | ml-engineer + user | Unify the LSTM decision (row 5) per OQ2 | product decision |

Constraints that apply to every step:

- **Single replica** (`CLAUDE.md`, ADR-009 §0.3). Live carries and the `_suppressed_scopes` set are
  per-process. B2 and B4 put more state in that process, which deepens the single-replica dependency. B6
  must not run in the API process if the API is ever scaled: put it in `src/workers/` with natural-key
  upserts.
- **`src/live` scoring ends in `src/models/inference.py`**, which belongs to ml-engineer. A6 and B5 need both
  agents, and live constants go in `src/config.py` through ml-engineer only.
- Every agent report ends with the raw `git status --short`.

## 5. Risks

- **Live CPU (B4).** All rule votes on every 2 s tick per scope. The votes are small numpy operations on 20
  rows and the DB lookups move to a prefetched snapshot, but measure before merging.
- **Upload cost (B7).** Window rules on stride-1 windows of a 105k-row year. They must be vectorised stream
  stages and must not loop the per-window pipeline.
- **Alert-rate shifts in Phase B.** B4 and B7 add detections on paths that never had them. Each needs its own
  clean-flag measurement and, where it alerts operators, the same budget review as C3.
- **Characterisation tests that pin bugs.** Use `xfail(strict)` with a row id, never a plain expected value.
- **Merge conflicts.** `comprehensive_pipeline.py` is 2,500 lines and other detector work is active. Keep
  Phase A PRs small and rebase often, or freeze detector edits to `cp` while A3-A5 are in flight.
- **Ownership drift.** The core mixes detector maths (ml) and precedence/labels (backend). OQ1 must be
  settled before A3.

## 6. Rejected alternatives

- **Big-bang rewrite** of all three paths into a new service. It cannot keep behaviour identical step by
  step, it blocks detector work for weeks, and it gives no point where parity can be measured.
- **Upload calls `ComprehensiveInferencePipeline` per window.** About 105k calls per year file, each with a
  deep-copied model attribution and DB lookups. It also cannot express batch look-ahead or the whole-file
  LSTM calibration.
- **Live reuses the upload stack.** `finalize_scores` needs the whole file (P99.9, stuck flush), which a
  stream never has.
- **Keep the forks and only add cross-path tests.** It detects drift but does not remove the cause. Every new
  detector would still be written two or three times.
- **Config flags inside each path.** This is today's state (`UPLOAD_*` vs `ENRICHMENT_*`). It is what
  produced the horizon 3 / lag 1 surprise.

## 7. Open questions

- **OQ1 Core location and owner.** Option (a): `src/models/scoring/` (ml-engineer), with the precedence and
  label table moving out of backend-owned `cp`. Option (b): a new `src/scoring/` tree, which needs an owner
  row in `CLAUDE.md` first. Recommendation: (a), with backend-engineer as required reviewer of the
  precedence table.
- **OQ2 Is the LSTM an alert source?** Upload says no (#130, robust gate gives 0 alerts). Live alerts on it
  at 0.05. `/comprehensive` alerts above max(EWMA, POT) or 0.8. This is a product decision.
- **OQ3 Training ranges.** Which normalisation was each deployed checkpoint trained with (adaptive from its
  upload, or fixed)? If adaptive, live and the API feed the model differently scaled input today.
- **OQ4 Live score scale.** `config.py:1800-1802` says live scores are raw reconstruction error, but
  `forward_pass_sync` returns `raw / ANOMALY_SCALING_FACTOR` (`attribution.py:49`), and `persistence.py:136`
  stores that value as `anomaly_score_raw`. Confirm which one the 0.05 calibration was measured on.
- **OQ5 Persisted upload labels.** Should upload write `fault_type` on `sensor_readings`, or `Incident`
  rows, so read-time inference can retire?
- **OQ6 `/api/inference`.** Keep it as a model-score-only endpoint (documented as such), or route it
  through the core with `API_PROFILE`?
- **OQ7 Live alert policy after B4.** Which live rule votes may alert, and at what rate budget?
