TASK: Pin PyTorch to a single CPU thread in the upload worker, per ADR-007.

Read `docs/architecture/ADR-007-upload-worker-sizing.md` first.

## Why

Measured on the real 105,120-row export with `torch.set_num_threads(1)` vs the
default 6: throughput was identical (0.043 vs 0.045 ms/sequence). The LSTM-VAE is
tiny — INPUT_DIM=4, HIDDEN_DIMS=[64,32], LATENT_DIM=8, SEQ_LEN=20 — so extra threads
buy nothing. On a shared-vCPU container (the deployment is a budget Railway plan)
multiple torch threads contend with the API process for the same core. Pinning to 1
removes that contention at zero throughput cost.

## What to implement

1. Set torch to a single thread in the worker's startup path, before any model is
   loaded or any inference runs. The worker entry point is `src/workers/main.py`;
   the upload consumer is `src/workers/upload_consumer.py`. Put it wherever the
   worker process initialises, not inside a per-request or per-chunk function —
   calling it repeatedly is wasteful and calling it after threads are spawned has
   no effect.

2. Make it configurable rather than hardcoded, following the established pattern in
   `src/config.py` (`int(os.environ.get("NAME", "default"))`). Name it clearly, e.g.
   `TORCH_NUM_THREADS`, default `1`. A deployment on a bigger box must be able to
   raise it without a code change.

3. A value of `0` or less should mean "leave torch's default alone" — do not call
   `set_num_threads` at all in that case. Guard it.

4. Log the applied value once at startup at INFO, so the deployed configuration is
   visible in Railway logs. Follow the existing logging conventions in the worker.

5. Check whether `src/models/torch_runtime.py` already exists for exactly this kind
   of concern — if it does, that is where this belongs. Read it before adding
   anything new.

## What NOT to do

- Do not touch the API process. Only the worker.
- Do not change `numReplicas` in `railway.worker.json`. Single-worker is a
  documented architectural constraint (Redis Streams / SSE hub, episode-UUID
  correlation) — scaling replicas would break correlation, not add throughput.
- Do not add any dependency.
- Do not change model architecture, batch sizes, or any detector threshold.

## Acceptance

Test file: `tests/workers/test_torch_threads.py` (create it; check whether
`tests/workers/` already has a conftest you should reuse).

  - with the env var unset, the applied thread count is 1
  - with `TORCH_NUM_THREADS=4`, the applied value is 4
  - with `TORCH_NUM_THREADS=0`, `torch.set_num_threads` is NOT called
    (patch/mock it and assert not called)
  - the configuration is applied once at worker startup, not per chunk

Do not assert on the raw global `torch.get_num_threads()` after the fact if the test
runner has already set it — patch the setter and assert the call instead. That keeps
the test independent and idempotent, which this repo requires.

## Regression

Baseline is **719 passed, 1 failed, 8 skipped, 1 xfailed**. The single expected
failure is
`tests/api/test_023_migration.py::TestNonFiniteGuard::test_nan_in_live_row_becomes_null_with_unknown_scale`
(SQLite converts NaN to NULL on insert; unreachable premise locally). Leave it.

    venv/bin/python -m pytest tests/ -q --timeout=600 --ignore=tests/load

Any other failure is yours.

When complete:
mkdir -p tasks/done
echo '{"agent":"devops-lead","status":"done","output":"src/workers/main.py","tests_passed":true,"config_var":"REPLACE","where_applied":"REPLACE"}' > tasks/done/devops-lead.json
On failure:
echo '{"agent":"devops-lead","status":"failed","reason":"brief"}' > tasks/done/devops-lead.json
