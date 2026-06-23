# Coverage Summary — Gap 1 (Vector Embeddings)

**Date:** 2026-06-10
**Command:** `./venv/bin/python -m pytest tests/ --cov=src --cov-fail-under=90`
**Result:** 351 passed, 1 skipped, 1 xfailed — **TOTAL coverage 92.06%** (gate: 90%) ✅

## Gap 1 Modules (new/modified for Vector Embeddings)

| Module | Stmts | Miss | Branch | BrPart | Coverage |
|---|---|---|---|---|---|
| `src/models/anomaly_embedding.py` | 77 | 0 | 16 | 0 | **100.00%** |
| `src/models/embedding_utils.py` | 51 | 0 | 12 | 0 | **100.00%** |
| `src/models/inference.py` (`predict_batch_enriched`) | 51 | 0 | 8 | 0 | **100.00%** |
| `src/api/schemas/sensor_data.py` | 66 | 0 | 8 | 0 | **100.00%** |
| `src/api/routes/inference.py` | 173 | 9 | 54 | 10 | **91.63%** |

Remaining misses in `routes/inference.py` are defensive branches that are
unreachable or impractical to trigger through the public API:
chunk-internal `KeyError`/`ValueError` guards already pre-empted by adapter
normalization (lines 234-235, 241-242), null/out-of-range timestamp guards
inside the sliding-window mapper (264, 268), and the active-model
checkpoint-path line on the upload route (170).

## Test Files (Gap 1 QA)

| File | Tests | Focus |
|---|---|---|
| `tests/models/test_anomaly_embedding.py` | 38 | Shapes, distance/similarity identities, uncertainty, KMeans clustering, decode round-trip, `EnrichedAnomalyResult` serialization |
| `tests/models/test_embedding_utils.py` | 21 | Normalization, statistics/PCA, k-NN search, t-SNE/UMAP projection incl. UMAP-installed and ImportError-fallback branches |
| `tests/api/test_inference_with_embeddings.py` | 11 | Enriched `/api/inference` + `/api/inference/upload` responses, `embedding_dim == LATENT_DIM`, backward compatibility of all pre-Gap-1 fields |
| `tests/api/test_inference_error_paths.py` | 15 | Timestamp back-persistence, commit-failure rollback (500), detector init failure (503), CSV validation failures (422), chunk inference failure (400), short/header-only CSVs, high-anomaly mapping |

## Notes

- VAE scoring is stochastic even in eval mode (reparameterization sampling),
  so all API-level score assertions run against a patched
  `predict_batch_enriched`; encoder `mu` (the embedding) IS deterministic in
  eval mode and is asserted directly.
- All API tests use isolated in-memory `sqlite+aiosqlite` databases — the
  local `greenhouse.db` is unmigrated and never touched.
- Total project coverage moved from 89.21% → 92.06% in one iteration
  (route error paths + UMAP-installed projection branch).
