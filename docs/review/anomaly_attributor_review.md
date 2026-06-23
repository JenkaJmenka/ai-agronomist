# Code Review — Week 1 Phase 1.1: Anomaly Attributor

**Verdict: NEEDS FIXES** *(superseded by re-review — final verdict: PASS WITH NOTES, see Addendum)*

One ML-guardrail STOP-WORK condition is unmet (no NaN/Inf detection on model outputs, with a silent-failure path that converts NaN into a plausible-looking attribution). The fix is small and localized. Everything else passes: the four documented spec deviations are all sound, the attribution math is correct, thresholds are properly externalized to config, and the 36 tests are deterministic and behavior-focused.

Files reviewed: `src/models/anomaly_attributor.py`, `tests/models/test_anomaly_attributor.py`, `src/config.py` (lines 241–253), against `src/models/lstm_vae.py` and the Phase 1.1 spec.

---

## Guardrail Checklist

### `.claude/guardrails/universal-guardrails.md`

| Guardrail | Result | Evidence |
|---|---|---|
| STOP: Tight coupling LSTM-VAE ↔ API layer | PASS | `anomaly_attributor.py:27-41` imports only stdlib, numpy, torch, `src.config`, `src.models.lstm_vae`. No `src/api` references anywhere. |
| STOP: Secrets/API keys hardcoded | PASS | No credentials, tokens, or keys in any reviewed file. |
| STOP: Checkpoint saved without metadata | PASS (N/A) | Module saves no checkpoints; `LSTMVAE.save_checkpoint` (lstm_vae.py:216-262) includes epoch/loss/accuracy/timestamp/model_config and is untouched. |
| STOP: DB schema without tenant_id | N/A | No database code in scope. |
| STOP: Test coverage <80% on critical paths | PASS | Reported 100% on module, 93.83% total. Not independently re-run (reviewer is read-only); figures are consistent with the test suite exercising every public and private method, including both `_normalize_contributions` branches and all `ValueError` paths. |
| STOP: Major design change without ADR | PASS, with note | The four spec deviations are algorithm-level corrections, documented in the module docstring (`anomaly_attributor.py:10-14`) and test docstring. Not an architecture change; ADR not strictly required. See Note N1. |
| WARN: Hard-coded thresholds | PASS | All thresholds in `src/config.py:245-253` (`ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD`, `ATTRIBUTION_CONFIDENCE_SCALE`, `EXPLANATION_DEVIATION_THRESHOLD`) plus reuse of `NUMERICAL_EPSILON` and `SENSOR_NAMES`. Spec's inline magic numbers (0.15, 2, 0.1, 1e-6, hardcoded sensor list) were all externalized. |
| WARN: Missing error handling | PARTIAL | Shape/emptiness validation is thorough (`set_baseline:92-102`, `_validate_sequence:246-256`). No finiteness handling — see Finding F1. |
| WARN: Code duplication (3+ blocks) | PASS | Spec's three copy-pasted tensor/forward/MSE blocks were correctly extracted into `_reconstruction_error` and `_to_batch_tensor` (lines 219-228). |
| WARN: Silent failures (bare except, pass) | FAIL | No bare excepts, but `max(0.0, anomaly_error - masked_error)` at line 149 silently clamps NaN to 0.0 — see Finding F1. |

### `.claude/guardrails/ml-guardrails.md`

| Guardrail | Result | Evidence |
|---|---|---|
| STOP: LSTM-VAE spec locked (no change without ADR) | PASS | Architecture untouched. Attributor uses the model's real API correctly: `encode(x) -> (mu, logvar)` with `(batch, latent_dim)` outputs (lstm_vae.py:115-139), `decode(z)` taking `(batch, latent_dim)` → `(batch, seq_len, input_dim)` (lstm_vae.py:141-162). Calls at `anomaly_attributor.py:222-223` match these signatures exactly. |
| STOP: Checkpoint without epoch/loss/accuracy/timestamp/model_config | N/A | No checkpointing in scope. |
| STOP: NaN/Inf in model outputs with no detection | **FAIL** | `_reconstruction_error` (lines 219-224) returns the raw MSE with no finiteness check; `_validate_sequence` (246-256) and `set_baseline` (92-102) accept NaN/Inf inputs. See Finding F1. |
| STOP: Training loop with no early stopping | N/A | No training in scope. |
| STOP: Hard-coded sensor ranges | PASS | Sensor names come from `config.SENSOR_NAMES` (line 39, 75); no physical ranges hardcoded. |
| WARN: beta > 1.0 / dropout > 0.5 / lr > 1e-2 | N/A | No hyperparameters changed. |

### `.claude/guardrails/red-flags.md`

| Red flag | Result | Evidence |
|---|---|---|
| 1. Writing outside file ownership | PASS | `src/models/` + `src/config.py` (ml-engineer) and `tests/models/` (qa-engineer) per CLAUDE.md ownership table. |
| 2. `os.system` / `subprocess shell=True` / `eval()` | PASS | None present. |
| 3. Hardcoded password/token/API key | PASS | None present. |
| 4. DELETE/DROP without tenant_id | N/A | No queries. |
| 5. `model.save()` without metadata JSON | N/A | No saving. |
| 6. Test file modifies implementation files | PASS | Tests only import; stubs (`_IdentityModel`, `_FirstChannelErrorModel`, test:33-52) are local classes. |
| 7. Circular imports `src/models/` ↔ `src/api/` | PASS | One-directional: attributor imports model + config only. |

---

## Assessment of the Four Documented Spec Deviations

All four are sound improvements over the spec:

1. **Deterministic `encode -> decode(mu)` instead of stochastic `forward`** (lines 10-14, 219-224) — Correct. `forward` calls `reparameterize` unconditionally (lstm_vae.py:177-181), so even in eval mode the spec's approach would inject ~1e-3 sampling noise into error deltas that are themselves often that small, making attributions non-reproducible. Using the posterior mean is the standard deterministic reconstruction for VAE scoring. Verified by `test_should_be_deterministic_across_calls` (test:260-267), which asserts exact equality — only possible with this fix.

2. **Baseline-mean axis fix in `get_explanation`** (lines 201-204) — Correct, and fixes a real bug in the spec. The spec's `np.mean(baseline_sequences, axis=0)` yields shape `(seq_len, input_dim)`; subtracting it from a `(input_dim,)` vector broadcasts to `(seq_len, input_dim)`, after which `deviations[sensor_idx]` indexes a *timestep*, not a sensor. The implementation's `mean(axis=(0, 1))` → `(input_dim,)` is the correct per-sensor deviation.

3. **Uniform fallback when all contributions ~0** (lines 230-244) — Sound for preserving the documented sums-to-1.0 contract (the spec silently returned an unnormalized all-zeros dict, breaking its own `test_attribution_sums_to_one` in that case). However, the fallback creates a confidence-semantics wrinkle — see Finding F2.

4. **Sensor names from `config.SENSOR_NAMES`** (lines 74-76) — Correct. The spec's inline default `["temp", ...]` disagreed with config's `"temperature"`, which would have broken any consumer keying off config names. Constructor also validates baseline width against the names (lines 98-102), which the spec did not.

---

## Findings (by severity)

### F1 — MAJOR (ML guardrail STOP-WORK): No NaN/Inf detection; NaN silently produces a plausible-looking attribution
`src/models/anomaly_attributor.py:149, 219-224, 246-256, 92-107`

`_reconstruction_error` returns whatever MSE the model produces, with no finiteness check, and `_validate_sequence`/`set_baseline` accept NaN/Inf in input arrays (real sensor data can contain NaN; the platform's online-learning pipeline also mutates this model's weights live, so corrupted/diverged weights are a realistic failure mode).

The failure is *silent*, not loud: if `anomaly_error` is NaN, then at line 149:

```python
raw_contributions.append(max(0.0, anomaly_error - masked_error))
```

`max(0.0, nan)` returns `0.0` in CPython (the `nan > 0.0` comparison is False), so every contribution becomes 0.0, the uniform fallback fires, and the caller receives a clean-looking uniform attribution with confidence 0.5 — while `anomaly_error` in the same dict is NaN. This is exactly the "NaN/Inf in model outputs with no detection" STOP-WORK condition, compounded by a silent-failure path.

**Required fix:** raise `ValueError` (or return an explicit error state) when `anomaly_error`, `masked_error`, or `baseline_error` is non-finite — e.g. a `math.isfinite` check in `_reconstruction_error` — and/or reject non-finite values in `_validate_sequence` and `set_baseline` via `np.isfinite(...).all()`. Add tests for NaN input and NaN model output.

### F2 — MODERATE: Confidence has a hard floor of 0.5 and the uniform fallback is indistinguishable from a genuine even split
`src/models/anomaly_attributor.py:157-159, 243`; `src/config.py:247-249`

With K=4 sensors, `max(attribution.values()) >= 0.25` always, so `confidence = min(max * 2, 1.0)` is always in [0.5, 1.0] — "confidence" can never express "low". Worse, the uniform fallback (nothing stands out, contributions all ~0) yields the same `{0.25, 0.25, 0.25, 0.25}` / confidence 0.5 as a genuinely four-way anomaly. A downstream consumer cannot tell "no attributable signal" from "everything contributed equally". `test_should_fall_back_to_uniform_when_all_contributions_zero` (test:273-285) currently pins this 0.5 behavior into the contract. Recommend: add a discriminator to the result (e.g. `"is_fallback": true` or set confidence to 0.0 on fallback) and document the confidence floor. Not blocking — the formula matches the spec — but the fallback deviation introduced this ambiguity and should resolve it.

### F3 — MODERATE: eval-mode guarantee is fragile when the model is shared with the online learner
`src/models/anomaly_attributor.py:69-73, 219-224`

The constructor calls `self.model.eval()` once ("disable dropout permanently"), but it holds a reference to a shared mutable `nn.Module`. The Gap 3 online-learning pipeline fine-tunes the same model in streaming fashion and will call `model.train()`, silently re-enabling dropout (lstm_vae.py:81, 97) for subsequent attributions — breaking the determinism guarantee the module's docstring promises. Recommend asserting/restoring eval mode inside `_reconstruction_error` (e.g. `self.model.eval()` or a `training`-state guard before the no-grad pass). The constructor also has the side effect of moving and eval-ing the caller's model in place (line 71-73), which is worth documenting.

### F4 — MINOR: `get_explanation` can raise unhandled exceptions on malformed input
`src/models/anomaly_attributor.py:208, 210`

`self.sensor_names.index(sensor_name)` raises a bare `ValueError` if an affected-sensor name is not in `sensor_names`, and `attribution_result["attribution"]` raises `KeyError` if the key is absent while `affected_sensors` is non-empty. Both occur only with hand-crafted/mismatched result dicts, but since the method explicitly tolerates partial dicts elsewhere (`.get` at 196, 210), validation is inconsistent. Either validate the result dict at entry or document that it must come from `attribute_sequence`.

### F5 — MINOR: Masking with a constant scalar baseline mean biases contributions downward (method limitation)
`src/models/anomaly_attributor.py:140-149`

Replacing a channel with the scalar mean over all baseline sequences and timesteps flattens its diurnal shape; the LSTM (trained on sinusoidal patterns per config's signal parameters) may itself penalize the constant signal, inflating `masked_error` and under-attributing that sensor. This is inherent to the spec's approach (not a regression), but worth a docstring caveat and a future improvement (mask with the per-timestep baseline mean, `baseline.mean(axis=0)`, which is shape-compatible at line 142).

### F6 — MINOR: Per-call recomputation and sequential forward passes
`src/models/anomaly_attributor.py:142-144, 170-180`

The baseline channel mean is recomputed on every `attribute_sequence` call per sensor (it is constant per baseline — compute once in `set_baseline`), and `attribute_batch` runs `(input_dim + 1)` separate forward passes per sequence instead of batching the masked variants into a single tensor. At current scale (4 sensors, seq_len 20) this is negligible — note only, no premature optimization required.

### F7 — MINOR: Type-hint / test-stub mismatch
`src/models/anomaly_attributor.py:58`; `tests/models/test_anomaly_attributor.py:33-52`

`model: LSTMVAE` is the declared type, but the tests (correctly, for behavioral isolation) pass duck-typed stubs that only implement `encode`/`decode`. The hint is therefore inaccurate as a contract. A small `Protocol` (`encode(...) -> tuple[Tensor, Tensor]`, `decode(...) -> Tensor`) would make the real dependency explicit and legitimize the stubs.

### F8 — MINOR: Org-rule deviations on file structure
`tests/models/test_anomaly_attributor.py` (451 lines) exceeds the 300-line file cap from the global code rules; `anomaly_attributor.py` (298 lines) sits at the cap and contains two classes (`AnomalyAttributor`, `AttributionStrength`), against the one-class-per-file rule. `AttributionStrength` is currently a spec-mandated co-located helper with no other consumers — acceptable for now, but it should move to its own module the moment a second consumer appears or the file grows.

---

## Notes (non-blocking)

- **N1 — ADR consideration:** The deviations are well-documented in code, but if attribution results become an API contract (Week 1 later phases mention inference-pipeline and dashboard integration), the deterministic-scoring decision (deviation 1) deserves a short ADR in `docs/architecture/`, since deterministic `decode(mu)` errors are not numerically comparable to any stochastic anomaly scores produced by the detector's `forward` path — thresholds calibrated on one do not transfer to the other.
- **N2 — Entropy epsilon:** `entropy` filters probabilities at `NUMERICAL_EPSILON` (1e-6, line 297) vs the spec's 1e-10. Marginally more aggressive but mathematically negligible (max discarded entropy contribution ~2e-5 bits per term), and reusing the shared config constant is the right call.
- **N3 — `assert` at line 135:** purely a type-narrowing assert after `_validate_sequence` already raised — safe even under `python -O` since the real guard is the `ValueError`. Fine as written.

## Test Quality Summary

The 36 tests are deterministic (fixed `torch.manual_seed(42)` and `np.random.default_rng` seeds; the deterministic attribution path makes exact-equality assertions valid), independent (fixture-scoped, no shared mutable state), behavior-focused (contract keys, sums-to-one, threshold semantics, error messages via `pytest.raises(match=...)`), and cover edge cases well: empty baseline, wrong dims, missing baseline, empty batch, batch-of-one, zero-sum and one-hot distributions, unnormalized Gini input, and near-zero entropy probabilities. The stub-model technique (test:33-52) to force zero-error and single-channel-error regimes without training is the right way to test attribution math. Gaps to close alongside F1: NaN/Inf inputs, NaN model outputs, and `get_explanation` with a sensor name absent from `sensor_names`.

## Required for re-review

1. F1: finiteness checks in `_reconstruction_error` and input validation (`set_baseline`, `_validate_sequence`) + accompanying tests.

Recommended (non-blocking): F2 fallback discriminator, F3 eval-mode guard in `_reconstruction_error`.

---

## Addendum — Re-review (2026-06-12)

**Updated verdict: PASS WITH NOTES** (supersedes NEEDS FIXES)

Scope: verification of fixes for F1 (required), F2, F3 (recommended) in `src/models/anomaly_attributor.py` and `tests/models/test_anomaly_attributor.py`. Config unchanged, as expected.

### F1 — RESOLVED (NaN/Inf detection)

Three layers of defense, correctly ordered, with no remaining silent path:

- **Input boundaries:** `set_baseline` rejects non-finite baselines (`anomaly_attributor.py:112-116`) and `_validate_sequence` rejects non-finite sequences (`:312-316`). Both checks run *after* the `float32` cast, which also correctly catches float64 values that overflow to Inf on cast.
- **Model-output boundary:** `_reconstruction_error` raises `ValueError` on a non-finite MSE (`:266-271`). Since every forward pass — baseline, anomaly, and all masked variants — routes through this single helper, the previously dangerous `max(0.0, anomaly_error - masked_error)` clamp at `:173` is now unreachable with NaN operands: both errors are guaranteed finite, and masked values are means of a validated-finite baseline.
- **Tests:** parametrized NaN/+Inf/-Inf rejection for baseline (`test:239-247`) and sequence (`test:284-292`), plus model-output corruption both at baseline time (`test:250-256`) and — importantly — *after* a healthy baseline via the runtime-switchable `_NaNDecodeModel` (`test:295-302`), which models exactly the diverged-online-update scenario cited in the original finding.

ML guardrail "NaN/Inf in model outputs with no detection": **now PASS**. The silent-failure WARN item from the universal checklist is also cleared.

### F2 — RESOLVED (fallback discriminator + confidence semantics)

`_normalize_contributions` now returns `(attribution, is_fallback)` (`:277-295`), the result dict carries `is_fallback` (`:196`), and fallback confidence is forced to 0.0 with an explanatory comment (`:181-183`). The confidence floor (`ATTRIBUTION_CONFIDENCE_SCALE / K`, 0.5 for the default config) is now documented in the return-value docstring (`:145-151`). Tests pin both sides of the contract: fallback → `is_fallback=True`, confidence 0.0 (`test:346-360`); genuine attribution → `is_fallback=False` (`test:379-387`); key added to the contract test (`test:317`). Sound.

### F3 — RESOLVED (eval-mode guard)

`_reconstruction_error` re-asserts eval mode when training mode was externally re-enabled (`:260-261`), guarding every reconstruction pass through the single chokepoint; the constructor's in-place mutation of the shared model is now documented under "Side effects" (`:71-77`). Verified by `test_should_restore_eval_mode_after_external_train_call` (`test:206-213`), which calls `model.train()` and asserts both restoration and a successful attribution. Sound.

### Regression check

No regressions found. All pre-existing behavior tests are intact and unmodified in semantics; the only changed assertion is the fallback test, updated to the new (correct) confidence-0.0 contract. New imports (`math`, `Tuple`) are used; type hints and docstrings were updated consistently with the signature change. Determinism guarantees unaffected. Reported gate: 46/46 module tests, 100% module coverage, 93.87% total (not independently re-run; reviewer is read-only — figures are consistent with the visible test surface).

### New minor notes (non-blocking)

- **N4 — Partially-initialized state on baseline failure:** `set_baseline` assigns `self.baseline_sequences` (`:118`) *before* computing `baseline_error` (`:119`). If `_reconstruction_error` raises (now reachable via the F1 check), the attributor is left with a baseline set but `baseline_error = None`; a later `attribute_sequence` against a repaired model would succeed and report `"baseline_error": None`. Trivial fix: compute the error into a local first, then assign both attributes together.
- **N5 — `affected_sensors` in fallback:** in the fallback case the uniform 0.25 contributions all exceed `ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD` (0.15), so `affected_sensors` lists every sensor while `is_fallback=True` says nothing was attributable. Consumers must gate on `is_fallback`; consider emptying `affected_sensors` when the fallback fires, for self-consistency.
- **N6 — File length:** the module grew from 298 to 360 lines, now exceeding the 300-line organizational cap (folds into deferred F8 — moving `AttributionStrength` to its own file would resolve both).

### Deferred (unchanged from original review)

F4 (`get_explanation` robustness on malformed result dicts), F5 (constant-mean masking bias — docstring caveat), F6 (performance: cached baseline means, batched masked forwards), F7 (Protocol for the model dependency), F8 (file-structure org rules). None are guardrail violations.
