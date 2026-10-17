# Upload Path ↔ Comprehensive Pipeline Detector Convergence

**Date:** 2026-09-09
**Author:** Architect agent
**Status:** DESIGN — no implementation code written

---

## Problem Statement

The CSV upload path runs 3 detectors (LSTM-VAE, `ColdRunStatisticalDetector`,
`WallclockStuckDetector`). The `ComprehensiveInferencePipeline` wires 18
detectors and is reachable only from the live SSE enrichment path and the
single-sequence `/api/inference/comprehensive` endpoint. The two stacks
can diverge silently: a fault visible on the live path may be invisible on
upload, and vice versa.

The question is whether and how to bring the comprehensive detector set to
the upload path without violating the upload path's design invariants:
bounded memory (5,000-row chunks with rolling carry), streaming throughput,
and the 173 tests that pin current alert values.

## Current Architecture

### Upload path (`score_portion` / `finalize_scores`)

```
CSV file
  │
  ├─ chunk (5 000 rows)
  │     ├─ WallclockStuckDetector.detect_chunk(df, StuckCarry)  → stuck alerts
  │     ├─ ColdRunStatisticalDetector.detect_rolling(rows, columns)  → stat hits
  │     └─ AnomalyDetector.predict_batch_enriched(sequences)    → LSTM scores
  │     carry: StatCarry + StuckCarry threaded to next chunk
  │
  ├─ ... repeat per chunk ...
  │
  └─ finalize_scores()
        ├─ flush open stuck faults
        ├─ calibrate_threshold (P99.9 of LSTM scores)
        ├─ merge LSTM + stat + stuck into high_anomalies
        ├─ group_alerts_into_episodes()
        └─ build UploadInferenceResponse
```

**Key properties:**
- Memory bounded per chunk — no full-file accumulation of raw data
- Statistical baseline threaded via `StatCarry.rows` (last `ROLLING_WINDOW` rows)
- Stuck state threaded via `StuckCarry` (one `ChannelFreezeState` per channel)
- Threshold calibrated over the full file's LSTM score distribution
- Episode-grouping runs once in `finalize_scores`; no recovery computation here
  (`is_recovery` is tagged query-time, in `anomaly_episode_service.py`)

### Comprehensive pipeline (`_run_with_components`)

```
One sequence (20×4)
  │
  ├─ 0.  DropoutDetector (short-circuit)
  ├─ 0a. NoiseDetector (per-sensor rolling variance)
  ├─ 0a+.SetpointAwareScadaDetector (climate rules)
  ├─ 0a++.BandBreachDetector (operator crop bands)
  ├─ 0b. OutOfBoundsDetector (hard physical limits, short-circuit)
  ├─ 0c. SpikeMADDetector (transient MAD outliers)
  ├─ 0d. BiasEMADetector (persistent offset)
  ├─ 0e. BiasDualSensorDetector (redundant-pair cross-check)
  ├─ 0e. CorrelationBreakdownDetector (Pearson floor)
  ├─ 0f. DiurnalProfileDetector (hourly Z-score, DB-backed profiles)
  ├─ 0g. MultivariateMahalanobisDetector (joint implausibility)
  ├─ 0h. BOCPDDetector (Bayesian change-point)
  │
  ├─ 1.  LSTM-VAE forward pass
  ├─ 1a. CUSUMDriftDetector + EMABaselineEstimator (per-tenant, stateful)
  │
  ├─ 2.  PhysicsValidator (may suppress)
  ├─ 3.  AnomalyAttributor + PatternDetector
  ├─ 4.  EWMA/POT adaptive threshold (per-tenant, DB-persisted)
  ├─ 5.  EnsembleAnomalyDetector (LSTM+IF+Statistical+Correlation)
  │       └─ fault-type fusion (rules > physics > ensemble > band)
  ├─ 5d. Psychrometric consistency suppression
  │
  ├─ 6.  EWMA/POT tracker persistence
  ├─ 7.  ConfidenceScorer
  ├─ 8.  RootCauseAnalyzer + RecommendationEngine
  ├─ 9.  IncidentMemory.store_incident + OnlineLearner.observe
  └─ PipelineResult (30+ fields)
```

**Key properties:**
- Designed for one sequence at a time (live path) or sequential `run_batch`
- Per-tenant stateful: CUSUM/EMA accumulators, EWMA/POT trackers, diurnal
  profile caches, online learner — all persisted in `LearningState` DB row
- DB-intensive: `IncidentMemory.store_incident` per anomaly, DB queries for
  diurnal profiles, seasonal scalers, EWMA/POT load/save
- `run_batch` exists but is a sequential `for` loop calling `_run_with_components`

---

## Question 1: Which detectors belong on the batch/backfill path?

### Classification of all 18 comprehensive-pipeline detectors

| # | Detector | Stateful? | Per-sequence? | Batch-safe? | Verdict | Reason |
|---|----------|-----------|---------------|-------------|---------|--------|
| 1 | **DropoutDetector** | No | Yes | Yes | **IN** | Stateless; checks if >threshold% of rows are null. Trivially batchable. |
| 2 | **NoiseDetector** | No (config only) | Yes (per-sensor on 20-step window) | Yes | **BLOCKED** | Rolling variance vs baseline — computed on a single sequence. Stateless. **Blocked pending recalibration:** fires on 69.6% of healthy clear-sky windows (1389/1997, measured 2026-09-10). See false-positive findings below. |
| 3 | **SetpointAwareScadaDetector** | No | Yes | Yes | **OUT** (deferred) | Requires `climate_context` (t_setpoint, t_plant, t_outdoor, i_solar) which upload CSVs do not carry. Could be added when CSVs include setpoint columns, but that is a data-model extension, not a detector-wiring problem. |
| 4 | **BandBreachDetector** | No | Yes | Yes | **IN** (if OperationalConfig exists) | Requires per-tenant `OperationalConfig` (day/night crop bands). Stateless per-sequence. Upload path already has `tenant_id` + `greenhouse_id`. Falls through gracefully when no config exists. |
| 5 | **OutOfBoundsDetector** | No | Yes | Yes | **IN** | Hard physical limits from `OOB_LIMITS`. Stateless. |
| 6 | **SpikeMADDetector** | No | Yes | Yes | **IN** (investigate) | Rolling MAD within one 20-step window. Stateless. **0% firing rate on both clean and faulted fixtures (measured 2026-09-10). Quiet is not the same as correct — needs investigation to confirm it is not miscalibrated for this cadence.** |
| 7 | **BiasEMADetector** | No | Yes (static method) | Yes | **IN** | EMA offset detection within one window. Stateless. |
| 8 | **BiasDualSensorDetector** | No | Yes | Conditional | **OUT** | Requires `sensor_pairs` — redundant sensor pair data not in upload CSVs. No data to run against. |
| 9 | **CorrelationBreakdownDetector** | No | Yes | Conditional | **OUT** | Same reason as BiasDualSensor — requires redundant sensor pairs. |
| 10 | **DiurnalProfileDetector** | No (reads DB) | Yes | Yes (read-only) | **DEFERRED** | Requires pre-built per-tenant diurnal profiles from DB. Read-only at detection time. Would add one DB query per upload (profile fetch), not per sequence. Valuable but adds a DB dependency to the currently DB-free scoring loop. |
| 11 | **MultivariateMahalanobisDetector** | No | Yes | Yes | **BLOCKED** | Rolling Mahalanobis on one 20-step window. Stateless. **Blocked pending recalibration:** fires 38.3% on healthy data (764/1997) vs 9.5% on real fault (319/3368) — the signal is inverted, not merely noisy (measured 2026-09-10). See false-positive findings below. |
| 12 | **BOCPDDetector** | No (internal state per call) | Yes | Yes | **IN** (with caveat) | BOCPD run-length computation is self-contained per call. However, it is designed for level-shift detection across sequences — on a single 20-step window it has very limited statistical power. See Question 2 for reformulation option. |
| 13 | **LSTM-VAE** | No | Yes (batch) | Yes | **ALREADY IN** | Upload path already runs this via `predict_batch_enriched`. |
| 14 | **CUSUMDriftDetector** | **Yes** (per-tenant, cumulative sums) | No (one update per sequence) | **Needs reformulation** | **OUT** (phase 1) | CUSUM accumulates `s_pos`/`s_neg` per sensor across the tenant's entire history. On upload, 105k sequential updates would build a CUSUM trace but without the interleaving of normal live readings. The CUSUM would alarm on any sustained anomaly, which is correct but would fire thousands of alarms on the stuck-fault window that the stuck detector already handles better. Could be run as a post-pass summary in a later phase. |
| 15 | **EMABaselineEstimator** | **Yes** (per-tenant, running mean/var) | No | **Needs reformulation** | **OUT** (phase 1) | Same issue as CUSUM — running mean/variance of sensor values accumulates across the tenant's lifetime. A bulk upload would massively shift the estimator. If the upload is historical backfill, the EMA should not be contaminated by it. If it is a new tenant's first data, a cold-start EMA could be computed per-file as a separate pass. |
| 16 | **EWMATracker + POTThresholdFitter** | **Yes** (per-tenant, DB-persisted) | No | **Needs reformulation** | **OUT** | Adaptive anomaly threshold that learns from the tenant's score history. Bulk-feeding 105k scores in one pass would over-adapt the threshold. Must be excluded from upload-time detection; the tracker should only observe final upload-calibrated scores if we decide to feed them at all (separate decision). |
| 17 | **EnsembleAnomalyDetector** | No (fits on baseline per call) | Yes | Yes | **IN** | `IsolationForest.fit` on baseline is cached; per-sequence scoring is stateless after fit. The fit happens once on the default or tenant baseline. |
| 18 | **StuckFaultDetector** (sample-indexed) | No | Yes | N/A | **ALREADY SUPERSEDED** | The upload path uses `WallclockStuckDetector` which is superior for variable-cadence batch data. The sample-indexed detector is correctly used only on the live path (fixed 20-step windows). No convergence action needed. |

### Additional upload-path detectors not in comprehensive pipeline

| Detector | Verdict | Reason |
|----------|---------|--------|
| **ColdRunStatisticalDetector** (rolling z-score) | **KEEP** | File-level rolling baseline with cross-chunk carry. Not in the comprehensive pipeline, which uses ensemble statistical detection on per-sequence Mahalanobis instead. Both contribute distinct value: rolling z-score catches local deviations against a growing baseline; ensemble statistical catches deviations against a fitted distribution. |
| **WallclockStuckDetector** | **KEEP** | Wall-clock gate is correct for batch; the comprehensive pipeline's sample-indexed `StuckFaultDetector` is not. |

### Summary: detectors IN for Phase 1

Stateless, per-sequence detectors that can run on the upload path without
any state management or DB interaction beyond what already exists:

1. DropoutDetector
2. OutOfBoundsDetector
3. SpikeMADDetector (0% on both fixtures — investigate before trusting)
4. BiasEMADetector (static method — per-window)
5. BOCPDDetector (per-window, limited power — see Q2)
6. EnsembleAnomalyDetector (fit once on baseline, score per-sequence —
   **but 1.05 ms/seq = 111 s/year exceeds the 60 s budget; must be
   vectorised or deferred before Phase 1 admits it**)

**BLOCKED pending recalibration (measured 2026-09-10):**
- NoiseDetector — 69.6% false positive rate on healthy clear-sky data
  (1389/1997 windows). Must be recalibrated against both fixtures before
  Phase 1 admits it.
- MultivariateMahalanobisDetector — inverted signal: 38.3% on healthy
  data vs 9.5% on the real stuck fault. Must be recalibrated before
  Phase 1 admits it. A plausible contributor is the synthetic-default
  ensemble baseline — see ADR-008.

**Conditional for Phase 2:**
- BandBreachDetector (requires OperationalConfig — add when config
  is reliably provisioned for upload tenants)
- DiurnalProfileDetector (requires DB-backed profiles — add when
  the scoring loop can accept a pre-fetched profile dict)

**Out:**
- SetpointAwareScadaDetector (needs climate-context data not in CSVs)
- BiasDualSensorDetector (needs redundant sensor pairs)
- CorrelationBreakdownDetector (needs redundant sensor pairs)
- CUSUMDriftDetector (streaming-stateful; would over-fire on backfill)
- EMABaselineEstimator (streaming-stateful; backfill contaminates)
- EWMA/POT trackers (adaptive threshold; bulk-feeding breaks them)

### False-positive findings (measured 2026-09-10)

Per-window firing rates measured on the real native export fixtures:

| Detector | Clean July fixture (no fault) | Dec fixture (real stuck fault) |
|---|---|---|
| NoiseDetector | **69.6%** (1389/1997) | 3.1% (106/3368) |
| MultivariateMahalanobisDetector | **38.3%** (764/1997) | 9.5% (319/3368) |
| OutOfBoundsDetector | 0% | 0% |
| SpikeMADDetector | 0% | 0% |

**NoiseDetector** fires on 7 of every 10 windows of healthy clear-sky
data. This is not a marginal calibration issue — it is fundamentally
broken for this sensor cadence and must be recalibrated before Phase 1
admits it.

**MultivariateMahalanobisDetector** fires **4× more often on healthy
data than on the actual fault**. The signal is inverted, not merely
noisy. A plausible contributor is the synthetic-default ensemble
baseline (10 fictitious sequences from `build_default_baseline()`) —
the Mahalanobis distance is computed relative to a covariance that has
never seen real sensor data. See ADR-008 for the baseline fix, which
must land before recalibration.

**SpikeMADDetector** shows 0% on both fixtures. The December fixture
contains a real stuck fault with abrupt onset — a correctly calibrated
spike detector should arguably fire at the transition. 0% may mean the
MAD floor or threshold is too high for this cadence. This is unexplained
and needs investigation; quiet is not the same as correct.

**OutOfBoundsDetector** correctly shows 0% on both fixtures — neither
contains readings outside hard physical limits.

### Phase 1 requirement: imputed-channel exclusion

With `StuckCarry.skip_channels` empty, the stuck detector raises a
**false `co2` stuck alert** on the constant `400.0` fill value used for
imputed channels. The production path wires this correctly at
`src/api/services/cold_run_upload.py:261`:
`StuckCarry(skip_channels=frozenset(imputed_channels))`.

Any Phase 1 rule detector must use the same imputed-channel exclusion
mechanism. OutOfBoundsDetector must skip imputed channels the same way
WallclockStuckDetector does via `skip_channels`, or it will flag
constant fill values as stuck/OOB faults. This must be wired at the
`score_portion` level, not per-detector.

---

## Question 2: Vectorisation vs per-sequence

The upload path already vectorises the LSTM-VAE via `predict_batch_enriched`
(batches all sequences in a chunk). The question is which of the IN-list
detectors can operate on a chunk-level array rather than looping per sequence.

### Per-detector vectorisation assessment

| Detector | Current interface | Vectorisable? | Expected speedup | Notes |
|----------|------------------|---------------|-------------------|-------|
| DropoutDetector | `detect(sequence_raw: list[dict])` | **Yes** — can check null-fraction per row across a DataFrame column at once | ~20× over per-sequence loop (assumption: `isna().mean(axis=1)` on chunk) | Simple; the current implementation already iterates rows |
| NoiseDetector | `detect(series, sensor)` on 20-step 1D array | **Partial** — rolling variance can use `pd.Series.rolling().var()` on the full chunk column, then check threshold per window | ~5–10× (assumption: Pandas rolling vs Python loop) | The high-pass filter branch uses `scipy.signal.butter` which expects a single series; vectorising across sequences is non-trivial. Recommend using the non-highpass branch for upload. |
| OutOfBoundsDetector | `detect_series(series, sensor)` on 20-step 1D | **Yes** — `(chunk_col < lo) \| (chunk_col > hi)` is a single NumPy op | ~50× (assumption: NumPy comparison vs Python loop) | Trivially vectorisable |
| SpikeMADDetector | `detect(series, mad_floor)` on 20-step 1D | **No** — rolling MAD with floor is inherently per-window | 1× (cannot batch) | Could be reformulated with `pd.Series.rolling().apply(mad)` but the custom floor logic makes it complex. Low priority: spike detection on 20-step windows is fast. |
| BiasEMADetector | `detect_ema(series, threshold)` — static | **Partial** — EMA is vectorisable (`pd.Series.ewm()`) but the current implementation uses a scalar loop | ~5× with pandas EWM | Worth doing if bias detection proves valuable on batch |
| MultivariateMahalanobisDetector | `detect(sequence)` on (20, 4) | **No** — Mahalanobis distance requires covariance estimation per window | 1× | Could batch the distance computation if the covariance is pre-computed from the baseline, but the current impl fits per-call. |
| BOCPDDetector | `detect(series)` on 1D | **No** — BOCPD run-length posterior is sequential by definition | 1× | This is inherently O(n²) in window length. On a 20-step window it is cheap (~0.01 ms). |
| EnsembleAnomalyDetector | `score()` per member per sequence | **Partial** — IsolationForest `score_samples` can batch; statistical Mahalanobis can batch | ~3× for IF, ~2× for statistical | **Measured 1.05 ms/seq → 111 s/year — the single largest Phase 1 cost.** Baseline fit (22.3 ms once) is already cached. Vectorisation is the first-order concern. |

### Measured cost profile (2026-09-10)

All figures measured on the real native export (105,120 rows, uniform
300 s cadence) with `torch.set_num_threads(1)` to approximate a
shared-vCPU container. Hardware: Apple M-series 12-core.

| Component | Per-sequence | Full year (104,902 seq) |
|-----------|-------------|------------------------|
| LSTM-VAE (`predict_batch_enriched`, vectorised) | **0.043 ms** | **4.5 s** |
| `IsolationForest` + statistical + correlation fit (once) | — | **22.3 ms** |
| `ensemble.detect` per sequence | **1.05 ms** | **~111 s** |
| 4 rule detectors (OOB, SpikeMAD, Noise, Mahalanobis) | 0.33–0.45 ms/window | **~40 s** |
| Whole current upload path end-to-end | — | **11 s** (of which `score_portion` 6.4 s) |
| Peak resident set size | — | **435 MB** |

The previous estimate assumed ~10 ms/sequence for the LSTM-VAE forward
pass, giving ~525 s CPU for a 105k-row upload. The actual cost is
0.043 ms — **~230× cheaper than assumed** — because
`predict_batch_enriched` vectorises the entire batch through the model
in one forward pass rather than looping per sequence. Verified as
genuine inference: 2,984 distinct scores in 3,000 sequences, σ = 0.097,
embeddings shape (3000, 8).

A shared Railway vCPU is plausibly 2–4× slower than the M-series
measurement host, giving **30–45 s/year** as the conservative
planning figure for the current upload path.

#### Performance budget

**Phase 1 must keep a full-year upload under 60 s at 1 torch thread.**
The current 11 s (30–45 s on Railway) leaves room for vectorised rule
detectors. The 111 s `ensemble.detect` cost does NOT fit within this
budget and must be vectorised or deferred to a background pass.

#### Revised bottleneck analysis

The previous conclusion ("The LSTM-VAE is the real bottleneck;
optimising rule detectors is second-order") is **reversed**. The LSTM
is 4.5 s of an 11 s run — fast and already vectorised.
`ensemble.detect` at 1.05 ms/sequence (111 s for a full year) is the
single largest Phase 1 cost — **10× the entire current pipeline**.
Vectorising or deferring the ensemble is now the first-order concern.
OOB and DropoutDetector vectorisation is a rounding error by comparison.

**Recommendation:** Vectorise `EnsembleAnomalyDetector.detect` (batch
`IsolationForest.score_samples` + batch statistical Mahalanobis) or
defer ensemble scoring to a post-upload background job. Leave
per-sequence rule detectors as-is — their 40 s total is within budget
once the ensemble is addressed.

Measurement scripts: `prof_lstm.py`, `prof_full.py`, `prof_det.py`,
`prof_misc.py` (session scratchpad, 2026-09-10, single torch thread,
Apple M-series 12-core).

---

## Question 3: Incident write strategy

### Problem

`_store_and_observe` in the comprehensive pipeline calls
`IncidentMemory.store_incident` per anomaly. On the live path this is
one write per SSE enrichment — acceptable. On an upload of 105k sequences,
if even 5% are anomalous, that is ~5,250 incident writes, each involving:

1. An INSERT into the `incidents` table with embedding (32-dim vector),
   root_cause_details JSON, and sequence data
2. A vector similarity search (or numpy fallback) for the online learner
3. A commit per write (current: inside `_store_and_observe`)

At ~2 ms per write (assumption: SQLite), that is ~10 s. At ~10 ms per write
(PostgreSQL with pgvector similarity), that is ~52 s. Tolerable but wasteful
when many incidents will belong to the same episode.

### Recommendation: episode-level incidents, not per-sequence

The upload path already groups alerts into `AnomalyEpisode` objects. Each
episode is one logical event. Write one `Incident` per episode, not per
sequence.

For the Dec 14–26 fixture, this reduces incident writes from ~2,000+ (every
stuck-fault and recovery sequence) to 4–8 episodes. The incident record
should carry:

- `episode.channel`, `episode.detector`, `episode.severity`
- `episode.start_ts`, `episode.end_ts`, `episode.duration_seconds`
- The embedding of the **peak-score sequence** within the episode
  (representative, not every sequence)
- `root_cause_details` derived from the episode's detector and severity,
  not from the full pipeline's attribution/pattern/physics chain (which
  is live-path-specific)

### What this means for `IncidentMemory`

No schema change. The existing `Incident` model already has `fault_type`,
`root_cause`, `root_cause_details` (JSON), and an embedding column. The
episode-level write populates these fields from the episode metadata
rather than from the per-sequence `PipelineResult`.

### What this means for the online learner

The online learner (`_observe_online_learner`) should NOT be fed 105k
sequences in a bulk upload. It is designed for incremental live adaptation.
Bulk-feeding would over-fit the learner to the uploaded file's distribution.
**Exclude the online learner from the upload path entirely.**

If the learner needs to incorporate upload data, a separate
`bulk_observe(sequences, subsample_rate)` method should be added with
random subsampling at ~1–5% — but that is a separate design decision.

---

## Question 4: State across chunk boundaries — uniform carry mechanism

### Current state: two bespoke carries

| Carry | Fields | Threaded through |
|-------|--------|-----------------|
| `StatCarry` | `rows` (last N), `seen` (dedup set), `total_rows`, `columns` | `score_portion` → next `score_portion` |
| `StuckCarry` | `channels` (per-channel freeze state), `chunk_medians`, `emitted_onsets`, `skip_channels`, `completed_faults` | `score_portion` → next `score_portion` |

Adding a third bespoke carry (e.g., for noise-detector baseline variance
accumulation, or for episode-grouper incremental state) would be a smell.

### Proposal: `DetectorCarry` protocol + `ChunkCarry` composite

```
DetectorCarry (Protocol):
    def merge_chunk(self, chunk_result: Any) -> None
        """Absorb one chunk's worth of detector output."""
    def snapshot(self) -> dict[str, Any]
        """Serializable snapshot for debugging / MQ round-trip."""

ChunkCarry:
    stat: StatCarry          # existing
    stuck: StuckCarry        # existing
    detectors: dict[str, DetectorCarry]   # new per-detector carries
```

Each new detector that needs cross-chunk state registers a named
`DetectorCarry` implementation. The `score_portion` / `finalize_scores`
interface changes from:

```python
score_portion(..., stat_carry=stat_carry, stuck_carry=stuck_carry)
```

to:

```python
score_portion(..., carry=chunk_carry)
```

`StatCarry` and `StuckCarry` implement `DetectorCarry` retroactively
(they already have the right shape — `StatCarry.rows` is the merge-chunk
state, `StuckCarry.channels` is the snapshot).

### Which IN-list detectors need cross-chunk carry?

| Detector | Needs carry? | Why |
|----------|-------------|-----|
| DropoutDetector | No | Stateless per-sequence |
| NoiseDetector | No | Per-sequence rolling variance on 20-step window |
| OutOfBoundsDetector | No | Stateless comparison |
| SpikeMADDetector | No | Per-sequence |
| BiasEMADetector | No | Per-sequence static method |
| MultivariateMahalanobisDetector | No | Per-sequence |
| BOCPDDetector | No | Per-sequence (self-contained run-length computation) |
| EnsembleAnomalyDetector | No | Baseline fit is cached on the pipeline instance, not per-chunk |

**Result:** None of the Phase 1 IN-list detectors need cross-chunk state.
The `ChunkCarry` composite is therefore a structural improvement (unifying
`StatCarry` + `StuckCarry` under one threading mechanism) but does not
block Phase 1 adoption. It should be implemented as a preparatory refactor
before Phase 2 detectors (DiurnalProfile, BandBreach) are added, since
those may accumulate per-chunk statistics.

### Recommendation

Phase 1: Add new detectors as stateless per-sequence calls inside
`score_portion`, passing results into the existing `PortionScores` struct.
No new carry needed.

Phase 2 (prep): Refactor `score_portion` to accept a single `ChunkCarry`
composite instead of `stat_carry` + `stuck_carry` as separate parameters.
This is a mechanical refactor with no behavioral change — the test suite
pins the carry semantics.

---

## Question 5: Relationship between the two paths

### The divergence problem

Today there are effectively two detection stacks:

| Aspect | Upload path | Comprehensive pipeline |
|--------|-------------|----------------------|
| Detectors | 3 (LSTM, rolling z-score, wallclock stuck) | 18 |
| State model | Streaming carry across chunks | Per-tenant caches + DB |
| Threshold | Per-file calibration (P99.9) | Per-tenant adaptive (EWMA/POT) |
| Output | `UploadInferenceResponse` (scores, episodes) | `PipelineResult` (30+ fields) |
| Incident storage | None (scores persisted on `SensorReading`) | `IncidentMemory` per anomaly |
| Episode grouping | Yes (detection layer) | No (single sequence) |

These will drift: when a new detector is added to the comprehensive
pipeline, it must be manually considered for the upload path. The architect
review already flagged this (`anomaly-quality-phase-architect.md:256–287`).

### Options considered

**Option A: Upload path adopts the comprehensive pipeline.**
Call `ComprehensiveInferencePipeline.run()` per sequence from within
`score_portion`. This is the "naïve wiring" the task description correctly
rejects:
- 105k calls to `_run_with_components`, each with DB operations
  (incident storage, tracker load/save, diurnal profile fetch)
- Per-tenant stateful detectors (CUSUM, EMA, EWMA/POT) would be
  corrupted by bulk sequential updates
- Physics suppression and psychrometric checks are meaningless on
  retrospective data without climate context
- Would break the bounded-memory invariant (ensemble detector fits
  IsolationForest on baseline for every call — cached but the cache
  key is the baseline hash, not the tenant)

**Rejected.** The comprehensive pipeline is designed for online inference,
not batch processing.

**Option B: Comprehensive pipeline adopts the upload path's streaming model.**
Rewrite the comprehensive pipeline to accept chunks with carries. Every
live-path call becomes a chunk of size 1.

This inverts the abstraction: the complex part (18 detectors, fault-type
fusion, confidence scoring) would need to handle both single-sequence and
multi-sequence modes. The fusion logic (rule_votes > physics > ensemble >
band) is inherently per-sequence because the decision tree branches depend
on what each detector found for *that* sequence.

**Rejected.** The fusion logic's per-sequence decision tree is not
decomposable into chunk-level operations without rewriting the entire
pipeline.

**Option C (recommended): Shared detector library, two orchestrators.**
Extract the stateless detectors into a shared module that both paths
import. Each path keeps its own orchestration:

- Upload path: calls stateless detectors per-sequence inside
  `score_portion`, collects votes into `PortionScores`, runs episode
  grouping and file-level threshold calibration in `finalize_scores`.
- Comprehensive pipeline: calls the same detector classes per-sequence
  inside `_run_with_components`, adds stateful detectors (CUSUM, EMA,
  EWMA/POT), runs fusion, attribution, confidence, incident storage.

**What is shared:**
- Detector classes (`NoiseDetector`, `OutOfBoundsDetector`,
  `SpikeMADDetector`, `BiasEMADetector`, `MultivariateMahalanobisDetector`,
  `BOCPDDetector`, `EnsembleAnomalyDetector`, `DropoutDetector`)
- Configuration constants (thresholds, floors, limits)
- `_RULE_VOTE_PROFILES` (fault-type → root_cause/severity/urgency mapping)
- `_denormalize_sequence` (normalization reversal for physical-unit detectors)

**What is NOT shared (intentionally divergent):**
- Threshold calibration (per-file P99.9 vs per-tenant EWMA/POT)
- Stateful detectors (CUSUM, EMA — live only)
- Incident storage model (episode-level on upload vs per-sequence on live)
- Fusion/attribution/confidence (live only — upload produces episodes,
  not root-cause chains)
- Physics/psychrometric suppression (requires climate context)
- Online learner observation (live only)

**Why this works:** The detectors are already stateless classes in
`src/models/`. The comprehensive pipeline imports and instantiates them.
The upload path would do the same. The "shared core" already exists — it
is the `src/models/` detector modules. The convergence work is wiring
and orchestration, not abstraction extraction.

### Divergence guard

To prevent future drift, add a registry constant:

```python
# src/models/detector_registry.py
BATCH_SAFE_DETECTORS: frozenset[str] = frozenset({
    "dropout", "noise", "oob", "spike_mad", "bias_ema",
    "mahalanobis", "bocpd", "ensemble",
})
```

When a new detector is added to the comprehensive pipeline, the author
must explicitly classify it as batch-safe or not. A test asserts that
every key in `_RULE_VOTE_PROFILES` is either in `BATCH_SAFE_DETECTORS`
or in an explicit `LIVE_ONLY_DETECTORS` set. This makes the divergence
visible and intentional rather than accidental.

---

## Question 6: Migration and risk

### What is pinned today (173 tests)

This count is current — every row below was recounted with
`pytest <path> --collect-only -q` on 2026-10-15. The Phase-1 gate table
under *Verification per phase* further down still reads "All 111 tests
pass"; that figure is left alone deliberately, because it records what
the gate verified when Phase 1 was written, not today's inventory.

The current test suite pins:

| Test file | Count | What it pins |
|-----------|-------|-------------|
| `test_episode_grouping.py` | 18 | Episode boundaries, durations (34.83/22.08/47.92/163.83 h), severity, `is_recovery` never set on the upload path |
| `test_stuck_fault_upload.py` | 18 | Stuck-fault detection, gate timing, imputed-channel exclusion |
| `test_label_integrity.py` | 27 | Trigger channel/detector/severity persistence, reasoning generation |
| `test_score_transform.py` | 33 | Canonical score transform, non-finite guards |
| `test_light_unit.py` | 31 | Clear-sky July no false positives, light unit conversion |
| `test_023_migration.py` | 16 | Score backfill, migration mechanics |
| `test_recovery_tag_fixture_a.py` | 13 | Cross-channel `is_recovery` tagging on real pipeline output — one persisted upload of `native_dec_stuck_fault.csv`, its humidity releases detected by the real detector, read back through `GET /api/anomalies/episodes`; also pins that the query-time and upload-time views of the same pipe-1 excursion report the same peak |
| `test_recovery_tag_cross_upload.py` | 4 | A release persisted by one upload tags a hit written by a later one — the reason tagging is computed at query time from `anomaly_episodes` rather than from per-upload `stuck_carry` state |
| `test_recovery_tag_window.py` | 13 | Tagging-window boundaries measured in seconds around a release (`EPISODE_GAP_SECONDS` back, `RECOVERY_KNOCKON_WINDOW_HOURS` forward), start-anchored rather than overlap-based, cross-channel requirement, and that tagging never rescores or reranks |

**Critical regression targets:**
- 6.00 h stuck detection latency (onset_ts → detected_ts)
- 163.83 h frozen duration (last_frozen_dt boundary, not end_dt)
- Recovery episodes tagged informationally, never rescored (no score cap)
- Zero light alerts on clear-sky July midday irradiance
- Canonical score never saturates at 1.0 for finite input

### Migration strategy

**Invariant:** Every phase must leave the existing test suite green.
No test may be deleted or weakened — only new tests added.

**Phase 1 — additive detector wiring (safe):**
New detectors add alerts to `high_anomalies`. They do not modify or
suppress existing alerts. Existing detectors (LSTM, stat, stuck) run
first, exactly as today. New detectors run after, appending to a
separate list. The merge into `high_anomalies` uses existing
`_merge_by_timestamp` for LSTM/stat and timestamp-sorted append for
rule-detector hits.

**Risk:** New detectors may fire on sequences that were previously
normal. This is intentionally additive — new anomalies are found, but
no existing anomaly disappears. The clear-sky July fixture is the
key regression: if OutOfBoundsDetector or SpikeMADDetector fire on
normal July readings, the test fails. This is verifiable by running
the fixture through the new detectors before merging.

**Verification per phase:**

| Phase | Gate | How to verify |
|-------|------|--------------|
| 1 | All 111 tests pass | `pytest tests/ -x` |
| 1 | No new false positives on July fixture | New test: `test_batch_detectors_no_july_false_positives` |
| 1 | Stuck-fault durations unchanged | Existing `test_main_fault_duration_is_163_83h` |
| 1 | Recovery capping unchanged | Existing `test_no_statistical_episode_with_urgent_severity` |
| 2 | Episode-level incidents written | New test: `test_upload_creates_episode_incidents` |
| 2 | No per-sequence incidents on upload | New test: `test_upload_no_per_sequence_incidents` |
| 3 | ChunkCarry refactor is behavioral no-op | All 111 tests pass without any test changes |

---

## Question 7: Phasing

### Phase 1 — Stateless detector fan-out in `score_portion`
**Scope:** Wire the non-blocked stateless detectors into `score_portion`.
Each runs per-sequence inside the existing chunk loop, after stuck
detection and before statistical detection. Results are collected into
`PortionScores` via a new `rule_hits` field.

**Phase 1 detectors (6, down from original 8):**
1. DropoutDetector
2. OutOfBoundsDetector
3. SpikeMADDetector (pending investigation of 0% firing rate)
4. BiasEMADetector
5. BOCPDDetector
6. EnsembleAnomalyDetector — **only if vectorised or deferred; the
   1.05 ms/seq cost (111 s/year) breaches the 60 s budget unvectorised**

**BLOCKED (see false-positive findings above):**
- NoiseDetector — 69.6% FP on healthy data; recalibrate first
- MultivariateMahalanobisDetector — inverted signal; fix baseline
  (ADR-008) then recalibrate

**Phase 1 requirement:** All rule detectors must respect the
imputed-channel exclusion mechanism (`skip_channels`) already used by
`WallclockStuckDetector`. Without it, constant fill values (e.g.,
`400.0` for CO2) trigger false alerts.

**Performance budget:** A full-year upload must stay under 60 s at
1 torch thread. The current path takes 11 s; rule detectors at
~40 s/year are within budget. The ensemble at 111 s is not.

**Deliverables:**
1. Add `rule_hits: list[UploadHighAnomalyDetail]` to `PortionScores`
2. In `score_portion`, after LSTM scoring, iterate sequences and call
   each non-blocked stateless detector. Denormalize via
   `_denormalize_sequence` (the same function the comprehensive pipeline
   uses). Pass `skip_channels` to detectors that check per-sensor values.
3. Map detector results to `UploadHighAnomalyDetail` using
   `_RULE_VOTE_PROFILES` for fault_type/severity.
4. In `finalize_scores`, merge `rule_hits` into `high_anomalies` after
   the existing LSTM/stat/stuck merge. Rule hits use timestamp-sorted
   append (same as stuck hits today).
5. Episode grouping runs unchanged — it already groups by (channel, detector),
   so new detector types create new episode keys automatically.

**Verification gate:**
- All 111 existing tests green
- New test: July fixture produces zero rule-detector hits
- New test: synthetic spike sequence triggers OutOfBoundsDetector on upload
- New test: full-year upload completes under 60 s at 1 torch thread
- Ensemble either vectorised (batch IF.score_samples) or deferred

**Estimated effort:** 1 backend-engineer session (2 if ensemble
vectorisation is included).

### Phase 2 — Episode-level incident writes
**Scope:** After `finalize_scores` produces the episode list, write one
`Incident` per episode to `IncidentMemory`.

**Deliverables:**
1. New function `write_episode_incidents(episodes, ..., db)` in
   `cold_run_upload.py` or a new `upload_incidents.py`.
2. For each episode where `peak_score > CANONICAL_ANOMALY_THRESHOLD`:
   - Store one `Incident` with `fault_type = episode.detector`,
     `root_cause` from `_RULE_VOTE_PROFILES` or "statistical"/"lstm",
     `root_cause_details` carrying the episode metadata (channel,
     duration, sample_count, severity).
   - Embedding: use the LSTM embedding of the peak-score sequence
     (already available in `PortionScores.embeddings` when
     `include_embeddings=True`).
3. Wire into `run_cold_run_upload` after `finalize_scores`, gated on
   `persist_db is not None`.

**Verification gate:**
- All existing tests green
- New test: upload with known anomaly produces exactly N episode-level
  incidents (not N×sequence_count)
- New test: upload with zero anomalies produces zero incidents

**Estimated effort:** 1 backend-engineer session.

### Phase 3 — ChunkCarry unification (prep for Phase 2 detectors)
**Scope:** Refactor `score_portion` and `finalize_scores` to accept a
single `ChunkCarry` composite instead of `stat_carry` + `stuck_carry`.

**Deliverables:**
1. `ChunkCarry` dataclass containing `stat: StatCarry`, `stuck: StuckCarry`
2. Update `score_portion`, `finalize_scores`, `run_cold_run_upload` signatures
3. All callers updated (upload route, upload worker)

**Verification gate:**
- All existing tests green with zero test modifications (behavioral no-op)

**Estimated effort:** 0.5 backend-engineer session.

### Phase 4 (optional) — Conditional detectors: BandBreach + DiurnalProfile
**Scope:** Add BandBreachDetector (gated on OperationalConfig availability)
and DiurnalProfileDetector (gated on pre-fetched profile availability) to
the upload path.

**Prerequisite:** Phase 3 (ChunkCarry) if either detector needs cross-chunk
state. Currently neither does — both are per-sequence — but DiurnalProfile
requires a pre-fetched profile dict that should be loaded once per upload
and threaded through the carry rather than queried per chunk.

**Verification gate:**
- All existing tests green
- New test: upload with OperationalConfig produces band-breach episodes
- New test: upload without OperationalConfig produces no band-breach episodes

**Estimated effort:** 1 backend-engineer session.

### Phase 5 (optional) — Detector registry + divergence guard
**Scope:** Add `BATCH_SAFE_DETECTORS` / `LIVE_ONLY_DETECTORS` constants
and a test that asserts every `_RULE_VOTE_PROFILES` key is classified.

**Verification gate:**
- New test: `test_all_detectors_classified`

**Estimated effort:** 0.25 session.

---

## Recommendation

**Do Phase 1 first — but scope is narrower than originally planned.**

Phase 1 ships 6 detectors (down from 8). NoiseDetector and
MultivariateMahalanobisDetector are BLOCKED pending recalibration —
their false-positive rates are too high to ship (69.6% and inverted
signal respectively). The ensemble baseline must be fixed (ADR-008)
before recalibrating Mahalanobis.

The ensemble detector itself is gated on vectorisation: at 1.05 ms/seq
(111 s/year unvectorised) it breaches the 60 s performance budget.
Batch `IsolationForest.score_samples` and batch statistical Mahalanobis
are the path; alternatively, defer ensemble scoring to a background
post-upload job.

Phase 2 (episode incidents) is valuable for the anomaly history and
similarity search use cases but is independent of Phase 1. It can ship
before or after.

Phase 3 (ChunkCarry) is a structural improvement that should happen before
the codebase accumulates a third or fourth bespoke carry, but is not
blocking.

Phases 4 and 5 are nice-to-haves that depend on product requirements
(OperationalConfig provisioning, divergence-guard discipline).

### Resolved by measurement (2026-09-10)

1. **LSTM-VAE throughput on a real 105k upload.** Measured at 0.043 ms
   per sequence (4.5 s for 104,902 sequences) with 1 torch thread.
   The original ~5 ms assumption was ~230× too high because
   `predict_batch_enriched` vectorises the full batch in one forward
   pass. The LSTM is not the bottleneck; `ensemble.detect` at 1.05 ms/seq
   (111 s/year) is. See measured cost profile above.

2. **OutOfBoundsDetector interaction with CSV adapter imputation.**
   Confirmed: with `StuckCarry.skip_channels` empty, the stuck detector
   raises a false `co2` stuck alert on the constant `400.0` fill value.
   The production path wires this correctly at
   `src/api/services/cold_run_upload.py:261`. The skip-channel mechanism
   must be extended to all Phase 1 rule detectors — stated as a Phase 1
   requirement above.

3. **EnsembleAnomalyDetector's IsolationForest fit cost.** Measured at
   22.3 ms total for IF + statistical + correlation fit on the default
   baseline. This is a one-time cost per upload and is negligible. The
   per-sequence `ensemble.detect` cost (1.05 ms) is the concern, not the
   fit. Per-tenant baseline provisioning is addressed in ADR-008.

4. **False-positive rate of new detectors on upload data.** Partially
   resolved. NoiseDetector (69.6% FP on healthy data) and
   MultivariateMahalanobisDetector (inverted signal: 38.3% healthy vs
   9.5% faulted) are BLOCKED pending recalibration. OutOfBoundsDetector
   (0%) and SpikeMADDetector (0%) pass the clean-fixture check, but
   SpikeMAD's 0% on the faulted fixture is unexplained. See
   false-positive findings above.

### What genuinely remains unmeasured

1. **SpikeMADDetector's 0% firing rate on the December fault fixture.**
   The fixture contains a real stuck fault with abrupt onset. A
   correctly calibrated spike detector should arguably fire at the
   transition. 0% may mean the MAD floor or threshold is too high for
   300 s cadence data. **Must be investigated before trusting SpikeMAD
   on the upload path.**

2. **Episode-level incident embedding quality.** Using the peak-score
   sequence's embedding as the episode representative may not capture
   the episode's full character (e.g., a stuck fault's embedding is
   from a window with zero variance, which is informative but
   different from the recovery window's embedding). **Similarity
   search quality should be evaluated empirically after Phase 2.**

3. **Performance on the deployed Railway container.** All measurements
   are from local Apple M-series hardware. A shared Railway vCPU is
   plausibly 2–4× slower. The 60 s budget should accommodate this,
   but has NOT been verified on the target host.

---

## Alternatives explicitly rejected

### "Big-bang: replace upload scoring with comprehensive pipeline calls"

Rejected for the reasons in Option A above: DB operations per sequence,
stateful detector corruption, missing climate context, bounded-memory
violation. This is the naïve approach the task description anticipated.

### "Run comprehensive pipeline on a subsample of upload sequences"

Would give incomplete coverage — a stuck fault spanning 5,000 sequences
but sampled at 1% would see ~50 sequences, which is enough for LSTM but
not enough for the rolling z-score baseline or the wall-clock stuck gate.
The upload path's strength is *complete* coverage with bounded memory;
subsampling abandons that property.

### "Defer all convergence — keep the two stacks separate"

Tempting ("if it ain't broke"), but the architect review already identified
concrete failures: stuck faults visible on the live path but invisible on
upload. This phase added the stuck detector to close that specific gap.
The OOB, spike, and noise detectors address the same class of gap for
their respective fault types. Deferring means accepting that upload users
see a subset of the live path's alerts indefinitely, with the subset
widening as new detectors are added to the comprehensive pipeline.

### "Abstract both paths behind a common DetectionOrchestrator interface"

Over-engineering. The two paths have fundamentally different execution
models (streaming chunk loop vs single-sequence with DB state). A common
interface would either be too abstract to be useful or would force one
path to emulate the other's model. The shared-library approach
(Option C) gives the convergence benefits without the interface tax.
