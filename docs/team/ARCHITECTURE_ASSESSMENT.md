# Week 1 Architecture Assessment — AI Agronomist (Phase 1 Foundation)

**Scope:** LSTM-VAE model, synthetic data generator, training pipeline,
checkpointing/metrics, unit tests, and Docker environment built in Week 1
(Tasks 1–4).
**Author:** Solution Architect
**Status:** Reviewed — foundation **APPROVED** with a short list of tracked
minor tech-debt items.

This document answers Sections 1, 3, and 4 of
`implementation plan/WEEK_1_TASK_5_ARCHITECTURE_REVIEW.md`. The architectural
decisions themselves are recorded in `docs/team/ADRs/ADR-001..003`.

---

## Components Reviewed

| Component | File | Summary |
| --- | --- | --- |
| LSTM-VAE model | `src/models/lstm_vae.py` | 2-layer stacked LSTM encoder/decoder `[64, 32]`, 8-D latent, MSE + β·KL (β=0.1), checkpoint with metadata |
| Synthetic generator | `src/models/data_generator.py` | 1000×20×4 diurnal/trend + noise, clipped to physical bounds, seeded local RNG |
| Alt. generator | `src/data/synthetic_generator.py` | `GreenhouseDataset` with spike/drift/dropout anomaly injection (eval candidate) |
| Data utilities | `src/models/data_utils.py` | CSV-or-synthetic load, sliding-window preprocess, split, loaders, denormalize |
| Training pipeline | `src/models/training.py` | `VAETrainer`: Adam, TensorBoard, grad-clip 1.0, per-epoch checkpoint, early stopping |
| Checkpoint manager | `src/models/checkpoint_manager.py` | save/load with metadata, best-by-loss, retention of best N |
| Metrics logger | `src/utils/metrics_logger.py` | thread-safe TensorBoard + JSON summary, context manager |
| Configuration | `src/config.py` | flat constants + aggregated dicts |
| CLI | `scripts/train_lstm_vae.py` | argparse entry point |
| Containers | `docker-compose.yml`, `Dockerfile`, `Dockerfile.jupyter` | PostgreSQL, Jupyter, TensorBoard, Adminer |

---

## Section 1 — Architecture Review

### 1.1 Design Soundness

**Is the LSTM-VAE architecture appropriate for greenhouse sensor data?**
Yes. The signal is a short multivariate time series with strong temporal
structure (diurnal cycles, slow trends) and cross-channel correlation. Stacked
LSTMs model the temporal structure, and the variational bottleneck gives a
probabilistic embedding whose reconstruction error is a natural, unsupervised
anomaly score — exactly what is needed at cold start with no anomaly labels.
See ADR-001.

**Are the architectural choices (latent dim, hidden dims, β=0.1) justified?**
Yes, and they are verifiable in code:

- `HIDDEN_DIMS = [64, 32]`, `LATENT_DIM = 8`, `SEQ_LEN = 20`, `INPUT_DIM = 4`
  in `src/config.py`. The `64 → 32 → 8` funnel is a meaningful bottleneck for a
  4-channel signal — expressive enough to learn channel coupling, tight enough
  to force generalization.
- `BETA = 0.1` deliberately down-weights the KL term relative to reconstruction.
  For anomaly detection we want faithful reconstruction of normal data (so that
  anomalies reconstruct poorly); a low β favours reconstruction fidelity over
  latent disentanglement, which is the correct bias here.
- The KL term is **batch-averaged** (`/ batch_size` in `loss_function`) so it is
  reduced on the same scale as the mean-reduced MSE — a correct and often
  overlooked detail.

**Any obvious design flaws or anti-patterns?**
No blocking flaws. Two honest design observations:

1. The decoder reconstructs by expanding a single pooled latent across all 20
   steps (`z.unsqueeze(1).repeat(1, seq_len, 1)`) rather than decoding
   autoregressively. This is simpler and fine for fixed-length scoring, but
   limits fine-grained step-to-step dynamics; flagged as a future option in
   ADR-001, not a defect.
2. The encoder uses only the **last** time step of the final LSTM
   (`h[:, -1, :]`) as the sequence summary. Standard and reasonable for a
   20-step window; mean/attention pooling is a possible Phase 2 refinement.

### 1.2 Scalability for Phase 1.5

**Can this foundation extend to multi-greenhouse / multi-customer?**
Yes, with additive work. `VAETrainer` is fully dependency-injected (model,
loaders, config dict, checkpoint/log dirs, device) and carries no
customer-specific state, so it can train N models for N customers unchanged.
`CheckpointManager` and `MetricsLogger` were written with multi-run / multi-
tenant use in mind (`MetricsLogger` is explicitly thread-safe via a `Lock`).
See ADR-003.

**Will the database schema need changes for Phase 1.5?**
There is no application database yet — `docker-compose.yml` provisions
PostgreSQL + Adminer, but no SQLAlchemy models exist in `src/`. This is
expected for Phase 1. When the schema is introduced (Phase 2), it must include a
**`tenant_id` / `customer_id`** column on every customer-scoped table from day
one (a project guardrail). Designing it in now is cheap; retrofitting later is
not.

**Can we add new sensor types without major refactoring?**
Mostly. `INPUT_DIM` is a single config knob and flows through the model,
generator, and loaders. The friction points are the **hard-coded 4-channel
specifics**: `SENSOR_NAMES`, the four explicit `*_RANGE` tuples, and the
per-channel signal blocks in `generate_normal_data` (one block each for temp /
humidity / CO₂ / light). Adding a fifth sensor means extending config ranges and
adding a generator block — a contained, additive change, not a refactor.

### 1.3 Code Quality & Maintainability

**Are components loosely coupled?**
Yes. The model layer depends only on `config` and tensors; the trainer depends
on `LSTMVAE` plus injected loaders; the data layer uses a lazy import to avoid a
circular dependency between `data_utils` and `data_generator`
(`load_greenhouse_data` imports the generator inside the function). No circular
imports at module load.

**Is the code testable and reproducible?**
Yes. Seeds are controlled at every level: the generator uses an instance-local
`np.random.RandomState(seed)` (independent of global RNG), and the CLI sets
`torch.manual_seed` / `np.random.seed`. Checkpoints persist full metadata.
See Section 3.5.

**Any technical debt introduced?** Yes — see the consolidated list in
Section 4.2. None of it is blocking.

**Are interfaces clean and well-documented?**
Strong. Nearly every public method has a typed signature and a docstring with
Args/Returns/Raises. The `forward` 4-tuple contract and the loss reduction
convention are documented inline.

### 1.4 Performance Considerations

**Will inference meet the <200 ms p99 latency target?**
Expected yes for single-sequence scoring. Inference is two short LSTM passes
over a 20-step, 4-feature window through a `64→32→8` network — on the order of
single-digit milliseconds on CPU. The <200 ms p99 budget is comfortable for
per-sequence scoring; batching further amortizes cost. (Formal latency
benchmarking is a Phase 2 task; this is an engineering estimate, not a measured
number.)

**Any memory issues with 1000+ sequences?**
No. The full training tensor is `1000 × 20 × 4` float32 ≈ 320 KB; the model is
small (tens of thousands of parameters). Everything fits comfortably in RAM; no
GPU required for Phase 1.

**Is training time reasonable (minutes, not hours)?**
Yes. With early stopping (`EARLY_STOPPING_PATIENCE = 10`) on a dataset this
size and a model this small, CPU training completes in minutes. `training_time`
is recorded in the returned history dict for verification.
**One performance caveat in data generation:** `add_temporal_correlations`
smooths via triple-nested Python loops (`O(N·T·D)`). Negligible at 1000×20×4,
but it will not scale to large datasets and should be vectorized before scaling
up (Section 4.2).

### 1.5 Risk Assessment

**Security concerns.**
- `LSTMVAE.load_checkpoint` uses `torch.load(..., weights_only=False)` to
  restore the rich metadata dict (it stores a `datetime` and config). This is
  acceptable for **self-produced** checkpoints but is unsafe for untrusted
  files (pickle deserialization). For Phase 2, when checkpoints may cross trust
  boundaries, prefer `weights_only=True` for weights plus a separate
  JSON/sidecar for metadata. `CheckpointManager.load_checkpoint` likewise uses
  the default `torch.load` — same guidance.
- Docker secrets are parameterized via environment variables with dev defaults
  (`POSTGRES_PASSWORD:-postgres`, `JUPYTER_TOKEN:-dev_token`). Acceptable for
  local dev; production must supply real secrets via a secrets manager, never
  committed defaults.
- No hard-coded secrets or API keys were found in `src/`.

**Operational risks.**
- No persistent application database/schema yet (expected at Phase 1).
- Checkpoint retention (`cleanup_old_checkpoints`) is implemented but is not
  wired into `VAETrainer.train()`, which saves **every** epoch; on long runs the
  checkpoint directory grows unbounded until cleanup is invoked. Low severity,
  worth wiring in.

**Missing critical components (all expected at this phase):**
- No REST/inference API yet (Phase 2 — ADR-003 extension map).
- No database models / migrations yet (Phase 2).
- No dedicated anomaly-scoring/threshold module yet — the building block
  (per-sample reconstruction MSE) exists in `VAETrainer.calculate_accuracy`.

---

## Section 3 — Code Quality Assessment

### 3.1 Type Hints & Documentation

Excellent and consistent. Public functions across `lstm_vae.py`,
`data_generator.py`, `data_utils.py`, `training.py`, `checkpoint_manager.py`,
and `metrics_logger.py` carry full type hints and Google-style docstrings
(Args / Returns / Raises). `src/config.py` annotates every constant. Minor:
`CheckpointManager.__init__` docstring references a stale `checkpoint_template`
arg name, and `synthetic_generator.py` has a misspelled attribute
(`self.anomalaries`) — cosmetic, non-functional.

### 3.2 Error Handling

Solid where it matters.
- `CheckpointManager.load_checkpoint` validates required keys and wraps failures
  in `RuntimeError`, raising `FileNotFoundError` for missing files;
  `get_best_checkpoint` / `cleanup_old_checkpoints` skip corrupted files instead
  of crashing.
- `data_utils` validates dimensionality and sequence length (`ValueError` for
  too-short series or wrong ndim) and guards against divide-by-zero in
  normalization.
- `MetricsLogger.log_scalar` raises if used after `close()`.
- The CLI wraps `trainer.train()` and exits non-zero with a traceback on
  failure.
- Minor gap: `MetricsLogger.log_histogram` does not check the `_closed` flag
  (unlike `log_scalar`).

### 3.3 Testing

**Verified suite status: 66 passed, 1 skipped (CUDA device test), 87% total
coverage.** Per-module coverage:

| Module | Coverage |
| --- | --- |
| `src/config.py` | 100% |
| `src/models/training.py` | 97% |
| `src/utils/metrics_logger.py` | 98% |
| `src/models/checkpoint_manager.py` | 93% |
| `src/models/data_utils.py` | 85% |
| `src/models/data_generator.py` | 76% |
| `src/models/lstm_vae.py` | 75% |

Coverage exceeds the >80% target in aggregate, with critical training and
checkpoint paths well above it. Tests live in `tests/` and `tests/models/` and
cover shape contracts, loss behaviour, reproducibility, checkpoint round-trips,
early stopping, and data preprocessing edge cases. The two lowest-covered
modules (`data_generator`, `lstm_vae`) are the next places to add targeted
tests (e.g. decoder/edge branches, generator clipping/coupling paths).

**Test-suite hygiene issue (tracked):** `tests/smoke_training.py` is **not
collected by pytest** (no `test_`-prefixed functions) and references
`checkpoint_dir.exists()`, but `VAETrainer` stores `checkpoint_dir` as a `str`
(`str` has no `.exists()`), so the smoke script would fail if run as-is. It does
not affect the 66/87% result but should be fixed or removed.

### 3.4 Code Organization

Clean layered packages (`src/models`, `src/data`, `src/utils`, `src/config.py`,
`scripts/`), reasonable file sizes (largest source files ~200–320 lines), and no
circular imports at load time (the one potential cycle is broken with a lazy
import in `data_utils.load_greenhouse_data`). **Repo hygiene debt:** several
stray files should be removed before they cause confusion —
`_tmp_lstm_vae_new.py`, `debug_repro.py`, and an unparseable
`src/generated_code.py`.

### 3.5 Reproducibility

Strong.
- Random seeds are controlled end-to-end: the generator uses an instance-local
  `np.random.RandomState(seed)` (independent of global RNG), and
  `scripts/train_lstm_vae.py` sets `torch.manual_seed` and `np.random.seed`.
- Anyone can reproduce a run via the CLI with the same `--seed`; defaults all
  come from `src/config.py`.
- Checkpoints include all needed state: `LSTMVAE.save_checkpoint` stores
  `model_state_dict` + `epoch`/`loss`/`accuracy`/`timestamp`/`model_config`;
  `CheckpointManager.save_checkpoint` additionally stores `optimizer_state_dict`,
  `hyperparameters`, and a `metadata_version`.
- Minor reproducibility caveat: CPU/GPU nondeterminism in cuDNN is not pinned
  (acceptable for Phase 1; revisit if exact GPU reproducibility is required).

---

## Section 4 — Recommendations for Phase 2

### 4.1 Things to Keep

- **The LSTM-VAE design and its config-driven construction** — sound, small,
  fast, and fully parameterized (ADR-001).
- **Dependency-injected `VAETrainer`** — clean seam for multi-customer training.
- **Reproducible, config-externalized synthetic generator** with instance-local
  RNG (ADR-002).
- **Checkpoint + metrics infrastructure** — metadata-rich checkpoints,
  best-by-loss selection, thread-safe metrics logging built for multi-run use.
- **High test coverage and disciplined docstrings/type hints** — 66 passing
  tests, 87% coverage, strong interface documentation.
- **Layered package structure** that supports additive Phase 1.5/2 growth
  (ADR-003).

### 4.2 Things to Improve (tracked tech debt — none blocking)

1. **Config dual source of truth.** `src/config.py` exposes both flat constants
   and aggregated dicts (`MODEL_CONFIG`/`TRAINING_CONFIG`/`DATA_CONFIG`).
   Consolidate to one canonical representation (e.g. dataclasses, with flat
   names derived) to prevent drift.
2. **Repo hygiene.** Remove stray files `_tmp_lstm_vae_new.py`,
   `debug_repro.py`, and the unparseable `src/generated_code.py`.
3. **Broken smoke script.** `tests/smoke_training.py` is uncollected by pytest
   and calls `.exists()` on a `str` `checkpoint_dir`. Fix to use `Path` (or
   remove) and make it a real, collected test.
4. **Vectorize data smoothing.** Replace the triple-nested loop in
   `add_temporal_correlations` with a vectorized moving average before scaling
   `NUM_SAMPLES` up.
5. **Pin dependencies to the real environment.** `requirements.txt` pinned
   versions drift from the installed environment; reconcile and lock so CI and
   Docker builds are reproducible.
6. **Checkpoint safety & retention.** Adopt `weights_only=True` (+ sidecar
   metadata) when checkpoints may be untrusted, and wire
   `CheckpointManager.cleanup_old_checkpoints` into the training loop so the
   checkpoint directory does not grow unbounded.
7. **Minor polish.** Fix the `checkpoint_template` docstring reference, the
   `anomalaries` typo, and add the `_closed` guard to
   `MetricsLogger.log_histogram`.

### 4.3 Phase 2 Extensions

**REST API integration.** Add `src/api/` (FastAPI). Load the best checkpoint via
`CheckpointManager.get_best_checkpoint`, expose a `/score` endpoint that windows
incoming sensor data (reuse `data_utils.preprocess_sequences`), runs
`model.forward`, and returns per-sample reconstruction MSE as the anomaly score
with a configurable threshold. Because the model has no coupling to the data
source, this is an additive layer — no model changes required.

**Database layer integration.** Add `src/db/` with SQLAlchemy 2.x models and a
multi-tenant schema. Every customer-scoped table must carry a `tenant_id` from
the start (project guardrail), and all queries must filter by it. PostgreSQL is
already provisioned in `docker-compose.yml`; only the application schema and
migrations are missing.

**Multi-customer support.** Introduce `src/models/model_adapter.py` for transfer
learning / per-customer fine-tuning and `src/data/greenhouse_adapter.py` +
vendor loaders (e.g. `priva_loader.py`) behind the existing
`load_greenhouse_data` contract. Train and persist one model per tenant using
the already-injectable `VAETrainer` and the multi-run-aware checkpoint/metrics
infrastructure. The anomaly-injecting `GreenhouseDataset` in
`src/data/synthetic_generator.py` can back a Phase 2 evaluation harness to
measure detection performance against known injected spikes/drifts/dropouts.

---

## Verdict

**The Week 1 foundation is sound and approved to build on.** The LSTM-VAE design,
training pipeline, reproducibility story, and test coverage (66 passed,
87% total) are solid and well-documented, and the modular layout makes Phase 1.5
/ Phase 2 growth additive rather than a rewrite. The outstanding items are
**minor, non-blocking tech debt** — config consolidation, repo cleanup, a broken
(uncollected) smoke script, non-vectorized smoothing, dependency pinning, and
checkpoint-safety/retention polish — all enumerated in Section 4.2 and safe to
address incrementally. **No STOP-WORK guardrail violations were found.**
