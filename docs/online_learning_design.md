# Online Learning Pipeline — Design Specification (Gap 3)

**Status:** Implemented (498 tests passing, 93.55% coverage)
**Scope:** Continuous per-tenant fine-tuning of the pretrained LSTM-VAE on streaming
sensor data, with replay buffers, catastrophic-forgetting prevention, drift detection
and a persisted audit trail.

---

## 1. Architecture

### 1.1 Component overview

```
                          POST /api/learning/observe
                                     │
                                     ▼
                       OnlineLearningManager (singleton)
                       ┌─────────────────────────────┐
                       │ base_model (frozen LSTM-VAE)│
                       │ learners: tenant_id → ...   │
                       └──────────────┬──────────────┘
                          deepcopy(base_model) per tenant
                                     │
                  ┌──────────────────┼───────────────────┐
                  ▼                  ▼                   ▼
        OnlineLearner(t_A)   OnlineLearner(t_B)   OnlineLearner(t_C)
        ┌───────────────┐    each owns:
        │ model (copy)  │      • ReplayBuffer (capacity 1000)
        │ SGD optimizer │      • SGD(lr=1e-4, momentum=0.9, nesterov)
        │ EWC regulizer │      • ElasticWeightConsolidation
        │ loss history  │      • losses / update_history tracking
        └───────────────┘
                  │ per-tenant losses
                  ▼
        DriftDetector(t_X)  (per-tenant, module-level dict in the route)
                  │
                  ▼
        LearningLogStore ──► learning_logs table (audit trail)
```

All hyperparameters live in `src/config.py` (Online Learning section): no magic
numbers in the implementation modules.

### 1.2 ReplayBuffer (`src/models/replay_buffer.py`)

Fixed-capacity FIFO store (`collections.deque(maxlen=capacity)`,
`REPLAY_BUFFER_CAPACITY = 1000`) of recent sequences plus parallel deques for
`losses`, `timestamps` and `labels` (default label `"unlabeled"`). Sampling modes:

- **`sample(batch_size)`** — uniform, without replacement; returns everything when
  fewer than `batch_size` sequences are stored.
- **`sample_prioritized(batch_size, alpha)`** — prioritized experience replay.
  Priorities are `(loss + ε)^α` (`PRIORITY_ALPHA = 0.6`), sampled *with*
  replacement. Returns `(batch, weights)`, where importance-sampling weights
  `w_i = (N · P(i))^-β` (`IMPORTANCE_WEIGHT_BETA = 0.6`) are normalized so the
  maximum possible weight is 1. The weights de-bias gradients toward the true
  data distribution despite the skewed sampling.
- **`sample_by_label(label, batch_size)`** — without replacement over the subset
  carrying a given label; raises `ValueError` when the label is absent.

A capacity warning is logged once when the buffer first fills;
`get_statistics()` reports size, fill ratio, loss min/max/mean/std and the label
distribution. `clear()` resets all deques and the warning flag.

**Subclasses:**

- **`AnomalyBuffer`** (`ANOMALY_BUFFER_CAPACITY = 100`) — `add_anomaly(seq, score,
  is_true_anomaly)` stores the anomaly score as sampling priority and applies
  tri-state labels: `None → suspected_anomaly`, `True → confirmed_anomaly`,
  `False → false_positive`. Keeps a parallel `anomaly_scores` deque.
- **`SeasonalBuffer`** — `add_seasonal(seq, season, **kwargs)` tags sequences with
  a season string in a parallel `seasons` deque; `sample_season(season,
  batch_size)` samples only that season (returns `None` when the season is
  absent). **Hazard:** the parallel deques stay aligned only if every insert goes
  through `add_seasonal` / `add_anomaly` — see §6.

### 1.3 OnlineLearner — per-sample weighted loss (`src/models/online_learner.py`)

`OnlineLearner.observe(sequence)` scores each incoming `(seq_len, input_dim)`
sequence with the current model (`torch.no_grad()`), stores it (with its loss as
priority) in the replay buffer, and runs one SGD update every
`ONLINE_UPDATE_INTERVAL = 50` observations (skipped until the buffer holds at
least one full batch).

**Why the spec's scalar-loss weighting was replaced.** The original spec applied
PER importance weights to the *batch-mean* scalar loss, i.e.
`weight_mean * loss_mean`. That is mathematically wrong: multiplying an
already-reduced scalar by the mean weight rescales every sample's gradient by
the same factor, so the de-biasing effect of importance sampling is lost
entirely (high-priority samples remain over-represented in the gradient). The
implementation instead computes a **per-sample** β-VAE loss vector of shape
`(batch,)` — `_per_sample_loss` mirrors `LSTMVAE.loss_function` (per-sample MSE
mean + β · per-sample KL sum) without the batch reduction — multiplies it
element-wise by the per-sample importance weights, and only then reduces:
`(loss_per_sample * w).mean()`. This is the standard PER formulation: each
sample's gradient contribution is individually scaled by `w_i`, restoring an
unbiased gradient estimate.

Update step details: Nesterov SGD (`ONLINE_LEARNING_RATE = 1e-4`, deliberately
10× smaller than offline `LEARNING_RATE`; `ONLINE_SGD_MOMENTUM = 0.9`), EWC
penalty added when enabled, gradient clipping at `GRAD_CLIP_NORM = 1.0`, model
returned to `eval()` mode after every step. Update losses are recorded in
`update_history["loss"]`; an INFO line is logged every
`ONLINE_LOG_EVERY_N_UPDATES = 10` updates.

`predict(sequence)` returns `anomaly_score` (reconstruction MSE), `embedding`
(latent μ, length `LATENT_DIM = 8`), `uncertainty` (mean latent σ,
`exp(0.5·logvar)`), a loss `trend` (`increasing`/`stable`, only once
`2 · ONLINE_TREND_WINDOW = 100` losses are recorded) and `updates_performed`.

`needs_retraining(threshold)` flags drift when the normalized recent-vs-historical
loss increase exceeds `DRIFT_THRESHOLD = 0.3` (same heuristic as the
`DriftDetector`, computed over the learner's own loss history).

`save_checkpoint(path)` / `load_checkpoint(path)` persist/restore the adapted
state dict; loading uses `torch.load(..., weights_only=True)` (safe
deserialization, no arbitrary pickle execution).

### 1.4 Catastrophic forgetting prevention (`src/models/catastrophic_forgetting.py`)

Both regularizers penalize drift from a parameter snapshot θ\*, weighted by
per-parameter importance Ω:

```
L = task_loss + λ/2 · Σᵢ Ωᵢ · (θᵢ − θ*ᵢ)²
```

- **ElasticWeightConsolidation (used by `OnlineLearner`,** `EWC_LAMBDA = 0.4`**).**
  Snapshots θ\* at construction; `compute_fisher(dataloader)` estimates the
  diagonal Fisher information as the batch-normalized sum of squared gradients
  of the model's own VAE loss over a reference dataset. `penalty()` returns the
  quadratic drift penalty, or **0 while the Fisher has not been computed** —
  see Known limitations (§6): the production path never calls `compute_fisher`,
  so EWC is currently inert at runtime.
- **SynapticIntelligence (alternative, corrected from the spec;** `SI_LAMBDA = 0.4`,
  `SI_DAMPING = 0.1`**).** Tracks importance online from the loss path integral
  `w += −grad · Δθ`, consolidating into Ω at task boundaries
  (`Ω += w / (Δθ_total² + ξ)`) and re-anchoring θ\*. The spec's version computed
  the penalty as `(param − param.data)²`, which is **identically zero**; the
  implementation fixes this by anchoring the penalty to a stored
  `reference_params` snapshot, so the penalty correctly grows as parameters
  drift after consolidation. SI is implemented and tested but not wired into
  the online pipeline (EWC is the active mechanism). Its docstring's recommended
  call order contains a latent bug (§6).

### 1.5 DriftDetector (`src/models/drift_detector.py`)

Per-tenant rolling comparison of recent vs historical reconstruction loss.
Keeps a `deque(maxlen=2 · DRIFT_WINDOW_SIZE)` (window 100); once more than
`window_size` losses are recorded, computes

```
drift = (mean(recent_window) − mean(historical)) / (mean(historical) + ε)
```

and flags drift when `drift > DRIFT_THRESHOLD = 0.3`, returning
`action: "retrain_model"` (else `"continue_learning"`). While history is
insufficient it returns `drift_detected: False` with a `warning` instead of a
score, and the route skips persisting those evaluations.

### 1.6 OnlineLearningManager — per-tenant isolation

`OnlineLearningManager` holds one frozen `base_model` plus a `learners`
dict (`tenant_id → OnlineLearner`). `create_learner` (called on demand by
`observe`) hands each tenant a **`copy.deepcopy` of the base model**, fixing the
spec bug where every tenant fine-tuned the *same* model instance and therefore
leaked gradient updates (and indirectly data signal) across tenants. The base
model itself is never trained. `predict` for a tenant without a learner falls
back to the frozen base model and reports `learner_active: false`.

---

## 2. Data flow

```
client ──POST /api/learning/observe {tenant_id, greenhouse_id, sequences[]}
   │
   ▼  (Pydantic: shape SEQ_LEN×INPUT_DIM, batch ≤ MAX_OBSERVE_BATCH=256)
route: for each sequence
   │     ├─ manager.observe(tenant_id, seq)
   │     │     ├─ learner created on demand (deepcopy of base model)
   │     │     ├─ loss = VAE loss under torch.no_grad()
   │     │     ├─ buffer.add(seq, loss)                  ← replay storage
   │     │     └─ every 50th observation: _update_step()
   │     │           └─ prioritized batch + importance weights
   │     │              → per-sample weighted loss (+ EWC penalty)
   │     │              → backward → clip → SGD step → eval()
   │     └─ drift_status = detector.update(loss)         ← drift check
   ▼
LearningLogStore (async, tenant-scoped)
   ├─ log_event("observe_batch", sequences_observed, updates_performed,
   │            loss_before=losses[0], loss_after=losses[-1])
   └─ log_event("drift_check", drift_score, drift_detected, details)
                 [only once the detector has real history]
   ▼
ObserveResponse {losses[], sequences_observed, updates_performed, drift}
```

The audit trail (`learning_logs`) is the durable record; learner state itself is
in-memory only (§5).

---

## 3. API contract (`/api/learning`, `src/api/routes/learning.py`)

All request/response models are Pydantic v2 (`src/api/schemas/learning.py`).
Sequence shape is validated against `SEQ_LEN = 20` × `INPUT_DIM = 4` from config;
violations return 422.

### 3.1 `POST /api/learning/observe`

Feed a batch of sequences to a tenant's learner (creates it on first use).

Request (`ObserveRequest`):

```json
{
  "tenant_id": "gh_001",            // required, min_length=1
  "greenhouse_id": "gh_1",          // optional, default "default"
  "sequences": [[[0.5,0.6,0.4,0.5], ...20 rows...], ...]
                                    // 1..MAX_OBSERVE_BATCH(256) sequences,
                                    // each SEQ_LEN×INPUT_DIM
}
```

Response (`ObserveResponse`, 200):

```json
{
  "losses": [0.92, 0.88, 0.91],     // per-sequence VAE loss (anomaly signal)
  "sequences_observed": 3,
  "updates_performed": 0,           // SGD updates triggered by this batch
  "drift": {                        // latest drift evaluation (nullable)
    "drift_detected": false,
    "drift_score": 0.0,
    "recent_avg": null,             // null while detector lacks history
    "historical_avg": null,
    "action": null,                 // "retrain_model" | "continue_learning"
    "warning": "Insufficient data (3/100)"
  }
}
```

Errors: 422 (shape/batch/tenant validation), 503 (base checkpoint unavailable).

### 3.2 `POST /api/learning/predict`

Score one sequence with the tenant's adapted model, or the frozen base model if
the tenant has no learner.

Request (`PredictRequest`): `{"tenant_id": "gh_001", "sequence": [[...4 floats...] ×20]}`

Response (`PredictResponse`, 200):

```json
{
  "anomaly_score": 0.034,           // reconstruction MSE
  "embedding": [/* LATENT_DIM=8 floats, encoder mu */],
  "uncertainty": 0.97,              // mean latent std exp(0.5·logvar)
  "trend": null,                    // "increasing"|"stable" after 100 losses
  "updates_performed": 0,
  "learner_active": false           // false = frozen base model answered
}
```

### 3.3 `GET /api/learning/statistics?tenant_id=...`

Live in-memory learner statistics merged with the persisted summary.

Response (`LearnerStatisticsResponse`, 200):

```json
{
  "tenant_id": "gh_001",
  "learner": {                      // or {"status": "no_learner"}
    "tenant_id": "gh_001",
    "sequences_observed": 153,
    "updates_performed": 3,
    "buffer_status": {"size": 153, "capacity": 1000, "fill_ratio": 0.153,
                      "loss_stats": {...}, "label_distribution": {...}},
    "loss_statistics": {"min": ..., "max": ..., "mean": ..., "std": ...},
    "update_history": {"loss": [/* last 10 update losses */]}
  },
  "summary": {
    "events_by_type": {"observe_batch": 12, "drift_check": 9},
    "total_events": 21,
    "total_updates_performed": 3,
    "last_event_at": "2026-06-11T12:30:00Z"
  }
}
```

### 3.4 `GET /api/learning/history?tenant_id=...[&greenhouse_id=&event_type=&limit=]`

Persisted learning events, newest first. `limit` defaults to 100
(`DEFAULT_HISTORY_LIMIT`), bounded 1..`MAX_HISTORY_LIMIT = 1000`.

Response: `List[LearningHistoryEntry]` — each entry is one `learning_logs` row:

```json
[{
  "id": "uuid", "tenant_id": "gh_001", "greenhouse_id": "gh_1",
  "event_type": "observe_batch", "created_at": "2026-06-11T12:30:00Z",
  "sequences_observed": 3, "updates_performed": 0,
  "loss_before": 0.92, "loss_after": 0.91,
  "drift_score": null, "drift_detected": null,
  "details": null, "model_checkpoint_path": null
}]
```

Unknown tenant → `[]` (200).

### 3.5 `GET /api/learning/drift?tenant_id=...[&greenhouse_id=]`

Latest persisted `drift_check` event plus live detector state.

Response (`DriftStateResponse`, 200):

```json
{
  "tenant_id": "gh_001",
  "latest_check": { /* LearningHistoryEntry of the newest drift_check, or null */ },
  "detector": {                     // null when no live detector exists
    "window_size": 100, "threshold": 0.3, "losses_tracked": 42
  }
}
```

404 when the tenant has neither a persisted drift check nor a live detector.

---

## 4. Database: `learning_logs` + migration 003

`LearningLog` (`src/db/models.py`) — one row per learning event; **multi-tenant:
every query MUST filter `tenant_id`** (enforced by `LearningLogStore`, whose
methods all take a mandatory keyword-only `tenant_id`).

| Column | Type | Notes |
|---|---|---|
| `id` | String(36) PK | app-side UUID4 (portable across SQLite/PostgreSQL) |
| `tenant_id` | String(255), NOT NULL, indexed | tenant scope |
| `greenhouse_id` | String(255), NOT NULL, default `'default'` | zone scope |
| `event_type` | String(64), NOT NULL, indexed | `observe_batch`, `update_step`, `drift_check`, `checkpoint_saved`, `retrain_recommended` |
| `created_at` | DateTime(tz), NOT NULL, default now | event time |
| `sequences_observed` | Integer, NOT NULL, default 0 | |
| `updates_performed` | Integer, NOT NULL, default 0 | |
| `loss_before` / `loss_after` | Float, nullable | first/last batch loss |
| `drift_score` | Float, nullable | drift_check events |
| `drift_detected` | Boolean, nullable | drift_check events |
| `details` | JSON, nullable | free-form structured payload |
| `model_checkpoint_path` | String(512), nullable | checkpoint_saved events |

Composite index `ix_learning_logs_tenant_created (tenant_id, created_at)` serves
the dominant "tenant history, newest first" query.

**Migration `003_add_learning_logs_table.py`** (`revises 002`): plain portable
DDL (no vector columns), creates the table plus the three indexes; `downgrade`
drops them in reverse order. Verified by an actual `upgrade head` →
`downgrade 002` → `upgrade head` round-trip on scratch SQLite (see the Gap 3
review document).

`LearningLogStore` (`src/db/learning_logs.py`) is the async access layer:
`log_event` (whitelists optional columns, rejects unknown fields with
`ValueError`), `get_history`, `get_latest_drift`, `get_learning_summary`
(per-event-type counts, summed updates, last event timestamp).

---

## 5. Operational notes

- **Base model provenance.** The route loads the base LSTM-VAE through the same
  `get_cached_detector(None)` LRU cache the inference routes use (latest
  baseline checkpoint on disk). If no checkpoint can be loaded, every learning
  endpoint that needs the manager fails with **503 Service Unavailable** rather
  than serving an untrained model.
- **In-memory learner state.** `OnlineLearningManager`, every per-tenant
  `OnlineLearner` (adapted weights, replay buffer, loss history) and every
  `DriftDetector` are process-local singletons created lazily on first use.
  **All adaptation is lost on process restart**; after restart, tenants
  transparently restart from the base checkpoint. Durable adapted weights
  require explicitly calling `learner.save_checkpoint(path)` (and
  `load_checkpoint` on boot) — there is currently no automatic checkpointing
  policy or API endpoint for it. The `learning_logs` audit trail survives
  restarts and records what learning had occurred.
- **Drift → retrain recommendation.** Drift detection does *not* trigger
  retraining automatically. When `drift_score > 0.3`, the detector returns
  `action: "retrain_model"`, the event is persisted as a `drift_check` row, and
  operators (or an orchestration job) are expected to poll
  `GET /api/learning/drift` or the history endpoint and schedule full offline
  retraining via the existing training pipeline. Incremental online updates
  continue in the meantime.
- **Tuning knobs.** All under "Online Learning" in `src/config.py`:
  learning rate, momentum, buffer capacities, update interval, batch size,
  EWC/SI lambdas, PER α/β, gradient clip, drift window/threshold, trend window,
  observe-batch and history-limit caps.

---

## 6. Known limitations

1. **SI documented-call-order bug (QA finding; latent — SI is unused).**
   `SynapticIntelligence`'s class docstring instructs callers to invoke
   `accumulate_importance()` *after* `loss.backward()` but **before**
   `optimizer.step()`. At that point parameters have not moved since the last
   `track_step()`, so `delta = param.data − prev_params` is identically zero and
   the path integral never accumulates — following the documented order yields
   zero importance and therefore a zero SI penalty. The correct order (used by
   the unit tests) is `backward → step → accumulate_importance → track_step`.
   Latent today because the online pipeline uses EWC, not SI; must be fixed
   before SI is ever wired in.
2. **EWC is inert in the production path.** `OnlineLearner` constructs
   `ElasticWeightConsolidation` but nothing in the learner or API path ever
   calls `compute_fisher(...)`, and `penalty()` returns 0 until the Fisher is
   computed. Forgetting protection currently rests only on the small learning
   rate, gradient clipping and replay mixing. (Full detail in the Gap 3 review.)
3. **SeasonalBuffer / AnomalyBuffer desync hazard with inherited `add()`.** The
   subclasses keep auxiliary deques (`seasons`, `anomaly_scores`) parallel to the
   base deques. The inherited `add()` / `add_batch()` remain callable and do
   **not** append to the auxiliary deques, so mixing `add()` with
   `add_seasonal()`/`add_anomaly()` silently misaligns indices —
   `sample_season` would then return sequences from the wrong season. Callers
   must use only the specialized add methods; a structural fix (override `add`,
   or store per-entry records) is recommended before these buffers are used in
   production paths.
4. **Single-process manager state.** The module-level `_manager` and
   `_drift_detectors` singletons are per-process: running multiple uvicorn
   workers (or replicas) gives each worker independent learners, buffers and
   drift histories, with no cross-worker consistency or stickiness. Deploy the
   learning API single-worker, or move learner state to a shared store before
   scaling out.
5. **Unbounded loss history.** `OnlineLearner.losses` and
   `update_history["loss"]` grow without bound for long-lived tenants (floats
   only, but O(n) memory and O(n) trend computation per predict).
6. **Tenant identity is asserted, not authenticated.** As elsewhere in the
   platform, `tenant_id` is a client-supplied field with no auth layer; data is
   tenant-isolated but not access-controlled.
