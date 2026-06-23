# Code Review — Gap 3: Online Learning Pipeline

**Reviewer:** code-reviewer agent
**Date:** 2026-06-11
**Verdict:** **APPROVED (with notes)** — no blocking findings. 1 High (latent),
5 Medium, 5 Low, 3 Info findings; none corrupts data, breaks tenant isolation,
or violates a STOP-WORK guardrail. The High and Medium items should be tracked
as fast-follow tickets.

## Scope

| Area | Files |
|---|---|
| Models | `src/models/replay_buffer.py`, `src/models/catastrophic_forgetting.py`, `src/models/online_learner.py`, `src/models/drift_detector.py` |
| DB | `src/db/learning_logs.py`, `LearningLog` in `src/db/models.py`, `alembic/versions/003_add_learning_logs_table.py` |
| API | `src/api/schemas/learning.py`, `src/api/routes/learning.py`, router registration in `src/api/main.py` |
| Tests | `tests/models/test_replay_buffer.py`, `tests/models/test_catastrophic_forgetting.py`, `tests/models/test_online_learner.py`, `tests/db/test_learning_logs.py`, `tests/api/test_learning_api.py` |
| Config | Online Learning section of `src/config.py` |

---

## Independent verification (performed by reviewer)

### Targeted test suites

```
venv/bin/python -m pytest tests/models/test_replay_buffer.py \
  tests/models/test_catastrophic_forgetting.py tests/models/test_online_learner.py \
  tests/db/test_learning_logs.py tests/api/test_learning_api.py -q --no-cov

124 passed, 19 warnings in 1.83s
```

All 124 Gap 3 tests pass. Warnings are benign (Starlette's deprecated
`HTTP_422_UNPROCESSABLE_ENTITY` alias; see L-5).

### Migration round-trip (scratch SQLite, `DATABASE_URL=sqlite:///./scratch_review.db`)

- `alembic upgrade head` → tables: `alembic_version, incident_actions, incidents,
  learning_logs, sensor_readings`; `learning_logs` created with all 13 columns
  (NOT NULL flags matching the ORM model) and indexes
  `ix_learning_logs_tenant_id`, `ix_learning_logs_event_type`,
  `ix_learning_logs_tenant_created`.
- `alembic downgrade 002` → `learning_logs` and all three indexes cleanly removed;
  the Gap 2 tables remain intact.
- `alembic upgrade head` again → succeeds; `alembic current` reports `003 (head)`.
- Scratch DB deleted afterwards.

Chain `001 → 002 → 003` is correct (`revision='003'`, `down_revision='002'`,
no branches), and the DDL is portable (no pgvector/dialect-specific types).

---

## Guardrail checklist

| Guardrail | Status |
|---|---|
| No secrets / API keys in code | ✅ PASS — none found in any reviewed file |
| No magic numbers (config constants) | ✅ PASS — all 17 Gap 3 hyperparameters in `src/config.py` with doc comments; modules import them. One semantic constant reuse noted (L-3) |
| Tenant isolation | ✅ PASS — `LearningLogStore` methods require keyword-only `tenant_id` and every query filters it; learners are per-tenant deep copies; tested explicitly (`test_tenant_isolation`, `test_summary_is_tenant_scoped`, `test_history_is_tenant_isolated`, `test_tenants_do_not_share_weights`) |
| No tight coupling LSTM-VAE ↔ API layer | ✅ PASS — routes consume `OnlineLearningManager`/`LearningLogStore` abstractions; schemas import config lazily |
| Model checkpoint metadata | ✅ N/A here — `save_checkpoint` writes a bare state dict (see M-4); the offline pipeline's metadata checkpoints are unaffected |
| Test coverage ≥ 80% | ✅ PASS — QA reports 93.55% overall (498 passed); 124 targeted tests independently re-run green |
| Error handling | ✅ PASS — fail-fast validation at boundaries (Pydantic 422, `ValueError` on unknown log fields/empty buffer/missing label), 503 on missing base checkpoint, 404 on unknown drift tenant |
| Hard-coded thresholds outside config | ✅ PASS |
| File size ≤ ~300 lines, functions ≤ 50 lines, ≤ 3 nesting levels | ✅ PASS — largest file 323 lines (`online_learner.py`, two cohesive classes; acceptable), all functions short and flat |

---

## Findings (severity-ranked)

### HIGH

**H-1. EWC is never armed in the production path — forgetting protection is
inert.** (`src/models/online_learner.py`, `src/api/routes/learning.py`)
`OnlineLearner.__init__` constructs `ElasticWeightConsolidation` and logs
"EWC enabled for catastrophic forgetting prevention", but **nothing in the
learner, manager or API path ever calls `ewc.compute_fisher(...)`**.
`ElasticWeightConsolidation.penalty()` deliberately returns `0.0` while
`self.fisher is None`, so every online update runs with a zero EWC penalty.
The feature is correct and tested in isolation, but as wired the advertised
catastrophic-forgetting prevention has no runtime effect; protection currently
rests only on the small learning rate (1e-4), gradient clipping and replay
mixing. *Latent/functional gap, not a correctness bug* — no wrong numbers are
produced — but the gap between documented and actual behavior is significant.
**Recommendation:** compute the Fisher once from a held-out reference set when a
learner is created (e.g. sample from the synthetic baseline data or the first
N buffered sequences), or log a prominent warning that EWC is dormant; update
the misleading "EWC enabled" log line until then.

### MEDIUM

**M-1. SI documented-call-order bug (QA finding — confirmed; latent, SI unused).**
(`src/models/catastrophic_forgetting.py:144-152`)
The `SynapticIntelligence` docstring instructs:

```
loss.backward()
si.accumulate_importance()   # after backward, before step
optimizer.step()
si.track_step()              # after step
```

`accumulate_importance` computes `delta = param.data − self._prev_params[name]`.
Before `optimizer.step()` the parameters have not moved since the last
`track_step()`, so `delta == 0` for every parameter and the path integral stays
zero forever → consolidated importance is zero → SI penalty is permanently
zero. The correct order — used by the unit tests' `_run_training_steps` helper —
is `backward → step → accumulate_importance → track_step`. Latent because
`OnlineLearner` uses EWC, not SI, but anyone following the class documentation
will silently get a no-op regularizer. **Fix the docstring (or restructure the
API so misuse is impossible, e.g. a single `on_step(loss)` hook) before SI is
ever adopted.**

**M-2. Unbounded memory growth in per-tenant learner state.**
(`src/models/online_learner.py:103-104,130,174`)
`self.losses` grows by one float per observed sequence and
`self.update_history["loss"]` by one per update, with no cap — unlike the
replay buffer (capped at 1000) and `DriftDetector.losses`
(`deque(maxlen=2·window)`). For a long-lived tenant streaming sequences this is
O(n) resident memory per tenant, and `predict()`/`needs_retraining()` recompute
`np.mean` over the entire history (O(n) CPU per call). Additionally, each
tenant holds a full deep copy of the LSTM-VAE plus optimizer state, and
`manager.learners` / `_drift_detectors` are never evicted — memory scales
linearly with distinct `tenant_id`s, which is unauthenticated client input
(see M-5). **Recommendation:** make `losses` a bounded `deque`, keep running
aggregates for the trend stats, and consider an idle-learner eviction policy.

**M-3. Module-level singleton state: thread-safety and event-loop blocking.**
(`src/api/routes/learning.py:48-49,66-78`)
`_manager` and `_drift_detectors` are module globals initialized without
locking. Because all five endpoints are `async def`, they execute on the single
event-loop thread, so plain FastAPI deployments won't race — but: (a) the lazy
init in `_get_manager`/`_get_drift_detector` is not safe if any future code
touches these from a thread pool or a multi-threaded server; (b) the entire
observe loop — up to `MAX_OBSERVE_BATCH=256` forward passes plus potentially
several SGD update steps and a model deepcopy on first contact — runs
**synchronously inside an async handler, blocking the event loop** for every
other request (including health checks) for possibly seconds; (c) under
multiple uvicorn workers each process holds divergent learner state (documented
single-process limitation). **Recommendation:** offload `manager.observe` to a
worker thread (`run_in_executor`/`anyio.to_thread`) with a per-tenant lock, and
document a single-worker deployment constraint until learner state is
externalized.

**M-4. `SeasonalBuffer`/`AnomalyBuffer` desync hazard via inherited `add()`.**
(`src/models/replay_buffer.py:201-262`)
The subclasses maintain auxiliary deques (`seasons`, `anomaly_scores`) parallel
to the base-class deques, but the inherited `add()`/`add_batch()`/`clear()`
remain callable and do not touch them: one `add()` call on a `SeasonalBuffer`
shifts every subsequent index, so `sample_season` returns sequences from the
wrong season once eviction begins (`seasons[i]` no longer corresponds to
`sequences[i]`); `clear()` also leaves `seasons`/`anomaly_scores` populated.
Latent today — neither subclass is used by `OnlineLearner` or the API — but it
is an API-shape bug waiting for its first caller. **Recommendation:** override
`add()`/`clear()` in both subclasses (append a default season/score, clear the
auxiliary deque), or store one record object per entry.

**M-5. Checkpoint persistence gaps.** (`src/models/online_learner.py:245-254`)
Two related notes: (a) `load_checkpoint` correctly uses
`torch.load(..., weights_only=True)` — good, safe deserialization. However
`save_checkpoint` writes a bare state dict with **no metadata** (tenant, step
counts, config, timestamp), unlike the offline `CheckpointManager` whose
metadata is a guardrail expectation; restored learners also lose
`step_count`/`update_count`/buffer contents, so statistics silently reset.
(b) Nothing in the pipeline ever calls `save_checkpoint` — adapted weights are
lost on restart with no operational hook (documented as a limitation, but there
is no endpoint or periodic job either). Pre-existing, out-of-scope note: the
*base*-model loading path (`src/models/inference.py:39`,
`src/models/lstm_vae.py:275`) still uses `weights_only=False`; the Gap 3 code
itself does not add any unsafe loads.

### LOW

**L-1. `observe` audit semantics: `loss_before`/`loss_after` are the first/last
sequence losses of the batch,** not pre/post-update losses, even when an SGD
update fired mid-batch (`src/api/routes/learning.py:113-114`). With
heterogeneous sequences this can suggest improvement/regression that is just
inter-sequence variance. Also, only the *last* drift evaluation of the batch is
persisted (one `drift_check` per request) — reasonable, but means per-sequence
drift transitions inside a batch are not auditable.

**L-2. Drift detector double-bookkeeping.** Loss-trend logic exists in both
`DriftDetector.update` and `OnlineLearner.needs_retraining`/`predict` (same
formula, different windows: 100 vs 50, full-history baseline vs rolling 2×window
baseline). Minor duplication; could drift apart. Consider having the learner
own a `DriftDetector`.

**L-3. Semantic constant reuse.** `get_statistics` truncates
`update_history` to the last `ONLINE_LOG_EVERY_N_UPDATES` (10) entries — a
logging-cadence constant repurposed as a history-page size
(`src/models/online_learner.py:226-229`). Works, but a dedicated constant would
better express intent.

**L-4. `OnlineLearningManager.predict` fallback duplicates learner logic and
reaches into model internals** (`self.base_model.fc_mu.weight.device`,
`src/models/online_learner.py:302-312`) rather than reusing a shared helper /
`next(model.parameters()).device`. Behavior is correct and tested.

**L-5. Tests use the deprecated `status.HTTP_422_UNPROCESSABLE_ENTITY` alias**
(19 deprecation warnings in the run) and assert on private attributes
(`buffer._capacity_warned`, `store._session`) in a few places. Cosmetic;
behavior-focused otherwise.

### INFO

**I-1. Test quality is strong.** Clear per-behavior tests with descriptive
names; tenant isolation, validation failures, FIFO eviction, PER bias and
weight bounds, EWC/SI mathematical properties (zero-at-anchor,
positive-after-drift, λ-scaling), drift warm-up and trigger paths, the 503
checkpoint path, singleton reset between tests, and checkpoint round-trips are
all covered. The prioritized-sampling statistical test (`>30/40` high-loss
draws with α=1) is mildly stochastic but effectively deterministic at these
probabilities.

**I-2. Spec corrections are real improvements, verified:** per-sample importance
weighting (vs the spec's meaningless scalar-loss × mean-weight product), the SI
penalty anchored to `reference_params` (the spec's `(param − param.data)` is
identically zero), and per-tenant `copy.deepcopy` isolation in the manager
(the spec shared one mutable model across all tenants). Each correction has a
dedicated regression test.

**I-3. Migration/ORM parity confirmed** column-by-column (types, nullability,
server defaults, three indexes), and `learning.router` is registered in
`src/api/main.py` alongside the other routers.

---

## Verdict

**APPROVED (with notes).** Tenant isolation, input validation, config
discipline, migration correctness and test quality are all solid, and the
implementation demonstrably fixes three real spec bugs. The High finding (EWC
dormant in the wired pipeline) and M-1..M-5 are recommended fast-follows; none
blocks merging Gap 3, since no finding produces incorrect output, leaks data
across tenants, or violates a STOP-WORK guardrail.

| Severity | Count | Items |
|---|---|---|
| High | 1 | H-1 (EWC Fisher never computed — protection inert; latent) |
| Medium | 5 | M-1 (SI docstring call-order bug, QA-confirmed, latent), M-2 (unbounded losses / per-tenant memory), M-3 (singleton thread-safety + event-loop blocking), M-4 (SeasonalBuffer/AnomalyBuffer desync), M-5 (checkpoint metadata/persistence gaps) |
| Low | 5 | L-1..L-5 |
| Info | 3 | I-1..I-3 |

*(M-5 is partially pre-existing/out-of-scope: the `weights_only=False` loads are
on the base-model path that predates Gap 3.)*
