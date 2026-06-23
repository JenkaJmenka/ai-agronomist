# Anomaly Vector Embeddings — Architecture

**Phase:** 1.9 — Gap 1 (Vector Embeddings) | **Date:** 2026-06-10
**Spec:** `implementation plan/Phase 1.9/IMPLEMENTATION_GAP_1_VECTOR_EMBEDDINGS.md`
**Status:** Implemented (ml-engineer + backend-engineer), QA-verified at 92.06% total coverage.

---

## 1. Overview

Anomaly detection previously produced a single scalar score per sensor sequence
(reconstruction MSE, clipped to `[0, 1]`). A scalar tells you *how* anomalous a
sequence is but nothing about *what kind* of anomaly it is.

Gap 1 enriches every inference result with an **8-dimensional vector embedding**
extracted from the LSTM-VAE latent space. Two anomalies caused by the same
physical failure mode (e.g. boiler failure vs. ventilation failure) land near
each other in this space, enabling:

- **Similarity search** — "find past anomalies like this one" (foundation for
  Gap 2, pgvector incident memory).
- **Clustering of failure types** — KMeans over historical embeddings.
- **Few-shot learning** — label a handful of incidents, classify new ones by
  nearest-neighbor in embedding space.

## 2. Design: embedding = encoder `mu`

The LSTM-VAE encoder maps a sequence `x ∈ ℝ^(20×4)` to a latent Gaussian
`N(μ, diag(σ²))` with `μ, logvar ∈ ℝ^8` (`LATENT_DIM = 8` in `src/config.py`).

**The embedding is the `mu` vector** — the mean of the latent posterior:

```
x (20×4 sensor window) ──encoder──► (mu, logvar) ∈ ℝ^8 × ℝ^8
                                      │
                                      ├── mu        → EMBEDDING (deterministic)
                                      ├── logvar    → UNCERTAINTY (spread)
                                      └── z=mu+σ⊙ε  → decoder → reconstruction → SCORE (stochastic)
```

### Why `mu` and not `z`?

- `z = mu + σ ⊙ ε` is **sampled** — the same input would yield a different
  vector on every call, which breaks similarity search and caching.
- `mu` is **deterministic in eval mode**: it depends only on the input and the
  weights (dropout is disabled by `model.eval()`). Two calls on the same
  sequence produce identical embeddings — tests assert this directly.
- The KL term in the β-VAE loss (β = 0.1) regularizes the posterior toward
  `N(0, I)`, keeping the latent space smooth so distances between `mu` vectors
  are semantically meaningful.

### Uncertainty

`logvar` quantifies how diffuse the posterior is — i.e. how unsure the encoder
is about where the sequence belongs in latent space. **Two conventions exist in
the codebase (intentional, per task instruction — be aware when comparing
values):**

| Layer | Formula | Semantics |
|---|---|---|
| `AnomalyEmbedding.embed_with_uncertainty` | `mean(exp(0.5·logvar))` | mean **std** |
| `AnomalyDetector.predict_batch_enriched` (API path) | `mean(exp(logvar))` | mean **variance** |

Variance = std², so API uncertainties are not directly comparable to
model-layer uncertainties. `EnrichedAnomalyResult.is_confident` compares the
std-based value against `EMBEDDING_CONFIDENCE_THRESHOLD = 0.15` (config).

## 3. Components

```
src/models/
├── anomaly_embedding.py    AnomalyEmbedding (embed_sequence/embed_batch/
│                           embed_with_uncertainty/embedding_distance/
│                           embedding_similarity/cluster_embeddings/
│                           decode_embedding) + EnrichedAnomalyResult dataclass
├── embedding_utils.py      Pure numpy/sklearn helpers: normalize_embeddings,
│                           embedding_statistics, find_nearest_embeddings,
│                           embedding_projection_2d (t-SNE / UMAP w/ fallback)
└── inference.py            AnomalyDetector.predict_batch_enriched()
                            → (scores, embeddings, uncertainties), ONE forward pass

src/api/
├── schemas/sensor_data.py  InferenceResponse + Optional embeddings /
│                           uncertainties / embedding_dim fields
└── routes/inference.py     /api/inference and /api/inference/upload now call
                            predict_batch_enriched once per batch/chunk
```

Dependency direction is strictly `api → models → config`; the model layer has
no API imports. The API layer consumes only the `AnomalyDetector` facade — it
never touches `LSTMVAE` internals.

### Single forward pass

`predict_batch_enriched` runs the model **once** and reuses the `mu`/`logvar`
the encoder already produced for the reconstruction:

```python
reconstructed, mu, logvar, _ = self.model(sequences_tensor)
scores        = ((sequences_tensor - reconstructed) ** 2).mean(dim=[1, 2])
uncertainties = torch.exp(logvar).mean(dim=1)
return scores.numpy(), mu.numpy(), uncertainties.numpy()
```

Embedding extraction therefore adds ~0 inference cost. As a side effect of this
change, a pre-existing bug where `/api/inference` called `predict_batch` twice
(doubling inference cost) was fixed.

### Spec deviations (deliberate)

| Spec said | Implemented | Why |
|---|---|---|
| Separate `AnomalyDetectorWithEmbeddings` class | Method on existing `AnomalyDetector` | Task override; avoids a parallel detector and duplicate model cache |
| `model_id`/`customer_id` response fields | Existing `tenant_id`/`greenhouse_id` kept | Spec schema didn't match the real codebase schema |
| `decode` input reshaped to `(batch, 1, latent)` | `(batch, latent)` passed directly | This codebase's `LSTMVAE.decode` expands the seq axis internally |
| `datetime.utcnow()` | `datetime.now(timezone.utc)` | `utcnow()` is deprecated |
| `KMeans(random_state=42)` literal | `RANDOM_SEED` from config + `n_init="auto"` | No magic numbers |

## 4. API contract

Both routes return the enriched fields. They are `Optional` in
`InferenceResponse`, so the change is **backward compatible** — pre-Gap-1
clients see identical existing fields and may ignore the new ones.

### Request — `POST /api/inference`

```json
{
  "tenant_id": "tenant_1",
  "greenhouse_id": "default",
  "sequences": [
    [[24.5, 60.1, 410.0, 800.0], "... 20 timesteps × [temp, humidity, co2, light] ..."]
  ],
  "timestamps": ["2026-06-10T09:00:00Z"]
}
```

`sequences` shape is validated as `(batch, SEQ_LEN=20, INPUT_DIM=4)`;
`timestamps` is optional and used for exact score back-persistence.

### Response — `200 OK`

```json
{
  "tenant_id": "tenant_1",
  "greenhouse_id": "default",
  "sequences_processed": 1,
  "anomalies_detected": 1,
  "anomaly_scores": [0.91],
  "high_anomalies": [
    {"sequence_index": 0, "score": 0.91, "timestamp": "2026-06-10T09:00:01.123456+00:00"}
  ],
  "embeddings": [[0.12, -0.43, 0.88, -0.05, 0.31, -0.77, 0.02, 0.55]],
  "uncertainties": [0.084],
  "embedding_dim": 8
}
```

- `embeddings[i]` is the encoder `mu` for `sequences[i]` (length =
  `embedding_dim` = `LATENT_DIM` from config).
- `uncertainties[i]` = mean of `exp(logvar)` (variance convention, see §2).
- Embeddings are returned for **every** sequence, not just anomalous ones —
  callers (Gap 2) decide which ones to persist.

### `POST /api/inference/upload` (CSV route)

`multipart/form-data` with `tenant_id`, `greenhouse_id`, `file` (.csv with
`timestamp,temperature,humidity,co2,light`). The CSV is processed in 5000-row
chunks with a `seq_len − 1` overlap between chunks; per-chunk embeddings and
uncertainties are accumulated and returned in the same shape as above. With
fewer than 20 valid rows, the route returns `200` with
`sequences_processed: 0` and empty arrays.

Error envelope (custom handler in `src/api/main.py`): non-422 errors return
`{"message": ..., "request_id": ...}` — **not** FastAPI's default
`{"detail": ...}`. 422 validation errors carry both `detail` and `message`.

## 5. Usage examples

### Embed and compare two incidents

```python
from src.models.anomaly_embedding import AnomalyEmbedding
from src.models.inference import AnomalyDetector

detector = AnomalyDetector(checkpoint_path="models/baseline.pt")
embedder = AnomalyEmbedding(detector.model, device="cpu")

emb_today = embedder.embed_sequence(today_window)        # (8,)
emb_may15 = embedder.embed_sequence(may15_boiler_window)  # (8,)

similarity = embedder.embedding_similarity(emb_today, emb_may15)
if similarity > 0.7:
    print("This looks like the May 15 boiler failure")
```

### Cluster historical anomalies into failure types

```python
scores, embeddings, uncertainties = detector.predict_batch_enriched(past_anomaly_windows)
clusters = embedder.cluster_embeddings(embeddings, n_clusters=5)
# clusters["labels"][i] → failure-type id of anomaly i
prototype = embedder.decode_embedding(clusters["cluster_centers"][0])  # (20, 4)
```

### Nearest-neighbor incident lookup

```python
from src.models.embedding_utils import find_nearest_embeddings

indices, distances = find_nearest_embeddings(query_embedding, incident_db_embeddings, k=5)
```

### 2-D visualization

```python
from src.models.embedding_utils import embedding_projection_2d

coords = embedding_projection_2d(embeddings, method="umap")  # t-SNE fallback if umap-learn missing
```

## 6. Known limitations

1. **Stochastic anomaly scores.** `model.eval()` disables dropout but **not**
   reparameterization sampling — `z = mu + σ⊙ε` is drawn fresh each forward
   pass, so two calls on the same input give slightly different reconstruction
   scores (~1e-3). Embeddings (`mu`) ARE deterministic. Tests must never assert
   exact score equality against the real model; they mock
   `predict_batch_enriched` instead.
2. **Single forward pass per uncertainty estimate.** Uncertainty comes from one
   encoder pass (`logvar`); it is not an MC-sampled epistemic estimate. It
   captures posterior spread, not model-weight uncertainty.
3. **Dual uncertainty conventions.** std (model layer) vs. variance (API
   layer) — see §2. Do not mix values across layers without converting.
4. **Untrained-model embeddings are meaningless.** Semantic neighborhood
   structure only emerges after training; the test suite uses fresh-weight
   models for shape/contract checks only.
5. **CSV-route memory growth.** Raw CSV parsing is chunked, but embeddings for
   *all* sequences are accumulated in memory and returned in one JSON response.
   A very large CSV (~1M rows) produces ~1M × 8 floats in the response. Gap 2
   should persist embeddings server-side instead of returning all of them.
6. **No embedding persistence yet.** Embeddings live only in the response; the
   pgvector incident memory (Gap 2) will store them.
7. **t-SNE/UMAP projections are for visualization only** — non-parametric,
   not stable across runs/batches; never use projected coordinates for
   similarity decisions.

## 7. Configuration (all in `src/config.py`)

| Constant | Value | Used by |
|---|---|---|
| `LATENT_DIM` | 8 | embedding dimensionality, `embedding_dim` response field |
| `EMBEDDING_CONFIDENCE_THRESHOLD` | 0.15 | `EnrichedAnomalyResult.is_confident` |
| `DEFAULT_EMBEDDING_CLUSTERS` | 5 | `cluster_embeddings` default |
| `DEFAULT_NEAREST_NEIGHBORS` | 5 | `find_nearest_embeddings` default |
| `PCA_VARIANCE_TARGET` | 0.95 | `embedding_statistics.effective_dims` |
| `PROJECTION_COMPONENTS_2D` | 2 | t-SNE/UMAP output dims |
| `RANDOM_SEED` | 42 | KMeans, t-SNE, UMAP reproducibility |

Dependency note: `scipy==1.15.3` is now pinned in `requirements.txt` (was only
transitive) — used for cosine/euclidean distances. `umap-learn` is optional;
its absence triggers a logged t-SNE fallback.
