# Code Review — Gap 1: Vector Embeddings (Phase 1.9)

**Reviewer:** code-reviewer | **Date:** 2026-06-10
**Spec:** `implementation plan/Phase 1.9/IMPLEMENTATION_GAP_1_VECTOR_EMBEDDINGS.md`

## VERDICT: ✅ PASS WITH NOTES

No STOP-WORK condition is present. All 351 tests pass (1 skipped, 1 xfailed) at
**92.06% total branch coverage — independently re-run and confirmed by this
review**, not taken from the QA report. The modified pre-existing tests were
verified behavior-preserving (details in §4). The notes below are WARN-level;
none block Gap 2.

### Scope reviewed

| File | Author | Status |
|---|---|---|
| `src/models/anomaly_embedding.py` (new) | ml-engineer | ✅ clean |
| `src/models/embedding_utils.py` (new) | ml-engineer | ✅ clean (2 low-severity edge cases) |
| `src/models/inference.py` (`predict_batch_enriched`) | backend-engineer | ✅ clean |
| `src/api/schemas/sensor_data.py` | backend-engineer | ✅ clean |
| `src/api/routes/inference.py` | backend-engineer | ⚠️ WARN notes (mostly pre-existing) |
| `src/api/routes/upload.py` | backend-engineer | ✅ no functional Gap-1 change observed |
| `src/config.py` (embedding constants) | ml-engineer | ✅ clean |
| `requirements.txt` (scipy pin) | ml-engineer | ✅ compatible |
| 5 new test files + 2 adapted test files | qa/backend | ✅ verified (§4) |

---

## 1. STOP-WORK checklist (`.claude/guardrails/universal-guardrails.md`)

| Check | Result | Evidence |
|---|---|---|
| Tight coupling LSTM-VAE ↔ API layer | ✅ PASS | Dependency direction is strictly `api → models → config`. Routes import only the `AnomalyDetector` facade; no `LSTMVAE` internals touched in API code; `src/models/` has zero `src/api` imports (grep-verified). `anomaly_embedding.py`/`embedding_utils.py` import only torch/numpy/sklearn/scipy + config. |
| Secrets / API keys hardcoded | ✅ PASS | Grep over `src/` for key/secret/password/token literal assignments: no matches. |
| Model checkpoint without metadata | ✅ PASS | Production path `LSTMVAE.save_checkpoint` persists epoch, loss, accuracy, timestamp, model_config (lstm_vae.py:254-262). Gap 1 introduces no new checkpoint writer. (Note: the `test_inference_enriched.py` fixture writes a minimal `{model_config, state_dict}` checkpoint — acceptable for a test, see §5.11.) |
| DB schema without tenant_id | ✅ PASS | No schema change in Gap 1; all route queries filter `tenant_id` (routes/inference.py:91-94, 106-109). |
| Test coverage <80% on critical paths | ✅ PASS | **Independently verified: `pytest tests/ --cov=src` → 351 passed, 1 skipped, 1 xfailed, TOTAL 92.06%** (branch coverage enabled in `.coveragerc`). Gap-1 modules: anomaly_embedding 100%, embedding_utils 100%, models/inference 100%, schemas 100%, routes/inference 91.63%. |
| Major design change without ADR | ✅ PASS | Additive feature implemented per the Phase 1.9 spec doc; locked LSTM-VAE architecture untouched; `docs/embeddings_architecture.md` created with this review. |

## 2. ML guardrails (`.claude/guardrails/ml-guardrails.md`)

| Check | Result | Evidence |
|---|---|---|
| LSTM-VAE spec locked (4/[64,32]/8/20/β0.1/d0.2) | ✅ PASS | `src/models/lstm_vae.py` unmodified; all defaults flow from config. |
| Checkpoint metadata (epoch, loss, accuracy, timestamp, config) | ✅ PASS | See §1. |
| NaN/Inf in model outputs with no detection | ✅ PASS | `predict_batch_enriched` calls `_validate_batch` (NaN/Inf rejection on input, inference.py:56-57); tests cover NaN, Inf, and 2-D rejection. NaN-guarded timestamp mapping on the CSV route. |
| Training loop / early stopping | N/A | No training-path change in Gap 1. |
| Hard-coded sensor ranges | ✅ PASS | Route uses `SENSOR_RANGES` from `data_utils`, which is composed from `TEMP_RANGE`/`HUMIDITY_RANGE`/`CO2_RANGE`/`LIGHT_RANGE` in `src/config.py`. |

## 3. Red flags (`.claude/guardrails/red-flags.md`)

| # | Check | Result |
|---|---|---|
| 1 | File written outside ownership | ⚠️ NOTE — backend-engineer modified `src/models/inference.py`, which the CLAUDE.md ownership table assigns to ml-engineer. This was explicitly directed by the spec (STEP 3) and the task override, so it is a sanctioned process exception, not a violation — recorded for the audit trail. |
| 2 | `os.system` / `subprocess shell=True` / `eval()` | ✅ PASS — grep matches are only `model.eval()` and list-arg `subprocess.run` in a pre-existing formatting hook. |
| 3 | Hardcoded password/token/key | ✅ PASS |
| 4 | DELETE/DROP without tenant_id | ✅ PASS — no DELETE/DROP statements; score updates are tenant+greenhouse filtered. |
| 5 | `model.save()` without metadata JSON | ✅ PASS — no new save path. |
| 6 | Test file importing AND modifying implementation | ✅ PASS — all new tests are read-only consumers; the only mutation is popping the detector's own temp-path cache entry for isolation (legitimate). |
| 7 | Circular imports `src/models/` ↔ `src/api/` | ✅ PASS — one-directional. |

## 4. SPECIFIC SCRUTINY — modified pre-existing tests

**Caveat:** the repository has only an initial commit and `tests/` is untracked
in git, so a literal diff against the pre-change files was impossible.
Verification was done by (a) logical reconstruction of the old assertions
against the unchanged route contract and (b) independent re-execution
(113/113 pass across all five API/integration/model test files).

### 4.1 `tests/api/test_inference.py` + `tests/integration/test_end_to_end.py` (backend-engineer)

**Verdict: behavior-preserving; assertions NOT weakened.**

- The route's score contract is unchanged: `batch_scores = clip(raw / ANOMALY_SCALING_FACTOR=2.0, 0, 1)`.
  The new `make_enriched_side_effect(score_fn)` wrapper passes the raw-score
  generator through **unmodified** and only pads the tuple with zero embeddings
  and constant 0.1 uncertainties — values no pre-existing assertion depends on.
- Every score expectation checks out against the same arithmetic the old
  `predict_batch` mocks exercised: 0.3→0.15, `linspace(0,2)`→clip→[0,1] with
  `min==0.0`/`max==1.0`, 1.44→0.72 (DB back-persistence check), 1.9→0.95
  (high-anomaly, `anomalies_detected == 1`, `sequence_index == 0`), and
  1.5→0.75 in e2e (with strict `len(scored_records) == 10` and per-record
  `approx(0.75)`).
- All assertions remain exact-value `pytest.approx` / strict `==` on status
  codes and counts — no tolerance widening, no removed assertions, no
  `in`-loosening detected anywhere in either file.
- Patch target `src.models.inference.AnomalyDetector.predict_batch_enriched`
  is correct: the routes no longer call `predict_batch` at all, and patching
  the class method covers the `lru_cache`d detector instances.
- The e2e DB-persistence and multi-tenant-isolation assertions are intact and
  still query the database directly (not just the response body).

### 4.2 `tests/api/test_inference_error_paths.py` — `detail` → `message` (qa-engineer)

**Verdict: correct, and status codes remain strictly asserted.**

- `src/api/main.py` registers a custom `StarletteHTTPException` handler
  (lines 163-180) returning `{"message": exc.detail, "request_id": ...}` —
  FastAPI's default `{"detail": ...}` envelope does **not** apply to
  route-raised `HTTPException`s in this app. Asserting `response.json()["message"]`
  is the only correct form; the old `detail` form would KeyError.
- 422 *validation* errors (RequestValidationError handler, lines 141-160)
  return **both** `detail` and `message`, so the surviving `"detail" in
  response.json()` checks in `test_inference.py` (lines 160, 171) remain valid.
- Every one of the 15 tests asserts an exact status code (`== 200/400/422/500/503`),
  and most additionally assert message substrings (`"persist"`, `"Inference
  failed"`, `"Invalid CSV format"`, `"no valid data"`, `"chunk"`) plus
  side-effects (`rollback_mock.assert_awaited_once()`). Not weakened —
  these are stronger than typical error-path tests.

## 5. WARN findings (log and continue)

Ordered by severity. Items marked *(pre-existing)* were not introduced by
Gap 1 but live in files touched today.

1. **Hard-coded `seq_len = 20` and `chunk_size = 5000` in
   `routes/inference.py:187-188`** *(pre-existing)*. `SEQ_LEN` exists in
   `src/config.py` and is even used by the schema validator in the same
   request path; the route should import it (the new code did this correctly
   for `LATENT_DIM`). `chunk_size = 5000` is additionally duplicated in
   `routes/upload.py:44` — guardrail "config values in one place".
2. **Silent `except Exception: pass` in timestamp parsing**
   (`routes/inference.py:85-88`) *(pre-existing)* — malformed timestamps are
   dropped without logging, violating the "silent failures" WARN guardrail.
   QA's error-path tests document the behavior but the route should at least
   `logger.warning` the skipped value.
3. **Internal exception text leaked to clients**: `detail=f"Inference failed:
   {str(e)}"`, `f"Failed to initialize model: {e}"` (includes checkpoint
   paths), `f"Failed to persist anomaly scores: {str(e)}"` *(pre-existing
   pattern, extended to the new enriched call sites)*. Guardrail: user-facing
   errors should be friendly; internals belong in structured logs (the
   request-ID middleware already logs them). Recommend generic client
   messages keyed by `request_id`.
4. **Dual uncertainty conventions** — model layer returns mean **std**
   (`exp(0.5·logvar)`), API path returns mean **variance** (`exp(logvar)`),
   per explicit task instruction. Both are individually correct and
   documented (AGENT_MEMORY + architecture doc), but this is a latent
   footgun once Gap 2 starts persisting/comparing uncertainties across
   layers. Recommend unifying on std (or naming the API field
   `latent_variance`) before pgvector storage freezes the contract.
5. **`/api/inference/upload` returns an untyped dict** — no `response_model`
   on the decorator, and its `high_anomalies[].timestamp` is a string
   (possibly `"unknown"`), diverging from the typed `HighAnomalyDetail`
   (datetime) used by `/api/inference`. The new enriched fields are therefore
   schema-validated on one route but not the other. *(pre-existing decorator
   gap; the new fields inherit it.)*
6. **Unbounded response growth on the CSV route** — raw parsing is chunked,
   but per-sequence embeddings/uncertainties for the *entire file* accumulate
   in Python lists and ship in one JSON body (~1M rows → ~1M×8 floats). This
   partially undermines the memory-bounded chunking design. Acceptable until
   Gap 2; flagged as a known limitation in the architecture doc.
7. **`torch.load(..., weights_only=False)`** (`inference.py:39`)
   *(pre-existing)* — pickle deserialization is arbitrary-code-execution if a
   checkpoint is ever attacker-supplied. Checkpoints are internal artifacts
   today; revisit when multi-tenant custom-model upload lands.
8. **Edge cases in `embedding_utils` / `anomaly_embedding`** (low):
   `embedding_statistics` divides by `singular_values.sum()` — a constant
   embedding cloud (all-identical rows) yields 0/0 → NaN; scipy `cosine`
   on a zero vector also yields NaN unguarded. Neither is reachable from the
   API path today (embeddings come from a real encoder), but Gap 2 helpers
   should guard them.
9. **`AnomalyDetector._model_cache` is unbounded** *(pre-existing)* — the
   route-level `lru_cache(maxsize=16)` bounds detector instances, but the
   class-level dict keyed by checkpoint path never evicts; direct
   constructions (e.g. training pipelines) accumulate. Also
   `raise RuntimeError(...)` in `_load_model` lacks `from e` (loses the
   exception chain).
10. **Minor test nits**: `test_inference_error_paths.py:38` hardcodes
    embedding dim `8` instead of `LATENT_DIM`; `import pytest_asyncio` sits
    above the module docstring in `test_inference.py` / `test_end_to_end.py`
    (demotes the docstring to a no-op string) *(pre-existing)*.
11. **Test checkpoint fixture is minimal** — `test_inference_enriched.py`
    saves `{model_config, model_state_dict}` without epoch/loss/timestamp.
    Fine for a unit fixture (and `_load_model` tolerates it), but using
    `save_checkpoint()` would also exercise the production write path.
12. **Dependency drift** *(pre-existing)*: `pyproject.toml` pins
    fastapi 0.104.1 / pydantic 2.5.2 while `requirements.txt` (the active
    manifest) pins fastapi 0.109.0 / pydantic 2.5.0. The new
    `scipy==1.15.3` pin itself is compatible with numpy 1.24.3 /
    scikit-learn 1.2.2 and matches the venv.

## 6. Per-dimension assessment

- **Security:** ✅ No new attack surface. Input validated at every boundary
  (Pydantic shape validator, `_validate_batch` NaN/Inf, dimensionality guards
  in `AnomalyEmbedding`). Tenant filtering intact. WARNs #3/#7 are hardening
  items, not vulnerabilities in the current deployment model.
- **Correctness:** ✅ `predict_batch_enriched` is mathematically consistent
  with `predict` (identical MSE reduction, verified by
  `TestExistingContractsUnchanged`); embedding = encoder `mu` is the right
  deterministic choice (vs. sampled `z`); `decode_embedding` correctly adapted
  to this codebase's `(batch, latent_dim)` decode contract, fixing a wrong
  reshape in the spec. The fix of the duplicated `predict_batch` call removes
  a real 2× inference cost bug. Stochastic-score handling (mock, never exact
  equality) is correct and consistently applied.
- **Performance:** ✅ Single forward pass — embeddings come at ~zero marginal
  cost; net change is a 2× *speedup* of `/api/inference` due to the
  double-call fix. WARN #6 (CSV response accumulation) is the only scaling
  concern. The O(records × timestamps) back-persistence matching loop is fine
  at current batch sizes.
- **Type hints:** ✅ New model code fully annotated with explicit return
  types, including the `Tuple[np.ndarray, np.ndarray, np.ndarray]` enriched
  contract; schemas fully typed with field constraints. `Dict[str, object]`
  returns would be stronger as TypedDicts, and WARN #5 leaves one route
  response untyped — notes, not blockers.
- **Test quality:** ✅ Strong. 85 Gap-1 tests: deterministic seeded RNGs,
  one assertion concept per test, descriptive names, independent fixtures
  (cache cleanup, isolated in-memory DBs), real edge cases (NaN/Inf, empty
  candidates, zero rows, k>n clamping, fewer-points-than-clusters,
  header-only CSV, sub-window CSV, commit failure + rollback verification),
  backward-compatibility suite, and the UMAP installed/missing branches via
  `sys.modules` stubbing. Mock migration verified behavior-preserving (§4).

## 7. Verification performed

```
./venv/bin/python -m pytest <5 gap-1 + 2 adapted files>  → 113 passed (5.84s)
./venv/bin/python -m pytest tests/ --cov=src             → 351 passed, 1 skipped,
                                                            1 xfailed, TOTAL 92.06%
```

Plus: guardrail greps (secrets / eval / subprocess / SENSOR_RANGES origin),
import-direction check between `src/models/` and `src/api/`, manual contract
trace of `LSTMVAE.encode/decode` against the new callers, and exception-handler
verification in `src/api/main.py`.

## 8. Recommendations (non-blocking, for tech-lead / Gap 2)

1. Unify the uncertainty convention (WARN #4) **before** Gap 2 persists values into pgvector.
2. Import `SEQ_LEN` in `routes/inference.py` and centralize `chunk_size` (WARN #1).
3. Add `response_model` to `/api/inference/upload` (WARN #5).
4. Replace client-facing `str(e)` with generic messages + request-ID logging (WARN #3).
5. In Gap 2, persist embeddings server-side rather than returning all of them on the CSV route (WARN #6).
