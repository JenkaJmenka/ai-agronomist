# Performance Audit: Anomaly Detection Pipeline

> **Historical — point-in-time record.** Written 2026-07-02. Status: record only — the audit that fed [performance-refactoring-plan.md](performance-refactoring-plan.md); the file paths and line numbers it cites are as of that date. Not maintained against current code.

**Date:** 2026-07-02
**Auditor:** Architect Agent (claude-opus-4-6)
**Scope:** Memory leaks / unbounded growth, CPU hot spots, architectural recommendations
**Context:** Multi-tenant greenhouse monitoring. LSTM-VAE(input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20). Prior OOM fix in PR #8.

---

## (A) Memory Leaks and Unbounded Growth

### A1. AnomalyDetector._model_cache is an unbounded class-level dict [CRITICAL]

**File:** `src/models/inference.py:13`
**Evidence:**
```python
class AnomalyDetector:
    _model_cache: Dict[str, LSTMVAE] = {}
```
The `_model_cache` is a class variable (shared across all instances) that maps checkpoint paths to loaded LSTMVAE models. There is no eviction strategy. Every distinct checkpoint_path encountered across the lifetime of the process adds another LSTMVAE model to this dict. In a multi-tenant system where each tenant can train and deploy their own model, the number of cached models grows linearly with the number of distinct tenants that have ever trained a model. Each LSTMVAE instance holds ~40K parameters (input_dim=4, hidden_dims=[64,32], latent_dim=8), which is small individually, but the pattern is architecturally unsound and the cache never shrinks.

Note: The `get_cached_detector` function in `src/api/routes/inference.py:36-41` adds an LRU cache (maxsize=MAX_PIPELINE_CACHE_SIZE=3) on top of AnomalyDetector instances, but each AnomalyDetector's `_load_model()` still writes into the unbounded `_model_cache`. The LRU cache limits the number of AnomalyDetector wrapper objects, but the inner model cache is a separate dict that retains every model ever loaded.

**Severity:** CRITICAL (unbounded growth proportional to tenant count)
**Recommended fix:** Replace `_model_cache` with an `OrderedDict`-based LRU eviction mechanism, or remove it entirely and rely solely on the outer `get_cached_detector` LRU cache. If the class-level cache is kept, add a `maxlen` parameter that defaults to `MAX_PIPELINE_CACHE_SIZE`.

---

### A2. OnlineLearningManager.learners dict grows without bound [CRITICAL]

**File:** `src/models/online_learner.py:281`
**Evidence:**
```python
class OnlineLearningManager:
    def __init__(self, base_model: LSTMVAE, device: str = DEVICE) -> None:
        self.learners: Dict[str, OnlineLearner] = {}
```
The `observe()` method at line 292-295 creates a new learner on demand via `create_learner()`, which deep-copies the base model for each new tenant. Each `OnlineLearner` contains:
- A full deep copy of the LSTMVAE model (~40K parameters)
- A `ReplayBuffer` with `capacity=1000` (holding 1000 numpy arrays of shape (20,4))
- A `losses` deque with maxlen=2000
- An `update_history` defaultdict of deques with maxlen=500
- An SGD optimizer with momentum state
- An optional EWC instance (which stores a Fisher dict doubling the parameter memory)

There is no eviction. Each tenant costs approximately: model (~160KB) + replay buffer (1000 * 20 * 4 * 4 bytes = ~320KB) + losses + optimizer = ~0.5-1MB per tenant. With 1000 tenants, this is 500MB-1GB of leaked memory.

The `_manager` singleton at `src/api/routes/learning.py:51` persists for the process lifetime. The only code that removes a learner is `clear_models` (line 257), which requires an explicit API call.

**Severity:** CRITICAL (grows linearly with tenant count, never evicted)
**Recommended fix:** Add an LRU eviction policy to `OnlineLearningManager.learners`. When a learner is evicted, serialize its checkpoint to disk so it can be restored on demand. Set a configurable maximum tenant count (e.g., `MAX_ONLINE_LEARNERS = 50`).

---

### A3. ComprehensiveInferencePipeline per-tenant caches grow without bound [HIGH]

**File:** `src/api/services/comprehensive_pipeline.py:174,180`
**Evidence:**
```python
self._tracker_cache: dict[str, tuple[EWMATracker, POTThresholdFitter]] = {}
self._cusum_cache: dict[str, CUSUMDriftDetector] = {}
```
Both caches grow with each new `customer_id` seen and are never evicted. Each entry holds:
- `_tracker_cache`: EWMATracker (5 floats) + POTThresholdFitter (deque of up to 500 floats) = ~4KB per tenant
- `_cusum_cache`: CUSUMDriftDetector (dict of per-sensor tuples + alarm list) = ~0.5KB per tenant

While the per-tenant footprint is smaller than A2, the pattern is identical: no eviction, linear growth with tenant count.

**Severity:** HIGH (unbounded growth, though per-tenant footprint is moderate)
**Recommended fix:** Add LRU eviction to both caches. The pipeline already persists tracker state to the DB via `_save_trackers`, so evicted entries can be restored on demand.

---

### A4. _drift_detectors dict grows without bound [HIGH]

**File:** `src/api/routes/learning.py:51-52`
**Evidence:**
```python
_drift_detectors: Dict[str, DriftDetector] = {}
```
The `_get_drift_detector` function creates a `DriftDetector` per tenant and never removes it. Each detector holds a deque of `2 * window_size = 200` floats. The footprint is small (~1.6KB/tenant), but the pattern contributes to cumulative memory growth.

**Severity:** HIGH (unbounded, cumulative with other per-tenant caches)
**Recommended fix:** Pair with the learner LRU in A2. When a learner is evicted, its drift detector should be evicted too.

---

### A5. _orchestrators dict grows without bound [HIGH]

**File:** `src/api/services/orchestrator_registry.py:17`
**Evidence:**
```python
_orchestrators: Dict[str, RetrainingOrchestrator] = {}
```
Each `RetrainingOrchestrator` contains an `ActiveLearner`, which holds a `FeedbackCollector` (deque of up to 1000 dicts, each containing a full sequence), a `FeedbackValidator`, operator reliability tables, and retraining history. The footprint can be significant, especially with large feedback entries.

**Severity:** HIGH (unbounded, potentially large per-tenant footprint due to feedback sequences)
**Recommended fix:** Add LRU eviction backed by `LearningStateStore` persistence (already implemented for save/load).

---

### A6. AnomalyMetrics.pattern_distribution and confidence_distribution grow without bound [MEDIUM]

**File:** `src/monitoring/anomaly_metrics.py:29-30`
**Evidence:**
```python
self.pattern_distribution: Dict[str, int] = defaultdict(int)
self.confidence_distribution: Dict[float, int] = defaultdict(int)
```
While `detections`, `false_positives`, and `true_positives` are bounded deques (maxlen=10000), the two distribution dicts are plain defaultdicts. `pattern_distribution` is bounded by the number of distinct pattern types (7-8), so effectively bounded. However, `confidence_distribution` uses `round(confidence * 10) / 10` as keys (11 possible buckets: 0.0-1.0), so it is also effectively bounded. This is a low-risk finding.

**Severity:** LOW (effectively bounded by the bucketing scheme)
**Recommended fix:** No action needed. Document that bucket count is bounded.

---

### A7. Layer 1 inference cache on disk has no eviction [MEDIUM]

**File:** `src/dashboard/services/inference_cache.py`
**Evidence:** Flagged in the previous caching code review (CQ-1). The content-addressed disk cache at `data/inference_result_cache/` writes a JSON file per (file_hash, model_version) pair. There is no eviction, TTL, or size limit. Over time, the cache directory grows without bound.

**Severity:** MEDIUM (disk, not memory, but can exhaust filesystem on a small VPS)
**Recommended fix:** Add a file-count or total-size cap. On write, scan the directory and delete the oldest files when the cap is exceeded. Alternatively, add a TTL and a periodic cleanup task.

---

### A8. FeedbackCollector stores raw sequences in feedback items [MEDIUM]

**File:** `src/models/feedback_collector.py:93-94`
**Evidence:**
```python
feedback_item: Dict[str, Any] = {
    "sequence": sequence,  # stored as-is
    ...
}
```
The `sequence` parameter is stored as-is in memory. When `sequence` is a numpy array of shape (20, 4), this is 320 bytes per entry. With capacity=1000, the total is ~312KB. However, if callers pass raw lists-of-lists (as the comprehensive pipeline does at line 749), the memory footprint can be higher due to Python object overhead. The deque has a maxlen, so this is bounded, but the per-entry cost is non-trivial.

**Severity:** MEDIUM (bounded by deque capacity, but holding full sequences is wasteful if only metadata is needed for retraining decisions)
**Recommended fix:** Store only the sequence hash or a compact summary in the feedback entry. Keep full sequences in the replay buffer (which is the designated store for retraining data).

---

## (B) CPU Hot Spots

### B1. Per-request AnomalyAttributor and PatternDetector instantiation [HIGH]

**File:** `src/api/services/comprehensive_pipeline.py:222-226`
**Evidence:**
```python
async def run(self, ...):
    attributor = AnomalyAttributor(self.detector.model)
    attributor.set_baseline(baseline)
    pattern_detector = PatternDetector()
    pattern_detector.set_baseline(baseline)
```
For every single-sequence `run()` call:
1. A new `AnomalyAttributor` is created, which moves the model to the device and sets eval mode (line 74 of anomaly_attributor.py).
2. `set_baseline()` runs a full forward pass of the model on the baseline (10 sequences of shape (20, 4)) to compute `baseline_error` (line 112-116 of anomaly_attributor.py). This is a torch inference operation repeated on every request.
3. A new `PatternDetector` is created and `set_baseline()` computes per-sensor mean/std/min/max, pairwise correlations, and FFT spectra over the baseline sequences.

The `run_batch()` method (line 239-278) correctly creates these once and reuses them. But the single-sequence `run()` path pays this cost every time.

When the default synthetic baseline is used (which is the common case when no baseline_sequences are provided), these computations produce the same result every time for the same detector/model.

**Severity:** HIGH (redundant model forward pass + FFT/correlation computation on every request)
**Recommended fix:** Cache the `AnomalyAttributor` and `PatternDetector` instances on the `ComprehensiveInferencePipeline` object, keyed by the identity of the baseline array. Invalidate when the baseline changes. The ensemble already uses `self._ensemble` with lazy init (line 182-186); apply the same pattern.

---

### B2. AnomalyAttributor runs N+1 model forward passes per sequence [HIGH]

**File:** `src/models/anomaly_attributor.py:156-168`
**Evidence:**
```python
def attribute_sequence(self, sequence):
    anomaly_error = self._reconstruction_error(self._to_batch_tensor(sequence))
    for sensor_idx in range(sequence.shape[1]):
        masked_sequence = sequence.copy()
        masked_sequence[:, sensor_idx] = ...
        masked_error = self._reconstruction_error(self._to_batch_tensor(masked_sequence))
```
For `input_dim=4`, this runs 5 model forward passes (1 original + 4 masked) per sequence. Each forward pass involves encode + decode through LSTMs. This is architecturally correct for permutation-based attribution, but:
- The original `anomaly_error` is also computed in `_run_lstm_vae` (line 652-659 of comprehensive_pipeline.py) just before attribution. It could be passed in instead of recomputed.
- The 4 masked passes could be batched into a single (4, 20, 4) forward pass instead of 4 sequential (1, 20, 4) passes.

**Severity:** HIGH (5 forward passes where 4 could be batched, plus 1 is redundant)
**Recommended fix:** (1) Accept `anomaly_error` as a parameter to avoid the redundant first pass. (2) Batch all 4 masked sequences into a single (4, 20, 4) tensor and run one forward pass. This reduces 5 sequential forward passes to 1.

---

### B3. StuckFaultDetector uses pandas rolling variance per sensor per call [MEDIUM]

**File:** `src/models/stuck_fault_detector.py:64-68`
**Evidence:**
```python
variance = float(
    pd.Series(channel)
    .rolling(window=self._window_k, min_periods=self._window_k)
    .var()
    .min()
)
```
For each of the 4 sensors, a pandas Series is created from a numpy array, a rolling variance is computed, and only the minimum is taken. The overhead is:
1. numpy-to-pandas conversion (allocation + indexing overhead)
2. pandas rolling window machinery (designed for DataFrames, not scalar fast paths)

For sequences of length 20 and window_k=10, this creates 4 pandas Series objects per call. On a hot inference path, this is wasteful.

**Severity:** MEDIUM (pandas overhead on a hot path, but sequence lengths are small)
**Recommended fix:** Replace with a pure numpy sliding-window variance using `np.lib.stride_tricks.sliding_window_view(channel, self._window_k).var(axis=1).min()`. This avoids pandas entirely.

---

### B4. Cold-run upload reads the CSV file twice [MEDIUM]

**File:** `src/api/services/cold_run_upload.py:82-89, 143`
**Evidence:**
```python
def _read_chunks():
    return pd.read_csv(tmp_path, chunksize=chunk_size, ...)

# First pass (lines 97-125): compute adaptive ranges + build raw_readings
for raw_chunk in _read_chunks():
    ...

# Second pass (lines 143-189): normalize, create sequences, run inference
for raw_chunk in _read_chunks():
    ...
```
The CSV file is read from disk twice: once to compute adaptive sensor ranges and collect raw readings for statistical detection, and once to run LSTM inference. For large files, this doubles the I/O cost and pandas parsing overhead.

**Severity:** MEDIUM (I/O + parsing doubled)
**Recommended fix:** Combine into a single pass. During the first chunk iteration, store the normalized sensor data in a list (in addition to computing adaptive ranges). Then run inference on the stored data without re-reading the CSV. This trades memory for I/O; for chunk_size=5000 rows of 4 floats, each chunk is ~80KB, so the memory cost is negligible.

---

### B5. cold_run_upload iterrows() for raw_readings construction [MEDIUM]

**File:** `src/api/services/cold_run_upload.py:113-125`
**Evidence:**
```python
for _, row in raw_df.iterrows():
    entry: dict[str, Any] = {
        "timestamp": row["timestamp"].isoformat() ...
    }
    for col in raw_df.columns:
        ...
    raw_readings.append(entry)
```
`iterrows()` is one of the slowest pandas iteration patterns. For a 10,000-row CSV, this Python-level loop creates 10,000 dicts with per-cell type checking. The result is consumed by `ColdRunStatisticalDetector.detect_rolling()`, which also iterates row-by-row.

**Severity:** MEDIUM (slow on large uploads, but uploads are batch operations)
**Recommended fix:** Use `raw_df.to_dict(orient="records")` which is ~5-10x faster than `iterrows()`. Filter out timestamp/source columns beforehand.

---

### B6. ColdRunStatisticalDetector.detect_rolling computes per-row rolling stats in Python [MEDIUM]

**File:** `src/models/cold_run_detector.py:183-221`
**Evidence:**
```python
for col in columns:
    values, timestamps = self._column_series(readings, col)
    for idx in range(effective_window, len(values)):
        history = values[idx - effective_window : idx]
        history = history[np.isfinite(history)]
        std = float(np.std(history))
        z_score = abs(current - float(np.mean(history))) / std
```
For each column, for each row beyond the window, a numpy slice is extracted, filtered for finite values, and mean/std are computed. This is O(columns * rows * window) complexity. With 5 columns, 10000 rows, and window=288, this is ~14.4M numpy operations.

**Severity:** MEDIUM (CPU-intensive on large uploads, but bounded by chunk_size)
**Recommended fix:** Pre-compute rolling mean and std using vectorized numpy or pandas rolling (which uses Cython internally). Replace the inner Python loop with `pd.Series(values).rolling(window).mean()` and `.std()`, then vectorize the z-score computation.

---

### B7. EnsembleAnomalyDetector.detect re-fits baseline on identity mismatch [LOW]

**File:** `src/models/ensemble_detector.py:307`
**Evidence:**
```python
if self._baseline_cache is None or baseline is not self._baseline_cache:
    self.fit_baseline(baseline)
```
The `is` identity check means that if `baseline` is a new numpy array with the same content (which happens when `build_default_baseline()` is called multiple times), the IsolationForest, StatisticalDetector, and CorrelationDetector are all re-fitted. `fit_baseline()` includes an sklearn IsolationForest fit, a matrix inverse for Mahalanobis distance, and a correlation matrix computation.

In the comprehensive pipeline, `_prepare_baseline()` is called in `run()` and returns a new array each time (from `np.asarray` or `build_default_baseline`). The ensemble therefore re-fits on every `run()` call.

**Severity:** LOW (for single-sequence calls, the baseline is small -- 10 sequences of shape (20,4), so fit is fast; but architecturally wasteful)
**Recommended fix:** Cache by content hash rather than identity. Compute `hash(baseline.tobytes())` and compare to the cached hash. Or better yet, cache the fitted ensemble on the pipeline object alongside the attributor and pattern detector (see B1).

---

### B8. Pattern lag detection uses O(lags * seq_len) Python loops [LOW]

**File:** `src/models/pattern_scores.py:323-348`
**Evidence:**
```python
def _is_pair_lagged(x, y):
    correlations_by_lag = {
        lag: abs(_lagged_correlation(x, y, lag))
        for lag in range(-max_lag, max_lag + 1)
    }
```
For each candidate pair, `_lagged_correlation` computes `np.corrcoef` at each lag shift. With 4 sensors, up to 6 candidate pairs, and `max_lag=5`, this is 66 `np.corrcoef` calls per sequence. Each `np.corrcoef` is vectorized, but the Python-level loop and repeated slicing is suboptimal.

**Severity:** LOW (small constant factor: 4 sensors = 6 pairs, max_lag=5)
**Recommended fix:** Use `np.correlate` or `scipy.signal.correlate` with mode='full' for FFT-based cross-correlation, computing all lags in a single vectorized call per pair.

---

## (C) Architectural Recommendations

### C1. Introduce a Per-Tenant Resource Manager

All five unbounded-growth findings (A1-A5) share the same root cause: per-tenant state created on demand with no lifecycle management. The recommended architecture:

1. Create a `TenantResourceManager` that owns all per-tenant state: `OnlineLearner`, `DriftDetector`, `EWMATracker`, `POTThresholdFitter`, `CUSUMDriftDetector`, and `RetrainingOrchestrator`.
2. Use an LRU eviction policy with a configurable maximum tenant count.
3. On eviction, serialize state to the database (patterns for this already exist in `LearningStateStore` and `_save_trackers`).
4. On access, restore from DB if evicted.
5. Expose a `MAX_ACTIVE_TENANTS` config constant (suggest default: 50).

This eliminates the per-tenant leak pattern at its root and provides a single point for monitoring tenant resource consumption.

### C2. Cache Pipeline-Scoped Components

The `ComprehensiveInferencePipeline` should cache `AnomalyAttributor`, `PatternDetector`, and the ensemble's fitted baseline as instance-level state, invalidated only when the detector or baseline changes. This eliminates the per-request re-initialization cost (B1) and the per-request re-fitting cost (B7).

### C3. Batch Attribution Forward Passes

The `AnomalyAttributor.attribute_sequence` method should batch all masked sequences into a single forward pass (B2). For 4 sensors, this changes 5 sequential forward passes into 1 batched pass, a ~4x speedup on the attribution step.

### C4. Vectorize Statistical Detection

The cold-run statistical detection path (B5, B6) should use vectorized pandas/numpy operations instead of Python-level row iteration. This matters for large CSV uploads (10K+ rows).

### C5. Single-Pass CSV Processing

The cold-run upload should read the CSV once, not twice (B4). Store chunk data in memory during the first pass and process it for inference without re-reading.

---

## Prioritized Fix List

| Priority | ID | Severity | Owner | Description |
|----------|------|----------|-------|-------------|
| P0 | A2 | CRITICAL | backend-engineer | OnlineLearningManager.learners unbounded growth -- add LRU eviction with checkpoint persistence |
| P0 | A1 | CRITICAL | backend-engineer | AnomalyDetector._model_cache unbounded growth -- replace with LRU or remove in favor of outer cache |
| P1 | C1 | HIGH | architect + backend-engineer | Design and implement TenantResourceManager to unify A3, A4, A5 |
| P1 | B1 | HIGH | ml-engineer | Cache AnomalyAttributor + PatternDetector on pipeline, avoid per-request re-init |
| P1 | B2 | HIGH | ml-engineer | Batch masked attribution forward passes (5 passes -> 1 batched) |
| P2 | A3 | HIGH | backend-engineer | Add LRU to _tracker_cache and _cusum_cache |
| P2 | A4 | HIGH | backend-engineer | Add LRU to _drift_detectors |
| P2 | A5 | HIGH | backend-engineer | Add LRU to _orchestrators |
| P3 | B3 | MEDIUM | ml-engineer | Replace pandas rolling variance in StuckFaultDetector with numpy |
| P3 | B4 | MEDIUM | backend-engineer | Single-pass CSV reading in cold_run_upload |
| P3 | B5 | MEDIUM | backend-engineer | Replace iterrows() with to_dict("records") |
| P3 | B6 | MEDIUM | ml-engineer | Vectorize ColdRunStatisticalDetector.detect_rolling |
| P4 | A7 | MEDIUM | backend-engineer | Add disk cache eviction for Layer 1 inference cache |
| P4 | A8 | MEDIUM | ml-engineer | Stop storing full sequences in FeedbackCollector entries |
| P5 | B7 | LOW | ml-engineer | Content-hash baseline caching in EnsembleAnomalyDetector |
| P5 | B8 | LOW | ml-engineer | FFT-based cross-correlation in lag detection |

### Implementation Notes

- **P0 items** are deployment blockers for production multi-tenant use. They should be addressed before scaling beyond a handful of tenants.
- **P1 items** can be delivered together as a "pipeline caching" PR. The architect should provide the `TenantResourceManager` design; the backend-engineer implements it.
- **P2 items** are subsumed by C1 (TenantResourceManager). If C1 is implemented, A3/A4/A5 are automatically resolved.
- **P3/P4 items** are upload-path optimizations. They matter for large CSV uploads but do not affect real-time monitoring latency.
- **P5 items** are minor optimizations with low impact given the small data dimensions (4 sensors, 20 timesteps).
