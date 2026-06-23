# IMPLEMENTATION PLAN: Gap 1 - Vector Embeddings for Anomalies
## Timeline: 2 days | Priority: TIER 1 - CRITICAL

---

## 🎯 OBJECTIVE
Extract 8-dimensional vector embeddings from LSTM-VAE latent space to enable semantic anomaly comparison.

**Problem Solved:**
- Currently: Anomalies are scalar scores [0-1] with no semantic meaning
- After: Anomalies are 8-dim vectors enabling similarity search, clustering, few-shot learning

---

## 📊 IMPACT
- ✅ Enables "find similar past anomalies" feature
- ✅ Foundation for pgvector incident memory
- ✅ Powers few-shot learning in Phase 1.5 Week 2
- ✅ Semantic clustering of failure types

---

## 🏗️ FILE STRUCTURE

```
src/models/
├── anomaly_embedding.py          ⭐ NEW - Main implementation
├── embedding_utils.py             ⭐ NEW - Utility functions
├── lstm_vae.py                    (existing - no changes needed)
└── inference.py                   (update to use embeddings)

src/api/
└── routes/inference.py            (update response schema)

tests/
├── models/test_anomaly_embedding.py  ⭐ NEW
└── api/test_inference_with_embeddings.py ⭐ NEW

docs/
└── embeddings_architecture.md     ⭐ NEW - Design documentation
```

---

## 🔧 IMPLEMENTATION PLAN

### STEP 1: Create `src/models/anomaly_embedding.py`

```python
"""
Anomaly Embedding Module: Extract vector representations from LSTM-VAE latent space.

Core concept:
- Use VAE encoder to get latent distribution (mu, logvar)
- μ (mu) = 8-dimensional point in latent space
- This 8-dim vector is the EMBEDDING of the anomaly
- Enable similarity search: cos_distance(embedding_1, embedding_2)
"""

import numpy as np
import torch
import torch.nn.functional as F
from typing import Dict, Tuple, List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class AnomalyEmbedding:
    """
    Extract and manage anomaly embeddings from LSTM-VAE latent space.
    
    Embeddings capture the "essence" of an anomaly in 8 dimensions:
    - Captures complex patterns learned by encoder
    - Enables semantic similarity: "this anomaly is like the May 15 boiler failure"
    - Foundation for few-shot learning and incident memory
    """
    
    def __init__(self, model, device: str = "cpu"):
        """
        Args:
            model: Trained LSTMVAE instance
            device: "cpu" or "cuda"
        """
        self.model = model.to(device)
        self.model.eval()  # No gradients during inference
        self.device = torch.device(device)
        
        # Embedding dimension (from LATENT_DIM in config)
        self.embedding_dim = model.latent_dim
    
    # =========================================================================
    # CORE METHODS: Extract embeddings
    # =========================================================================
    
    def embed_sequence(self, sequence: np.ndarray) -> np.ndarray:
        """
        Extract embedding for a single sequence.
        
        Args:
            sequence: Shape (seq_len, input_dim=4) or (1, seq_len, 4) if batch
        
        Returns:
            embedding: Shape (latent_dim=8,) - the 8-dimensional embedding
        
        Example:
            >>> seq = np.random.rand(20, 4)  # 20 timesteps, 4 sensors
            >>> embedding = embedder.embed_sequence(seq)
            >>> embedding.shape
            (8,)
        """
        # Handle input shapes
        if sequence.ndim == 2:
            sequence = sequence[np.newaxis, :]  # (seq_len, 4) → (1, seq_len, 4)
        
        # Convert to tensor
        x = torch.tensor(sequence, dtype=torch.float32).to(self.device)
        
        with torch.no_grad():
            mu, logvar = self.model.encode(x)  # (batch, latent_dim)
            
        return mu[0].cpu().numpy()  # Return first (and only) batch item
    
    def embed_batch(self, sequences: np.ndarray) -> np.ndarray:
        """
        Extract embeddings for multiple sequences (batch).
        
        Args:
            sequences: Shape (batch_size, seq_len, input_dim)
        
        Returns:
            embeddings: Shape (batch_size, latent_dim)
        
        Example:
            >>> batch = np.random.rand(100, 20, 4)  # 100 sequences
            >>> embeddings = embedder.embed_batch(batch)
            >>> embeddings.shape
            (100, 8)
        """
        x = torch.tensor(sequences, dtype=torch.float32).to(self.device)
        
        with torch.no_grad():
            mu, logvar = self.model.encode(x)
            
        return mu.cpu().numpy()  # (batch, latent_dim)
    
    def embed_with_uncertainty(self, sequence: np.ndarray) -> Dict:
        """
        Extract embedding + uncertainty estimate.
        
        Uncertainty = spread of the learned latent distribution.
        High uncertainty = model is unsure about this anomaly type.
        
        Args:
            sequence: Shape (seq_len, 4) or (1, seq_len, 4)
        
        Returns:
            Dict with:
                - embedding: (latent_dim,)
                - uncertainty: float (standard deviation in latent space)
                - mu: (latent_dim,) - mean of distribution
                - logvar: (latent_dim,) - log variance
        
        Example:
            >>> result = embedder.embed_with_uncertainty(seq)
            >>> result['embedding'].shape, result['uncertainty']
            ((8,), 0.234)  # High uncertainty = 0.234 std
        """
        if sequence.ndim == 2:
            sequence = sequence[np.newaxis, :]
        
        x = torch.tensor(sequence, dtype=torch.float32).to(self.device)
        
        with torch.no_grad():
            mu, logvar = self.model.encode(x)
        
        mu_np = mu[0].cpu().numpy()
        logvar_np = logvar[0].cpu().numpy()
        
        # Uncertainty = average standard deviation across latent dims
        std = np.exp(0.5 * logvar_np)  # exp(0.5 * logvar) = std
        uncertainty = float(std.mean())
        
        return {
            "embedding": mu_np,
            "uncertainty": uncertainty,
            "mu": mu_np,
            "logvar": logvar_np,
            "std": std
        }
    
    # =========================================================================
    # ANALYSIS: Interpret embeddings
    # =========================================================================
    
    def embedding_distance(self, 
                           embedding_1: np.ndarray, 
                           embedding_2: np.ndarray,
                           metric: str = "cosine") -> float:
        """
        Compute distance between two embeddings.
        
        Args:
            embedding_1: Shape (latent_dim,)
            embedding_2: Shape (latent_dim,)
            metric: "cosine" (default) or "euclidean"
        
        Returns:
            distance: float in [0, 1] for cosine, [0, ∞) for euclidean
        
        Interpretation:
            - cosine distance: 0 = identical direction, 1 = opposite
            - euclidean: 0 = identical, larger = more different
        
        Example:
            >>> emb1 = np.array([0.1, 0.2, ...])  # 8-dim
            >>> emb2 = np.array([0.15, 0.22, ...])  # Similar anomaly
            >>> dist = embedder.embedding_distance(emb1, emb2)
            >>> dist  # Small distance = similar anomalies
            0.023
        """
        from scipy.spatial.distance import cosine, euclidean
        
        if metric == "cosine":
            return cosine(embedding_1, embedding_2)
        elif metric == "euclidean":
            return euclidean(embedding_1, embedding_2)
        else:
            raise ValueError(f"Unknown metric: {metric}")
    
    def embedding_similarity(self, 
                            embedding_1: np.ndarray,
                            embedding_2: np.ndarray) -> float:
        """
        Compute similarity (inverse of cosine distance).
        
        Args:
            embedding_1, embedding_2: Shape (latent_dim,)
        
        Returns:
            similarity: float in [0, 1], where 1 = identical
        
        Example:
            >>> sim = embedder.embedding_similarity(emb1, emb2)
            >>> sim
            0.977  # Very similar!
        """
        distance = self.embedding_distance(embedding_1, embedding_2, metric="cosine")
        return 1.0 - distance  # Convert distance → similarity
    
    def cluster_embeddings(self, embeddings: np.ndarray, n_clusters: int = 5) -> Dict:
        """
        Cluster embeddings to find anomaly types.
        
        Args:
            embeddings: Shape (n_anomalies, latent_dim)
            n_clusters: Number of anomaly types to find
        
        Returns:
            Dict with:
                - cluster_centers: (n_clusters, latent_dim)
                - labels: (n_anomalies,) - cluster assignment for each
                - inertia: sum of squared distances within clusters
        
        Example:
            >>> embeddings = np.random.rand(1000, 8)  # 1000 past anomalies
            >>> clusters = embedder.cluster_embeddings(embeddings, n_clusters=5)
            >>> clusters['labels']  # Which cluster is each anomaly?
            [0, 0, 2, 1, 0, ...]
            
        Interpretation:
            - Cluster 0 = boiler failures
            - Cluster 1 = ventilation failures
            - Cluster 2 = CO2 supply failures
            - etc.
        """
        from sklearn.cluster import KMeans
        
        kmeans = KMeans(n_clusters=n_clusters, random_state=42)
        labels = kmeans.fit_predict(embeddings)
        
        return {
            "cluster_centers": kmeans.cluster_centers_,
            "labels": labels,
            "inertia": kmeans.inertia_,
            "n_clusters": n_clusters
        }
    
    # =========================================================================
    # RECONSTRUCTION: From embedding back to sequence
    # =========================================================================
    
    def decode_embedding(self, embedding: np.ndarray) -> np.ndarray:
        """
        Reconstruct sequence from embedding (inverse of embed_sequence).
        
        Args:
            embedding: Shape (latent_dim,) or (1, latent_dim)
        
        Returns:
            reconstructed: Shape (seq_len, input_dim)
        
        Note:
            This is the "prototype" sequence for this anomaly type.
            Useful for visualization and understanding what the model sees.
        
        Example:
            >>> embedding = np.array([0.1, -0.2, ..., 0.05])  # 8-dim
            >>> prototype = embedder.decode_embedding(embedding)
            >>> prototype.shape
            (20, 4)
            >>> # Now can visualize: what does "cluster 1 anomaly" look like?
        """
        if embedding.ndim == 1:
            embedding = embedding[np.newaxis, np.newaxis, :]  # (latent_dim,) → (1, 1, latent_dim)
        elif embedding.ndim == 2:
            embedding = embedding[:, np.newaxis, :]  # (batch, latent_dim) → (batch, 1, latent_dim)
        
        z = torch.tensor(embedding, dtype=torch.float32).to(self.device)
        
        with torch.no_grad():
            # Decode from latent space
            reconstruction = self.model.decode(z)  # (batch, seq_len, input_dim)
        
        return reconstruction[0].cpu().numpy()  # Return first batch item


# ============================================================================
# HELPER: Integration with inference
# ============================================================================

class EnrichedAnomalyResult:
    """Wraps anomaly detection result with embedding metadata."""
    
    def __init__(self,
                 sequence: np.ndarray,
                 anomaly_score: float,
                 embedding: np.ndarray,
                 uncertainty: float,
                 timestamp: Optional[datetime] = None):
        self.sequence = sequence
        self.anomaly_score = anomaly_score
        self.embedding = embedding
        self.uncertainty = uncertainty
        self.timestamp = timestamp or datetime.utcnow()
    
    def to_dict(self) -> Dict:
        """Convert to JSON-serializable dict for API response."""
        return {
            "anomaly_score": float(self.anomaly_score),
            "embedding": self.embedding.tolist(),  # Convert to list for JSON
            "uncertainty": float(self.uncertainty),
            "timestamp": self.timestamp.isoformat(),
            "is_confident": self.uncertainty < 0.15,
            "embedding_dim": len(self.embedding)
        }
```

---

### STEP 2: Create `src/models/embedding_utils.py`

```python
"""
Utility functions for working with embeddings.
"""

import numpy as np
from typing import List, Dict, Tuple
import logging

logger = logging.getLogger(__name__)


def normalize_embeddings(embeddings: np.ndarray) -> np.ndarray:
    """
    Normalize embeddings to unit length (L2 normalization).
    
    Useful for cosine distance computation.
    
    Args:
        embeddings: Shape (n, d) where n=num_embeddings, d=dimension
    
    Returns:
        normalized: Same shape, each row has norm=1
    """
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1  # Avoid division by zero
    return embeddings / norms


def embedding_statistics(embeddings: np.ndarray) -> Dict:
    """
    Compute statistics on a batch of embeddings.
    
    Returns:
        - mean: (latent_dim,) average embedding
        - std: (latent_dim,) std per dimension
        - cov: (latent_dim, latent_dim) covariance matrix
        - pca_variance: How much variance explained by first few dims
    """
    mean = embeddings.mean(axis=0)
    std = embeddings.std(axis=0)
    cov = np.cov(embeddings.T)
    
    # PCA variance
    U, s, Vt = np.linalg.svd(cov)
    variance_explained = s / s.sum()
    
    return {
        "mean": mean,
        "std": std,
        "covariance": cov,
        "pca_variance_explained": variance_explained,
        "effective_dims": np.where(np.cumsum(variance_explained) >= 0.95)[0][0]  # How many dims for 95%?
    }


def find_nearest_embeddings(query: np.ndarray,
                           candidates: np.ndarray,
                           k: int = 5,
                           metric: str = "cosine") -> Tuple[np.ndarray, np.ndarray]:
    """
    Find k nearest neighbors to query embedding.
    
    Args:
        query: Shape (latent_dim,)
        candidates: Shape (n_candidates, latent_dim)
        k: Number of neighbors to return
        metric: "cosine" or "euclidean"
    
    Returns:
        indices: (k,) indices of nearest neighbors
        distances: (k,) distances to each neighbor
    """
    from sklearn.metrics.pairwise import cosine_distances, euclidean_distances
    
    query = query.reshape(1, -1)
    
    if metric == "cosine":
        distances = cosine_distances(query, candidates)[0]
    elif metric == "euclidean":
        distances = euclidean_distances(query, candidates)[0]
    else:
        raise ValueError(f"Unknown metric: {metric}")
    
    indices = np.argsort(distances)[:k]
    return indices, distances[indices]


def embedding_projection_2d(embeddings: np.ndarray, 
                           method: str = "tsne") -> np.ndarray:
    """
    Project high-dimensional embeddings to 2D for visualization.
    
    Args:
        embeddings: Shape (n, latent_dim=8)
        method: "tsne" or "umap"
    
    Returns:
        projected: Shape (n, 2)
    """
    if method == "tsne":
        from sklearn.manifold import TSNE
        tsne = TSNE(n_components=2, random_state=42)
        return tsne.fit_transform(embeddings)
    elif method == "umap":
        try:
            import umap
            reducer = umap.UMAP(n_components=2, random_state=42)
            return reducer.fit_transform(embeddings)
        except ImportError:
            logger.warning("UMAP not installed, falling back to TSNE")
            return embedding_projection_2d(embeddings, method="tsne")
    else:
        raise ValueError(f"Unknown method: {method}")
```

---

### STEP 3: Update `src/models/inference.py`

Add embedding extraction to inference:

```python
"""
Updated inference.py - Now returns embeddings with anomaly scores.
"""

import torch
import numpy as np
from typing import Dict, Optional
from datetime import datetime
from src.models.lstm_vae import LSTMVAE
from src.models.anomaly_embedding import AnomalyEmbedding, EnrichedAnomalyResult


class AnomalyDetectorWithEmbeddings:
    """Inference engine that returns both scores AND embeddings."""
    
    def __init__(self, checkpoint_path: str, device: str = "cpu"):
        self.device = torch.device(device)
        self.model = LSTMVAE().to(self.device)
        self.model.load_checkpoint(checkpoint_path)
        self.model.eval()
        
        # Create embedding extractor
        self.embedder = AnomalyEmbedding(self.model, device=device)
    
    def predict(self, sequence: np.ndarray) -> EnrichedAnomalyResult:
        """
        Detect anomaly and return enriched result with embedding.
        
        Returns:
            EnrichedAnomalyResult with:
            - anomaly_score (scalar)
            - embedding (8-dim vector)
            - uncertainty
        """
        x = torch.tensor(sequence[np.newaxis, :], dtype=torch.float32).to(self.device)
        
        with torch.no_grad():
            # Get reconstruction
            recon, mu, logvar, _ = self.model(x)
            
            # Anomaly score (reconstruction error)
            anomaly_score = torch.nn.functional.mse_loss(recon, x).item()
            
            # Embedding + uncertainty
            embedding = mu[0].cpu().numpy()
            std = torch.exp(0.5 * logvar)[0].cpu().numpy()
            uncertainty = float(std.mean())
        
        return EnrichedAnomalyResult(
            sequence=sequence,
            anomaly_score=anomaly_score,
            embedding=embedding,
            uncertainty=uncertainty
        )
    
    def predict_batch(self, sequences: np.ndarray) -> List[EnrichedAnomalyResult]:
        """Inference on batch of sequences."""
        x = torch.tensor(sequences, dtype=torch.float32).to(self.device)
        
        with torch.no_grad():
            recon, mu, logvar, _ = self.model(x)
            
            anomaly_scores = torch.nn.functional.mse_loss(recon, x, reduction='none').mean(dim=[1, 2])
            embeddings = mu.cpu().numpy()
            stds = torch.exp(0.5 * logvar).cpu().numpy()
            uncertainties = stds.mean(axis=1)
        
        results = []
        for i in range(len(sequences)):
            result = EnrichedAnomalyResult(
                sequence=sequences[i],
                anomaly_score=anomaly_scores[i].item(),
                embedding=embeddings[i],
                uncertainty=uncertainties[i]
            )
            results.append(result)
        
        return results
```

---

### STEP 4: Update API Response Schema

Update `src/api/schemas/sensor_data.py`:

```python
from pydantic import BaseModel
from typing import List, Optional

class InferenceResponse(BaseModel):
    """Response from inference endpoint - NOW WITH EMBEDDINGS."""
    
    # Existing fields
    model_id: str
    customer_id: str
    sequences_processed: int
    anomalies_detected: int
    
    # NEW: Embeddings
    anomaly_scores: List[float]  # [0.05, 0.42, 0.89, ...]
    embeddings: List[List[float]]  # [[0.1, -0.2, ...], [0.05, 0.1, ...], ...]
    uncertainties: List[float]  # [0.12, 0.08, 0.25, ...]
    
    # Metadata
    embedding_dim: int = 8
    high_anomalies: List[dict] = []
```

---

### STEP 5: Create Tests

`tests/models/test_anomaly_embedding.py`:

```python
import pytest
import numpy as np
import torch
from src.models.lstm_vae import LSTMVAE
from src.models.anomaly_embedding import AnomalyEmbedding


@pytest.fixture
def model():
    """Create a test LSTM-VAE."""
    model = LSTMVAE(input_dim=4, hidden_dims=[64, 32], latent_dim=8, seq_len=20)
    model.eval()
    return model


@pytest.fixture
def embedder(model):
    """Create embedder from model."""
    return AnomalyEmbedding(model, device="cpu")


@pytest.fixture
def sample_sequence():
    """Create sample sequence."""
    return np.random.rand(20, 4)


class TestAnomalyEmbedding:
    
    def test_embed_sequence_shape(self, embedder, sample_sequence):
        """Embedding should be (latent_dim,)."""
        embedding = embedder.embed_sequence(sample_sequence)
        assert embedding.shape == (8,)
    
    def test_embed_batch_shape(self, embedder):
        """Batch embeddings should be (batch_size, latent_dim)."""
        batch = np.random.rand(10, 20, 4)
        embeddings = embedder.embed_batch(batch)
        assert embeddings.shape == (10, 8)
    
    def test_embed_with_uncertainty(self, embedder, sample_sequence):
        """Should return dict with embedding and uncertainty."""
        result = embedder.embed_with_uncertainty(sample_sequence)
        assert "embedding" in result
        assert "uncertainty" in result
        assert result["embedding"].shape == (8,)
        assert isinstance(result["uncertainty"], float)
        assert result["uncertainty"] >= 0
    
    def test_embedding_distance(self, embedder):
        """Distance between identical embeddings should be ~0."""
        emb = np.random.rand(8)
        dist = embedder.embedding_distance(emb, emb)
        assert abs(dist) < 1e-6
    
    def test_embedding_similarity(self, embedder):
        """Similarity of identical embeddings should be ~1."""
        emb = np.random.rand(8)
        sim = embedder.embedding_similarity(emb, emb)
        assert abs(sim - 1.0) < 1e-6
    
    def test_cluster_embeddings(self, embedder):
        """Should cluster embeddings."""
        embeddings = np.random.rand(100, 8)
        clusters = embedder.cluster_embeddings(embeddings, n_clusters=3)
        
        assert clusters["cluster_centers"].shape == (3, 8)
        assert len(clusters["labels"]) == 100
        assert len(np.unique(clusters["labels"])) <= 3
    
    def test_decode_embedding(self, embedder):
        """Decode should return sequence shape."""
        embedding = np.random.rand(8)
        reconstruction = embedder.decode_embedding(embedding)
        assert reconstruction.shape == (20, 4)
```

---

## 📊 INTEGRATION CHECKLIST

- [ ] Create `src/models/anomaly_embedding.py`
- [ ] Create `src/models/embedding_utils.py`
- [ ] Update `src/models/inference.py` to use AnomalyEmbedding
- [ ] Update `src/api/schemas/sensor_data.py` with embedding fields
- [ ] Update `src/api/routes/inference.py` to return embeddings
- [ ] Create tests in `tests/models/test_anomaly_embedding.py`
- [ ] Update dashboard to display embeddings (visualization)
- [ ] Document in `docs/embeddings_architecture.md`

---

## 🧪 TESTING STRATEGY

### Unit Tests (1 day)
```bash
pytest tests/models/test_anomaly_embedding.py -v
```

### Integration Tests (0.5 days)
```bash
# Test end-to-end: CSV → inference → embeddings
pytest tests/api/test_inference_with_embeddings.py -v
```

### Manual Validation (0.5 days)
1. Upload CSV with known anomalies
2. Extract embeddings
3. Compute pairwise distances
4. Verify similar anomalies cluster together

---

## 🚀 DEPLOYMENT

### Phase:
- **Phase 1.5 Week 1, Day 1-2**
- Before: pgvector incident memory
- After: Online learning module

### Backward Compatibility:
✅ **Yes** - Existing inference still works, embeddings are additional output

### Rollout:
1. Deploy to staging
2. Run inference on test dataset
3. Visualize embeddings (t-SNE) to verify clustering
4. Deploy to production

---

## 📈 SUCCESS CRITERIA

- [ ] 100+ past anomalies have embeddings
- [ ] Similar anomalies cluster (cosine distance < 0.3)
- [ ] Inference latency < 50ms (embedding extraction adds <5ms)
- [ ] API returns embeddings in response
- [ ] Dashboard shows embedding visualization

---

## 💾 DEPENDENCIES

Add to `requirements.txt`:
```
scikit-learn>=1.0.0  # For clustering, metrics
scipy>=1.7.0        # For distance calculations
```

Optional (for visualization):
```
tsne-sklearn>=0.1.5  # t-SNE projection
# or
umap-learn>=0.5.0    # UMAP projection (better than t-SNE)
```

---

## 📝 NEXT STEPS

→ Proceed to **Gap 2: Incident Memory System (pgvector)** once embeddings are working.

**Expected Time:** 2 days  
**Output:** Anomaly embeddings stored in inference responses  
**Foundation for:** pgvector memory, few-shot learning, semantic search
