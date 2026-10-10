# ADR-007: Upload Worker Sizing and CPU Configuration

**Date:** 2026-09-10
**Status:** Accepted
**Deciders:** Orchestrator, Architect

## Context

The AI Agronomist upload path processes full-year CSV telemetry files
(105,120 rows at 300 s cadence) through the LSTM-VAE, statistical
detectors, and the wallclock stuck-fault detector. The question was
whether the budget Railway plan can handle the detector fan-out designed
in the upload ↔ comprehensive pipeline convergence plan, or whether
additional compute must be provisioned.

### Measured workload (2026-09-10)

All figures measured on the real native export
(`2026_utk_test.csv`, 105,120 rows, uniform 300 s cadence) with
`torch.set_num_threads(1)` to approximate a shared-vCPU container.
Hardware: Apple M-series 12-core.

| Quantity | Measured |
|---|---|
| LSTM-VAE per sequence (vectorised batch) | 0.043 ms |
| LSTM-VAE, full year (104,902 sequences) | 4.5 s |
| Whole current upload path, end-to-end | 11 s (of which `score_portion` 6.4 s) |
| Peak resident set size, full year | 435 MB |
| `IsolationForest` + statistical + correlation fit (once) | 22.3 ms |
| `ensemble.detect` per sequence | 1.05 ms → 111 s for 105k |
| 4 rule detectors (OOB, SpikeMAD, Noise, Mahalanobis) per-sequence loop | 0.33–0.45 ms/window → ~40 s for 105k |

A shared Railway vCPU is plausibly 2–4× slower than the M-series
measurement host, giving **30–45 s/year** as the conservative planning
figure for the current upload path (without ensemble or rule detectors).

### Threading observation

1 torch thread and 6 torch threads gave identical throughput
(0.043 vs 0.045 ms/sequence) because the model is tiny:
`INPUT_DIM=4`, `HIDDEN_DIMS=[64,32]`, `LATENT_DIM=8`, `SEQ_LEN=20`.
Multiple torch threads buy nothing and contend with the API process on
a shared vCPU.

### Existing constraints

- `railway.worker.json` sets `numReplicas: 1`.
- This stack has a documented single-worker constraint: Redis Streams /
  SSE hub and episode-UUID correlation require a single worker. Scaling
  replicas would break correlation, not add throughput.
- The upload path is deliberately memory-bounded: `score_portion` runs
  per 5,000-row chunk with rolling carries (`StatCarry`, `StuckCarry`)
  so a full year never lands in memory at once. The 435 MB peak is
  dominated by the torch runtime and model weights, not by the data.

## Decision

1. **Remain on the budget Railway tier.** Do not provision additional
   CPU for this workload. A full-year upload costs 11 s of
   single-threaded CPU locally and is projected at 30–45 s on a shared
   vCPU — well within any reasonable timeout.

2. **Pin `torch.set_num_threads(1)` in the worker entrypoint.** Threads
   buy nothing for this model size, and on a shared vCPU multiple torch
   threads contend with the API process for the same physical cores.

3. **Size the container on memory, not CPU.** Budget ~1 GB for the
   worker process (435 MB measured peak plus torch runtime headroom and
   growth margin). Flag that anything at or below 512 MB is the one
   thing worth raising — it is the tightest constraint on this workload.

4. **Keep `numReplicas: 1`.** The single-worker constraint is
   architectural (correlation, SSE hub), not a scaling decision.

5. **Adopt the Phase 1 performance budget: a full-year upload stays
   under 60 s at 1 torch thread.** The current 11 s (30–45 s on
   Railway) leaves headroom for the Phase 1 rule-detector fan-out.
   The 111 s `ensemble.detect` cost would breach this budget if
   admitted unvectorised — that is a detector-design concern, not a
   hardware-provisioning concern.

### Measurement conditions

Measured 2026-09-10, single torch thread, Apple M-series 12-core.
**This has NOT been profiled on the deployed Railway container.**
Verifying the 2–4× slowdown estimate on the target host is the one
open verification item.

## Consequences

### Positive

- **No additional spend.** The workload fits the budget tier.
- **The 60 s budget is generous.** 30–45 s projected leaves room for
  rule detectors (~40 s locally, projected ~80–160 s on Railway — which
  means the per-sequence loop detectors need the ensemble to be
  vectorised or deferred, but the remaining detectors fit).
- **Pinning threads removes a real contention risk.** On a shared vCPU,
  torch's default thread pool (often = CPU count) competes with the
  FastAPI event loop and any concurrent requests. Pinning to 1 thread
  makes the worker a good neighbour.

### Negative / Risks

- **The 2–4× slowdown factor is an estimate, not a measurement.** If
  the Railway vCPU is slower than 4×, the 60 s budget is tighter than
  projected. This is the single most important thing to verify.
- **The 111 s `ensemble.detect` cost would breach the budget** if
  Phase 1 admits it unvectorised. This is a design constraint on
  Phase 1, not a hardware constraint — vectorise or defer.
- **Memory headroom is the thing to watch.** The 435 MB peak is
  dominated by the torch runtime. A torch version upgrade could shift
  this. If the Railway tier offers ≤512 MB, the container may OOM
  under load.
- **Single-worker means no concurrent uploads.** Two simultaneous
  full-year uploads would run sequentially (or the second would queue
  behind the first in the RabbitMQ worker). This is acceptable for the
  current user base but would need revisiting at scale.
