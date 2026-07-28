"""
Configuration constants for the AI Agronomist platform.

This module centralizes all hyperparameters and configuration values used
by the LSTM-VAE model and the Synthetic Data Generator to ensure
consistency and reproducibility across the pipeline.

All values are designed to be used by:
- src/models/data_generator.py
- src/models/lstm_vae.py
"""

import os
import torch
from typing import Dict, List, Tuple


def _select_device() -> str:
    """Return the best available compute device.

    Priority: FORCE_DEVICE env var > CUDA > CPU.

    MPS (Apple Silicon) is intentionally skipped — PyTorch's MPS backend does
    not fully support the ModuleList-of-single-layer-LSTM pattern used by
    LSTMVAE, which causes "Placeholder storage has not been allocated on MPS
    device!" at runtime.  Set FORCE_DEVICE=mps to opt in once a future
    PyTorch release resolves the issue.
    """
    import os as _os
    forced = _os.environ.get("FORCE_DEVICE", "").strip().lower()
    if forced:
        return forced
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


DEVICE: str = _select_device()

# Intra-op thread count for CPU PyTorch (LSTM matmuls dominate cold-start
# training and portion scoring). PyTorch defaults to the physical core count,
# but inside containers ``os.cpu_count()`` can misreport the cgroup CPU quota,
# so we make it explicit and env-overridable. Pin TORCH_NUM_THREADS to the
# deployed vCPU allocation (Railway worker: 8). 0 leaves PyTorch's default.
TORCH_NUM_THREADS: int = int(os.environ.get("TORCH_NUM_THREADS", str(os.cpu_count() or 4)))


def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean env var (1/true/yes/on -> True), falling back to *default*."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Toggle for the PROFILE phase-timing logs emitted by src/utils/profiling.py.
# Enabled by default so the upload -> train -> find pipeline can be measured in
# production; set PROFILE_LOGGING_ENABLED=0 (or false/no/off) on the service to
# silence it once tuning is done — no code change, just a restart.
PROFILE_LOGGING_ENABLED: bool = _env_flag("PROFILE_LOGGING_ENABLED", True)

# =============================================================================
# Reproducibility & Global Settings
# =============================================================================
RANDOM_SEED: int = 42
# Note: Using 42 as standard seed for reproducibility

# =============================================================================
# LSTM-VAE Model Architecture
# =============================================================================
# The number of features per time step (temperature, humidity, CO2, light)
INPUT_DIM: int = 4

# The length of each input sequence
SEQ_LEN: int = 20

# The dimensionality of the latent space bottleneck
LATENT_DIM: int = 8

# List of dimensions for the hidden LSTM layers
HIDDEN_DIMS: List[int] = [64, 32]

# Derived from HIDDEN_DIMS
NUM_LAYERS: int = len(HIDDEN_DIMS)

# Dropout probability for regularization
DROPOUT_RATE: float = 0.2

# Weight of the KL divergence term in the VAE loss function
BETA: float = 0.1

# =============================================================================
# Training Hyperparameters
# =============================================================================
# Number of training samples processed in one iteration
BATCH_SIZE: int = 32

# Initial learning rate for the optimizer
LEARNING_RATE: float = 1e-3

# Total number of training epochs
NUM_EPOCHS: int = 100

# L2 regularization coefficient applied by the optimizer
WEIGHT_DECAY: float = 1e-5

# Number of epochs without validation improvement before stopping early
EARLY_STOPPING_PATIENCE: int = 10

# Proportion of data held out for validation during training
VALIDATION_SPLIT: float = 0.2

# =============================================================================
# Filesystem Paths
# =============================================================================
# Directories for model checkpoints, generated data, and training logs
PATHS: dict = {
    "checkpoint_dir": "models/checkpoints/",
    "data_dir": "data/synthetic/",
    "log_dir": "logs/tensorboard/",
}

# Directory where per-customer fine-tuned model checkpoints are saved.
# Override via CUSTOMER_MODEL_DIR env var (e.g. point at a persistent volume
# in containerised deploys, or at tmp_path in tests).
CUSTOMER_MODEL_DIR: str = os.environ.get("CUSTOMER_MODEL_DIR", "models/")

# =============================================================================
# Synthetic Data Generation & Normalization
# =============================================================================
# Total number of synthetic sequences to generate
NUM_SAMPLES: int = 1000

# The range for normalized sensor data [0, 1]
NORM_MIN: float = 0.0
NORM_MAX: float = 1.0

# Physical bounds for sensor readings (unnormalized)
TEMP_RANGE: Tuple[float, float] = (20.0, 30.0)      # Celsius
HUMIDITY_RANGE: Tuple[float, float] = (50.0, 80.0)  # Percentage
CO2_RANGE: Tuple[float, float] = (300.0, 500.0)     # ppm
LIGHT_RANGE: Tuple[float, float] = (0.0, 1000.0)    # lux

# Metadata for sensor identification
SENSOR_NAMES: List[str] = ["temperature", "humidity", "co2", "light"]

SENSOR_UNITS: Dict[str, str] = {
    "temperature": "°C",
    "humidity": "%",
    "co2": " ppm",
    "light": " lux",
}

# =============================================================================
# Synthetic Signal Generation Parameters
# =============================================================================
# Tunable signal-shape and noise constants used by SyntheticDataGenerator to
# build the per-channel diurnal/trend signals plus Gaussian sensor noise.
# These are externalized here so the generator contains no magic numbers.

# Divisor applied to a channel's (high - low) span to derive its sine amplitude.
AMPLITUDE_DIVISOR: float = 4.0

# Temperature channel: diurnal sine frequency and additive noise std.
TEMP_SINE_FREQ: float = 0.2
TEMP_NOISE_STD: float = 0.5

# Humidity channel: inverse-coupling factor to temperature and noise std.
HUMIDITY_COUPLING_FACTOR: float = 0.5
HUMIDITY_NOISE_STD: float = 1.0

# CO2 channel: upward trend slope (per time step) and noise std.
CO2_TREND_SLOPE: float = 0.1
CO2_NOISE_STD: float = 5.0

# Light channel: day/night sine amplitude, sine frequency and noise std.
LIGHT_AMPLITUDE: float = 400.0
LIGHT_SINE_FREQ: float = 0.1
LIGHT_NOISE_STD: float = 50.0

# Moving-average window size used to smooth raw signals into continuous series.
SMOOTHING_KERNEL_SIZE: int = 3

# =============================================================================
# Aggregated Configuration Dictionaries
# =============================================================================
# These dicts are derived from the flat constants above and provide a grouped
# view of the configuration consumed by the model, training, and data layers.
MODEL_CONFIG: dict = {
    "input_dim": INPUT_DIM,
    "hidden_dims": HIDDEN_DIMS,
    "latent_dim": LATENT_DIM,
    "seq_len": SEQ_LEN,
    "beta": BETA,
    "dropout": DROPOUT_RATE,
}

TRAINING_CONFIG: dict = {
    "epochs": NUM_EPOCHS,
    "batch_size": BATCH_SIZE,
    "learning_rate": LEARNING_RATE,
    "weight_decay": WEIGHT_DECAY,
    "early_stopping_patience": EARLY_STOPPING_PATIENCE,
    "validation_split": VALIDATION_SPLIT,
}


DATA_CONFIG: dict = {
    "num_sequences": NUM_SAMPLES,
    "seq_len": SEQ_LEN,
    "input_dim": INPUT_DIM,
    "random_seed": RANDOM_SEED,
}

# =============================================================================
# Anomaly Detection and Post-Processing
# =============================================================================
ANOMALY_THRESHOLD: float = 0.8
ANOMALY_SCALING_FACTOR: float = 2.0

# Cold-run CSV upload inference (/api/inference/upload)
COLD_RUN_THRESHOLD_METHOD: str = "percentile"
COLD_RUN_PERCENTILE: float = 99.9
COLD_RUN_MIN_ANOMALIES: int = 1
COLD_RUN_BASELINE_FRACTION: float = 0.2
# Rolling-window statistical detection (5-min data → 288 = 24 h).
COLD_RUN_STAT_ROLLING_WINDOW: int = 288
COLD_RUN_STAT_Z_MIN: float = 5.5
COLD_RUN_STAT_MIN_CONSECUTIVE: int = 3
COLD_RUN_STAT_USE_IQR: bool = False
# Rows sampled from the head of an uploaded CSV to calibrate adaptive sensor
# ranges before the single scoring pass (bounds upload memory on large files).
# Override via ADAPTIVE_RANGE_SAMPLE_ROWS env var.
ADAPTIVE_RANGE_SAMPLE_ROWS: int = int(os.environ.get("ADAPTIVE_RANGE_SAMPLE_ROWS", "20000"))

# Maximum number of pipeline instances kept in the LRU cache.
# Override via MAX_PIPELINE_CACHE_SIZE env var (e.g. set to 1 on memory-constrained hosts).
MAX_PIPELINE_CACHE_SIZE: int = int(os.environ.get("MAX_PIPELINE_CACHE_SIZE", "1"))

# Maximum number of AnomalyMetrics detection events held in the bounded deque.
# Older events are automatically evicted when the cap is reached.
MAX_METRICS_RETENTION_EVENTS: int = 10000

# =============================================================================
# Anomaly Embeddings (Gap 1 — Vector Embeddings)
# =============================================================================
# An embedding whose latent-space uncertainty (mean std) is below this value
# is considered a confident representation of the anomaly.
EMBEDDING_CONFIDENCE_THRESHOLD: float = 0.15

# Default number of KMeans clusters when grouping anomaly embeddings.
DEFAULT_EMBEDDING_CLUSTERS: int = 5

# Default number of neighbors returned by nearest-embedding search.
DEFAULT_NEAREST_NEIGHBORS: int = 5

# Cumulative PCA variance ratio used to estimate effective dimensionality.
PCA_VARIANCE_TARGET: float = 0.95

# Output dimensionality for visualization projections (t-SNE / UMAP).
PROJECTION_COMPONENTS_2D: int = 2

# =============================================================================
# Online Learning (Gap 3 — Online Learning Pipeline)
# =============================================================================
# SGD step size for streaming fine-tuning (much smaller than offline
# LEARNING_RATE to avoid destabilizing the pretrained model).
ONLINE_LEARNING_RATE: float = 1e-4

# Momentum for the online SGD optimizer (helps with noisy streaming gradients).
ONLINE_SGD_MOMENTUM: float = 0.9

# Maximum number of sequences retained in the general replay buffer.
REPLAY_BUFFER_CAPACITY: int = 1000

# Maximum number of sequences retained in the specialized anomaly buffer.
ANOMALY_BUFFER_CAPACITY: int = 100

# Perform one online SGD update every N observed sequences.
ONLINE_UPDATE_INTERVAL: int = 50

# Mini-batch size sampled from the replay buffer per online update.
ONLINE_BATCH_SIZE: int = 32

# Elastic Weight Consolidation regularization strength (lambda).
EWC_LAMBDA: float = 0.4

# EWC activation: sequences an online learner must observe before its Fisher
# information is computed (once) from the replay buffer, and how many
# mini-batches to average over. Until then the penalty is 0 — forgetting
# protection then rests only on the small LR, grad clipping, and replay mix.
EWC_FISHER_WARMUP_SAMPLES: int = int(
    os.environ.get("EWC_FISHER_WARMUP_SAMPLES", "200")
)
EWC_FISHER_BATCHES: int = int(os.environ.get("EWC_FISHER_BATCHES", "10"))

# Synaptic Intelligence regularization strength (lambda).
SI_LAMBDA: float = 0.4

# Synaptic Intelligence damping term (xi) preventing division by ~0 deltas.
SI_DAMPING: float = 0.1

# Prioritized replay: priority exponent (0 = uniform, 1 = full prioritization).
PRIORITY_ALPHA: float = 0.6

# Prioritized replay: importance-sampling weight exponent (beta).
IMPORTANCE_WEIGHT_BETA: float = 0.6

# Max global gradient norm during online updates (explosion guard).
GRAD_CLIP_NORM: float = 1.0

# Drift detection: size of the recent-loss comparison window.
DRIFT_WINDOW_SIZE: int = 100

# Drift detection: normalized recent-vs-historical loss increase that
# triggers a drift alert / retraining recommendation.
DRIFT_THRESHOLD: float = 0.3

# Online learner trend analysis: number of most recent losses treated as
# "recent" when compared against the older history.
ONLINE_TREND_WINDOW: int = 50

# Log an INFO line every N online updates (keeps streaming logs quiet).
ONLINE_LOG_EVERY_N_UPDATES: int = 10

# Small epsilon for numerical stability in priority/drift ratio computations.
NUMERICAL_EPSILON: float = 1e-6

# Maximum number of sequences accepted by one /api/learning/observe request.
MAX_OBSERVE_BATCH: int = 256

# Maximum number of masked/unmasked rows fed to the model in a single
# attribution forward pass. Batched attribution builds B*(num_sensors+1)
# rows; this caps each micro-batch to bound peak memory on large batches.
# Override via ATTRIBUTION_MAX_BATCH env var.
ATTRIBUTION_MAX_BATCH: int = int(os.environ.get("ATTRIBUTION_MAX_BATCH", "256"))

# --- Message queue (RabbitMQ) ---------------------------------------------
# Broker URL; on Railway reference the RabbitMQ service's private domain.
RABBITMQ_URL: str = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
# Unacked messages a worker consumes at once (torch inference is heavy; keep low).
MQ_PREFETCH_COUNT: int = int(os.environ.get("MQ_PREFETCH_COUNT", "2"))
# Delivery attempts before a message is parked in the dead-letter queue.
MQ_MAX_RETRIES: int = int(os.environ.get("MQ_MAX_RETRIES", "3"))
# Delay between retries, applied via TTL-backed retry queues.
MQ_RETRY_DELAY_MS: int = int(os.environ.get("MQ_RETRY_DELAY_MS", "30000"))
# Hard per-message handler deadline: a hung handler (e.g. a wedged DB call)
# must surface as a visible retry, never a silent prefetch-window jam.
MQ_HANDLER_TIMEOUT_SECONDS: float = float(os.environ.get("MQ_HANDLER_TIMEOUT_SECONDS", "600"))
# Retraining legitimately runs long — separate, generous deadline.
MQ_RETRAIN_HANDLER_TIMEOUT_SECONDS: float = float(
    os.environ.get("MQ_RETRAIN_HANDLER_TIMEOUT_SECONDS", "3600")
)
# Upload preparation (cold-start train + portion production) runs on the worker
# and can take minutes; deadline sits between the portion (600s) and retrain
# (3600s) timeouts so a hung prep surfaces as a visible retry, never a jam.
MQ_PREPARE_HANDLER_TIMEOUT_SECONDS: float = float(
    os.environ.get("MQ_PREPARE_HANDLER_TIMEOUT_SECONDS", "1800")
)
# Backstop stale-prepare sweep: an upload job stuck 'preparing' (or 'running'
# with total_portions NULL) with no activity for this long is treated as lost
# (prepare message never delivered / DLQ hook failed) and failed, its blob
# deleted. Kept safely above the prepare handler timeout + retry budget so it
# never fails legitimately in-flight preparation.
STALE_PREPARE_MINUTES: int = int(os.environ.get("STALE_PREPARE_MINUTES", "45"))
# Watchdog cadence: exit for a clean container restart when the MQ connection
# or any consumer channel dies (aio-pika channel restore can fail after a
# broker restart, leaving a zombie process that consumes nothing).
MQ_WATCHDOG_INTERVAL_SECONDS: float = float(os.environ.get("MQ_WATCHDOG_INTERVAL_SECONDS", "60"))
# AMQP heartbeat interval negotiated with the broker. Default broker-side is
# 60s — too tight around CPU-heavy inference; brief event-loop stalls must not
# get the connection reset ('Connection reset by peer' requeue loops).
MQ_HEARTBEAT_SECONDS: int = int(os.environ.get("MQ_HEARTBEAT_SECONDS", "300"))
# Rows per upload portion message (~1 MB JSON for typical sensor CSVs).
UPLOAD_PORTION_ROWS: int = int(os.environ.get("UPLOAD_PORTION_ROWS", "5000"))
# Sequences per online-learning portion message.
ONLINE_PORTION_SEQUENCES: int = int(os.environ.get("ONLINE_PORTION_SEQUENCES", "64"))
# Rows per DB page when a retrain job streams training data.
RETRAIN_PORTION_ROWS: int = int(os.environ.get("RETRAIN_PORTION_ROWS", "10000"))
# Files at or below this row count keep the synchronous upload path.
UPLOAD_SYNC_MAX_ROWS: int = int(os.environ.get("UPLOAD_SYNC_MAX_ROWS", "20000"))

# Hard cap on rows returned by any /api/readings list endpoint.
READINGS_MAX_ROWS: int = int(os.environ.get("READINGS_MAX_ROWS", "50000"))
# Default landing-page window for the Monitoring dashboard (item C2).
READINGS_RECENT_HOURS: int = int(os.environ.get("READINGS_RECENT_HOURS", "24"))

# =============================================================================
# Anomaly Attribution (Phase 1.9.1 Week 1 — Anomaly Attributor)
# =============================================================================
# Minimum normalized contribution for a sensor to be reported as "affected".
ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD: float = 0.15

# Scale applied to the maximum normalized contribution to derive a confidence
# score (a single sensor dominating at >=50% maps to confidence 1.0).
ATTRIBUTION_CONFIDENCE_SCALE: float = 2.0

# Minimum absolute mean deviation (in normalized sensor units) for a sensor
# to be mentioned in the natural-language attribution explanation.
EXPLANATION_DEVIATION_THRESHOLD: float = 0.1

# Maximum page size accepted by /api/learning/history queries.
MAX_HISTORY_LIMIT: int = 1000

# =============================================================================
# Pattern Detection (Phase 1.9.1 Week 1 — Pattern Detector)
# =============================================================================
# Minimum sequence length (time steps) accepted by the pattern detector;
# variance and correlation are undefined on shorter sequences.
PATTERN_MIN_SEQ_LEN: int = 2

# DRIFT: a sensor is "stuck" when its in-sequence variance falls below this
# fraction of its baseline variance.
PATTERN_DRIFT_VARIANCE_RATIO: float = 0.01

# SPIKE: |z-score| above this marks a point as an outlier.
PATTERN_SPIKE_Z_THRESHOLD: float = 3.0

# SPIKE: outliers must span at most this fraction of the sequence to count as
# a *sudden* spike (longer excursions are sustained shifts, not spikes).
PATTERN_SPIKE_MAX_DURATION_FRACTION: float = 0.25

# SPIKE: peak z-score reaching THRESHOLD * SCALE maps to spike score 1.0.
PATTERN_SPIKE_SCORE_SCALE: float = 2.0

# DEPENDENCY: a pairwise |corr_now - corr_baseline| above this flags the pair
# (and both its sensors) as a broken dependency.
PATTERN_DEPENDENCY_PAIR_THRESHOLD: float = 0.5

# LAG: maximum cross-correlation shift (time steps) searched in each direction.
PATTERN_LAG_MAX_STEPS: int = 5

# LAG: minimum sequence length for a meaningful cross-correlation search.
PATTERN_LAG_MIN_SEQ_LEN: int = 10

# LAG: sequence length is divided by this value to derive the lag search
# window (ensures the search window scales with sequence length).
PATTERN_LAG_WINDOW_FRACTION: int = 4

# LAG: only sensor pairs at least this correlated in the baseline are
# candidates for lag detection ("normally correlated but delayed").
PATTERN_LAG_MIN_BASELINE_CORRELATION: float = 0.5

# LAG: |cross-correlation| at the best non-zero shift must reach this value.
PATTERN_LAG_MIN_CORRELATION: float = 0.5

# LAG: the best non-zero shift must beat the zero-shift correlation by this
# margin (otherwise the signals are simply aligned).
PATTERN_LAG_IMPROVEMENT_MARGIN: float = 0.2

# SATURATION: boundary band width as a fraction of the baseline value range;
# readings within this band of the baseline min/max count as "at boundary".
PATTERN_SATURATION_BOUNDARY_TOLERANCE: float = 0.02

# SATURATION: fraction of time steps a sensor must spend at a boundary to be
# considered saturated/railed.
PATTERN_SATURATION_FRACTION_THRESHOLD: float = 0.9

# CYCLIC: minimum sequence length for a usable FFT periodicity estimate.
PATTERN_CYCLIC_MIN_SEQ_LEN: int = 20

# CYCLIC: a sensor counts as periodic in the baseline when its dominant
# non-DC frequency holds at least this fraction of total non-DC power.
PATTERN_CYCLIC_MIN_BASELINE_POWER: float = 0.3

# CYCLIC: the cycle is "broken" when the sequence's power fraction at the
# baseline-dominant frequency drops below this ratio of the baseline fraction.
PATTERN_CYCLIC_POWER_DROP_RATIO: float = 0.5

# CASCADE: a sensor participates in a cascade when its mean |z-score| over
# the sequence exceeds this value.
PATTERN_CASCADE_MIN_SENSOR_Z: float = 2.0

# CASCADE: minimum number of simultaneously deviating sensors for a cascade.
PATTERN_CASCADE_MIN_SENSORS: int = 2

# Confidence: gap between the top-2 pattern scores is scaled by this factor
# (a gap of 1/SCALE or larger maps to full confidence 1.0).
PATTERN_CONFIDENCE_GAP_SCALE: float = 2.0

# =============================================================================
# Confidence Scoring (Phase 1.9.1 Week 2 — Confidence Scorer)
# =============================================================================
# Default weights for combining the four confidence signals; must sum to 1.0.
# Tunable per customer based on false-positive feedback.
CONFIDENCE_SIGNAL_WEIGHTS: dict = {
    "recon_error": 0.4,
    "pattern_clarity": 0.3,
    "ensemble_agreement": 0.2,
    "uncertainty": 0.1,
}

# Reconstruction error at this multiple of the baseline error maps to a full
# (1.0) reconstruction-error signal.
CONFIDENCE_ERROR_RATIO_SCALE: float = 3.0

# Expected reconstruction error on normal data, used when the caller does not
# supply a measured baseline error.
CONFIDENCE_DEFAULT_BASELINE_ERROR: float = 0.05

# Total number of detectors in the ensemble; agreement is normalized by this.
CONFIDENCE_ENSEMBLE_SIZE: int = 4

# =============================================================================
# Ensemble Detector (Gap 4)
# =============================================================================
# Weighted vote across lstm_vae, isolation_forest, statistical, correlation.
ENSEMBLE_DETECTOR_WEIGHTS: Dict[str, float] = {
    "lstm_vae": 0.25,
    "isolation_forest": 0.25,
    "statistical": 0.25,
    "correlation": 0.25,
}
ENSEMBLE_ANOMALY_THRESHOLD: float = 0.5
ENSEMBLE_MIN_AGREEMENT: int = 2
ENSEMBLE_LSTM_VAE_THRESHOLD: float = ANOMALY_THRESHOLD
ENSEMBLE_ISOLATION_FOREST_THRESHOLD: float = 0.5
ENSEMBLE_STATISTICAL_THRESHOLD: float = 3.0
ENSEMBLE_CORRELATION_THRESHOLD: float = 0.35

# Max absolute delta when grid-searching ANOMALY_THRESHOLD during retrain.
ACTIVE_LEARNING_MAX_THRESHOLD_DELTA: float = 0.15

# Confidence at or above this threshold is "high confidence" (actionable).
CONFIDENCE_HIGH_THRESHOLD: float = 0.75

# Confidence at or below this threshold is "low confidence" (possibly noise).
CONFIDENCE_LOW_THRESHOLD: float = 0.4

# Ordered (lower-bound, label) pairs for human-readable confidence levels;
# evaluated top-down, anything below the last bound is CONFIDENCE_MIN_LEVEL.
CONFIDENCE_LEVEL_BOUNDS: List[Tuple[float, str]] = [
    (0.85, "very_high"),
    (0.75, "high"),
    (0.6, "medium"),
    (0.4, "low"),
]

# Label for confidence below every CONFIDENCE_LEVEL_BOUNDS lower bound.
CONFIDENCE_MIN_LEVEL: str = "very_low"

# =============================================================================
# Root Cause Analysis (Phase 1.9.1 Week 2 — Root Cause Analyzer)
# =============================================================================
# Minimum normalized attribution contribution for a sensor to be counted as
# "affected" when matching diagnosis rules.
ROOT_CAUSE_ATTRIBUTION_THRESHOLD: float = 0.15

# Fallback confidence when no diagnosis rule matches the anomaly.
ROOT_CAUSE_UNKNOWN_CONFIDENCE: float = 0.5

# More than this many affected sensors triggers the "multi_sensor" cascade
# rule (strict greater-than comparison: 3+ sensors when set to 2).
ROOT_CAUSE_MULTI_SENSOR_MIN: int = 2

# Near-zero variance threshold: sensor variance below this value is evidence
# of a stuck/drifting sensor (used in supporting-evidence gathering).
ROOT_CAUSE_VARIANCE_THRESHOLD: float = 0.001

# =============================================================================
# Recommendation Engine (Phase 1.9.1 Week 2 — Recommendation Engine)
# =============================================================================
# Maximum number of actions returned per severity level.
RECOMMENDATION_MAX_ACTIONS: dict = {"high": 5, "medium": 3, "low": 2}

# Root causes that warrant "urgent" urgency classification.
RECOMMENDATION_URGENT_CAUSES: List[str] = [
    "compressor_failure",
    "power_supply_issue",
    "ventilation_failure",
]

# Substring keywords — a root cause containing any of these requires a
# technician, UNLESS it also matches a "not required" keyword.
RECOMMENDATION_TECH_REQUIRED_KEYWORDS: List[str] = [
    "failure",
    "compressor",
    "power",
]

# Exclusion keywords — sensor-level issues that do NOT need a technician
# even when they contain a "required" keyword (e.g. "sensor_failure").
RECOMMENDATION_TECH_NOT_REQUIRED_KEYWORDS: List[str] = ["sensor"]

# =============================================================================
# Active Learning (Phase 1.9.1 Week 3 — Feedback Loop)
# =============================================================================
# Maximum number of feedback entries retained in the circular buffer.
ACTIVE_LEARNING_FEEDBACK_CAPACITY: int = 1000

# Minimum number of high-quality labels required before retraining.
ACTIVE_LEARNING_MIN_QUALITY_LABELS: int = 20

# Operator accuracy (EMA) must be at or above this to count as "high quality".
ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD: float = 0.85

# Minimum distinct pattern types in the high-quality set before retraining.
ACTIVE_LEARNING_MIN_PATTERN_TYPES: int = 2

# Cooldown: minimum hours between consecutive retrain triggers.
ACTIVE_LEARNING_MIN_RETRAIN_INTERVAL_HOURS: int = 4

# EMA smoothing factor for operator reliability updates (higher = more weight
# on the latest outcome).
ACTIVE_LEARNING_OPERATOR_ALPHA: float = 0.2

# Default reliability assigned to operators with no recorded outcomes.
ACTIVE_LEARNING_OPERATOR_RELIABILITY_DEFAULT: float = 0.5

# Outcome count below which reliability is dampened toward the default.
ACTIVE_LEARNING_MIN_OPERATOR_SAMPLES: int = 5

# Maximum number of training samples drawn per retrain cycle.
ACTIVE_LEARNING_MAX_TRAINING_SAMPLES: int = 100

# Minimum training samples required to proceed with retraining.
ACTIVE_LEARNING_MIN_TRAINING_SAMPLES: int = 10

# Model improvement (fraction) required to promote a retrained model.
ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD: float = 0.05

# IsolationForest contamination applied when sanitizing training data.
TRAINING_IF_CONTAMINATION: float = 0.05

# Lookback window (days) for recent feedback in retraining decisions.
ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS: int = 30

# Default operator self-assessed confidence when not provided.
ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE: float = 0.8

# =============================================================================
# API Authentication (Phase D — production hardening)
# =============================================================================
import os
from typing import Dict


def _parse_tenant_api_keys() -> Dict[str, str]:
    """Parse ``TENANT_API_KEYS=tenant_a:secret1,tenant_b:secret2`` from env."""
    raw = os.environ.get("TENANT_API_KEYS", "")
    keys: Dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        tenant_id, api_key = part.split(":", 1)
        tenant_id = tenant_id.strip()
        api_key = api_key.strip()
        if tenant_id and api_key:
            keys[tenant_id] = api_key
    return keys


# =============================================================================
# EWMA / POT Adaptive Thresholding (SCADA — Phase 2)
# =============================================================================
# Smoothing factor for the EWMA anomaly-score tracker (higher = faster adapt).
EWMA_ALPHA: float = 0.1

# Number of EWMA standard deviations above the mean for the dynamic threshold.
EWMA_K: float = 3.0

# Quantile of the residual buffer used as the initial POT exceedance threshold.
POT_Q: float = 0.90

# Target false-positive rate for the POT/GPD-derived threshold.
POT_FPR: float = 0.001

# Capacity of the POT circular residual buffer.
POT_BUFFER_SIZE: int = 500

# =============================================================================
# Gas Sensor Detection (SCADA — Phase 4)
# =============================================================================
# Column names for gas sensors stored in RawReading.data JSON.
GAS_SENSOR_COLUMNS: List[str] = [
    "c2h4",
    "gas_co2",
    "no",
    "no2",
    "nox",
    "co",
]

# =============================================================================
# SCADA Spike / CUSUM / Dropout Detection (Phase A)
# =============================================================================
# Per-sensor velocity limits for spike detection (units per timestep).
VELOCITY_LIMITS: dict[str, float] = {
    "temperature": 2.0,
    "humidity": 5.0,
    "co2": 50.0,
    "light": 200.0,
}

# CUSUM drift detector parameters (standardised-residual space).
# Names include _SIGMA suffix to document that units are standard deviations,
# not raw sensor units.
# spec ref: K=0.2, H=1.0 (docs/anomaly_framework_spec.md §4); production uses
# K=0.25/H=4.0 for ARL0 ≈ 340 (fewer false drift alarms on live telemetry).
CUSUM_K_SIGMA: float = float(os.environ.get("CUSUM_K_SIGMA", "0.25"))   # production-sensitive: detects 0.5σ shifts
CUSUM_H_SIGMA: float = float(os.environ.get("CUSUM_H_SIGMA", "4.0"))   # ARL₀ ≈ 340 samples

# Polling dropout: P consecutive steps with any null/NaN triggers alarm
# (SCADA spec section 1.5 — consecutive-step formulation).
DROPOUT_CONSECUTIVE_P: int = 5

# Stuck-fault rolling window size (SCADA spec section 1.3).
# spec ref: w=5, epsilon=1e-3 (docs/anomaly_framework_spec.md §3); production
# uses a longer window and a 10x stricter epsilon (requires near-exact flatness).
STUCK_WINDOW_K: int = 10
STUCK_VARIANCE_THRESHOLD: float = 1e-4
# Normalized-space range below which a window is treated as truly flat (stuck).
STUCK_FLAT_RANGE_THRESHOLD: float = 1e-4

# =============================================================================
# Setpoint-aware SCADA rules (dynamic climate-computer detection)
# =============================================================================
# Temperature deficit / excess margin relative to heating setpoint (°C).
SETPOINT_TEMP_DELTA_C: float = float(os.environ.get("SETPOINT_TEMP_DELTA_C", "2.0"))

# Solar / outdoor conditions that naturally explain over-heating (no alarm).
OVERHEAT_SOLAR_WM2: float = float(os.environ.get("OVERHEAT_SOLAR_WM2", "400.0"))
OVERHEAT_OUTDOOR_TEMP_C: float = float(os.environ.get("OVERHEAT_OUTDOOR_TEMP_C", "25.0"))

# Relative humidity condensation / dehumidification thresholds.
RH_CRIT_PERCENT: float = float(os.environ.get("RH_CRIT_PERCENT", "95.0"))
RH_ABSOLUTE_MAX_PERCENT: float = float(os.environ.get("RH_ABSOLUTE_MAX_PERCENT", "98.0"))
# Humidity deficit (g/m³) at/below which dehumidification is considered failed.
# Corrected 2026-07: rescaled 10× (was 0.5) after psychrometric HD coefficient
# fix (216.7 → 2167.0 for kPa inputs).  The old 0.5 was calibrated to an HD
# scale that was 10× too small; 5.0 preserves the same physical meaning on the
# corrected g/m³ scale.  At RH ≥ 95% (the gate for this rule), typical HD
# values are 0.5–2.0 g/m³ — well below this ceiling — so all near-saturation
# conditions correctly trigger DEHUMIDIFICATION_FAILURE.
DEHUMID_HD_CRIT_G_M3: float = float(os.environ.get("DEHUMID_HD_CRIT_G_M3", "5.0"))

# Crop microclimate: canopy vs greenhouse air (°C).
THERMAL_DIVERGENCE_CRIT_C: float = float(
    os.environ.get("THERMAL_DIVERGENCE_CRIT_C", "5.0")
)
THERMAL_DIVERGENCE_SEVERE_C: float = float(
    os.environ.get("THERMAL_DIVERGENCE_SEVERE_C", "10.0")
)

# Sensor flatline: variance window (N) and consecutive identical samples.
# At 5-minute sampling, N=12 ≈ 1 h; 24 identical samples ≈ 2 h.
FLATLINE_VARIANCE_WINDOW: int = int(os.environ.get("FLATLINE_VARIANCE_WINDOW", "12"))
FLATLINE_IDENTICAL_STEPS: int = int(os.environ.get("FLATLINE_IDENTICAL_STEPS", "24"))

# Column aliases for optional climate-computer / microclimate context.
SETPOINT_COLUMN_ALIASES: Tuple[str, ...] = (
    "t_setpoint",
    "heating_setpoint",
    "calculated_heating_temp",
    "setpoint_temp",
    "heating_temp",
)
PLANT_TEMP_COLUMN_ALIASES: Tuple[str, ...] = (
    "t_plant",
    "measured_plant_temp",
    "plant_temp",
    "canopy_temp",
)
OUTDOOR_TEMP_COLUMN_ALIASES: Tuple[str, ...] = (
    "t_outdoor",
    "outdoor_temp",
    "outside_temp",
    "ambient_outdoor_temp",
)
SOLAR_COLUMN_ALIASES: Tuple[str, ...] = (
    "i_solar",
    "solar_radiation",
    "radiation_wm2",
    "solar",
    "radiation",
)
MEASURED_HD_COLUMN_ALIASES: Tuple[str, ...] = (
    "measured_hd",
    "humidity_deficit",
    "hd",
    "humidity_deficit_g_m3",
    "hd_g_m3",
)

# =============================================================================
# Noise / Precision Degradation Detection (SCADA spec section 1.4)
# =============================================================================
# Multiplier applied to per-sensor noise targets: alarm when rolling_std
# of the high-pass filtered signal exceeds σ_target × NOISE_ALPHA.
NOISE_ALPHA: float = 3.0

# Per-sensor manufacturer noise floor in raw sensor units (RMS).
# Alarm when rolling high-pass std exceeds target × NOISE_ALPHA.
SENSOR_NOISE_TARGETS: Dict[str, float] = {
    "temperature": 0.05,   # °C RMS
    "humidity": 0.1,       # % RMS
    "co2": 5.0,            # ppm RMS
    "light": 2.0,          # lux RMS
}

# =============================================================================
# Two-Pass Cold-Start Bootstrap (Pass 1 — Physics-Informed Bootstrapping)
# =============================================================================
# Minimum rows required after Pass 1 filtering to proceed with Pass 2.
BOOTSTRAP_MIN_CLEAN_ROWS: int = 100

# Minimum valid (non-NaN) rows required for IsolationForest fitting.
BOOTSTRAP_MIN_IF_ROWS: int = 10

# Drop a gas sensor column from IF input if more than this fraction is null.
BOOTSTRAP_GAS_NULL_THRESHOLD: float = 0.95

# IsolationForest contamination parameter for Pass 1 outlier removal.
BOOTSTRAP_IF_CONTAMINATION: float = 0.04

# Resampling cadence applied to raw SCADA data before bootstrap processing.
BOOTSTRAP_RESAMPLE_INTERVAL: str = "5min"

# Output paths for the bootstrap pipeline.
BOOTSTRAP_CLEAN_PATH: str = "data/bootstrapped_clean.csv"
BOOTSTRAP_ANOMALY_PATH: str = "data/pass1_suspected_anomalies.csv"

# Percentile used to derive the dynamic detection threshold (tau) for Pass 2.
BOOTSTRAP_THRESHOLD_PERCENTILE: float = 99.0

# Cap on the number of training sequences fed to the Pass 2 LSTM-VAE. Cold-start
# training time scales with the resampled row count (≈ the upload's time span);
# an uncapped long-span upload exceeded the worker prepare timeout and drove a
# retrain loop. An even-stride subsample keeps training bounded regardless of
# file size while preserving coverage across the whole range. Override via
# BOOTSTRAP_MAX_TRAIN_SEQUENCES; set to 0 to disable the cap.
BOOTSTRAP_MAX_TRAIN_SEQUENCES: int = int(
    os.environ.get("BOOTSTRAP_MAX_TRAIN_SEQUENCES", "5000")
)

# Psychrometric validation (Tetens equation): max allowable absolute error
# between measured humidity deficit and the value computed from T_air + RH.
# Recalibrated from 0.05 to 0.5 g/m³ after fixing the HD coefficient
# (216.7 → 2167.0 for kPa inputs): the correct-scale HD for typical
# greenhouse conditions (20-30°C, 50-80% RH) ranges ~3-12 g/m³, so
# 0.5 g/m³ tolerance is ≈ 5-15% relative — comparable to the previous
# 0.05 tolerance on the 10x-too-small scale.
# Override via PSYCHROMETRIC_HD_TOLERANCE env var.
PSYCHROMETRIC_HD_TOLERANCE: float = float(
    os.environ.get("PSYCHROMETRIC_HD_TOLERANCE", "0.5")
)  # g/m³

# Bootstrap Pass 1 physics gate: computed HD values above this ceiling are
# considered physically impossible (e.g. RH 5% at 40°C → HD ≈ 40 g/m³).
# Calibrated to the correct HD scale (post-fix); the highest physically
# plausible HD (hot + very dry greenhouse) is ~30 g/m³. Override via
# BOOTSTRAP_PHYSICS_HD_MAX env var.
BOOTSTRAP_PHYSICS_HD_MAX: float = float(
    os.environ.get("BOOTSTRAP_PHYSICS_HD_MAX", "35.0")
)

# Pass-1 MAD robust Z-score cull (Iglewicz–Hoaglin). Rows with any channel
# |z| above this threshold are scrubbed before IsolationForest fit.
BOOTSTRAP_MAD_Z_THRESHOLD: float = float(
    os.environ.get("BOOTSTRAP_MAD_Z_THRESHOLD", "3.5")
)

# Seasonal baseline / scaler (σ_season relative to global σ).
SEASONAL_MIN_SAMPLES: int = int(os.environ.get("SEASONAL_MIN_SAMPLES", "500"))
SEASONAL_SCALE_MIN: float = float(os.environ.get("SEASONAL_SCALE_MIN", "0.5"))
SEASONAL_SCALE_MAX: float = float(os.environ.get("SEASONAL_SCALE_MAX", "2.0"))
SEASONAL_BASELINE_ROW_CAP: int = int(
    os.environ.get("SEASONAL_BASELINE_ROW_CAP", "50000")
)

# Band-breach detector: last-k mean debounce before comparing to [min, max].
BAND_BREACH_DEBOUNCE_K: int = int(os.environ.get("BAND_BREACH_DEBOUNCE_K", "3"))

# CO2 enrichment flag default — conservative False so un-enriched greenhouses are
# never flooded by CO2_BAND_LOW alarms.
#
# Rationale: tomato/cucumber lower CO2 bounds (~600-800 ppm) sit well above ambient
# outdoor air (~420 ppm).  A greenhouse WITHOUT CO2 enrichment equipment runs at
# ambient, so CO2_BAND_LOW would fire on literally every reading, carrying zero
# diagnostic information and causing alarm fatigue.  CO2_BAND_HIGH (excess CO2
# buildup from respiration) needs no special equipment to be actionable and is
# always emitted regardless of this flag.
#
# Growers with enrichment hardware opt in by setting co2_enrichment=true in their
# operational config, unlocking the "enrichment system underperforming" signal.
DEFAULT_CO2_ENRICHMENT: bool = False

# Spike detector velocity limits per sensor (physical units per 5-min step).
SPIKE_DELTA_MAX: Dict[str, float] = {
    "temperature": 5.0,   # °C
    "humidity": 10.0,     # %
    "co2": 50.0,          # ppm
    "light": 200.0,       # lux
}

# =============================================================================
# SCADA Advanced Anomaly Detectors (Phase B)
# =============================================================================

# Spike / Transient Outlier — MAD-based detector.
# spec ref: k=5, c>=2.5 (docs/anomaly_framework_spec.md §1); production uses a
# longer window for a steadier rolling median on live telemetry.
SPIKE_MAD_WINDOW_K: int = int(os.environ.get("SPIKE_MAD_WINDOW_K", "10"))
SPIKE_MAD_TUNING_C: float = float(os.environ.get("SPIKE_MAD_TUNING_C", "3.0"))
# Per-sensor MAD noise floor (physical units). The rolling MAD is clamped to
# at least this value before thresholding: on a near-constant series the MAD
# collapses to ~0 and `delta > c*MAD` flags ordinary measurement noise.
# Floors sit at ~2x the SENSOR_NOISE_TARGETS RMS values.
SPIKE_MAD_FLOOR: Dict[str, float] = {
    "temperature": 0.1,   # °C
    "humidity": 0.2,      # %
    "co2": 10.0,          # ppm
    "light": 4.0,         # lux
}

# Out-of-Bounds — hard physical / hardware limits per sensor.
OOB_LIMITS: Dict[str, Tuple[float, float]] = {
    "temperature": (-40.0, 80.0),
    "humidity": (0.0, 100.0),
    "co2": (0.0, 5000.0),
    "light": (0.0, 200000.0),
}

# Stuck-at — 1-D series epsilon for the document-aligned detector.
STUCK_EPSILON: float = float(os.environ.get("STUCK_EPSILON", "1e-4"))

# CUSUM drift — EMA baseline estimator smoothing factor.
CUSUM_EMA_ALPHA: float = float(os.environ.get("CUSUM_EMA_ALPHA", "0.01"))

# Noise / Precision Degradation — hybrid Butterworth + variance-ratio.
NOISE_HYBRID_WINDOW: int = int(os.environ.get("NOISE_HYBRID_WINDOW", "5"))
NOISE_BASELINE_VARIANCE: Dict[str, float] = {
    # spec ref: sigma_baseline^2=0.02 (docs/anomaly_framework_spec.md §5).
    "temperature": 0.01,   # (°C)² — calibrated sensor noise floor
    "humidity": 0.04,      # (%)²
    "co2": 25.0,           # (ppm)²
    "light": 100.0,        # (lux)²
}
# spec ref: theta_noise=50.0 (docs/anomaly_framework_spec.md §5); production
# uses 10.0 (5x more sensitive) for earlier precision-degradation alerts.
NOISE_RATIO_THRESHOLD: float = float(os.environ.get("NOISE_RATIO_THRESHOLD", "10.0"))

# Bias / Constant Offset — dual-sensor cross-difference.
# spec ref: w=3, theta_bias=1.5°C (docs/anomaly_framework_spec.md §6);
# production uses a longer window and a tighter threshold (3x more sensitive).
BIAS_DUAL_WINDOW: int = int(os.environ.get("BIAS_DUAL_WINDOW", "10"))
BIAS_DUAL_THRESHOLD: float = float(os.environ.get("BIAS_DUAL_THRESHOLD", "0.5"))
# Single-sensor EMA fallback.
BIAS_EMA_SPAN: int = int(os.environ.get("BIAS_EMA_SPAN", "288"))
BIAS_EMA_THRESHOLD: float = float(os.environ.get("BIAS_EMA_THRESHOLD", "2.0"))
# Per-sensor absolute deviation limits for the EMA bias check. A single
# absolute threshold cannot serve sensors whose scales differ by orders of
# magnitude: 2 lux of light jitter is measurement noise, while a 2°C
# temperature offset is a real calibration fault.
BIAS_EMA_THRESHOLD_BY_SENSOR: Dict[str, float] = {
    "temperature": 2.0,   # °C
    "humidity": 5.0,      # %
    "co2": 50.0,          # ppm
    "light": 100.0,       # lux
}

# Spatial Correlation Breakdown — rolling Pearson pair detector.
# spec ref: w=5, theta_corr=0.70 (docs/anomaly_framework_spec.md §7);
# production uses a longer window and a stricter correlation floor.
CORR_BREAKDOWN_WINDOW: int = int(os.environ.get("CORR_BREAKDOWN_WINDOW", "12"))
CORR_BREAKDOWN_THRESHOLD: float = float(os.environ.get("CORR_BREAKDOWN_THRESHOLD", "0.80"))

# Diurnal Profile Deviation — hourly Z-score.
# spec ref: theta_diurnal=3.0 (docs/anomaly_framework_spec.md §9); production
# uses 2.5 (more sensitive) for earlier profile-deviation alerts.
DIURNAL_Z_THRESHOLD: float = float(os.environ.get("DIURNAL_Z_THRESHOLD", "2.5"))
# Auto-built hourly profiles: trailing history window and the minimum reading
# count below which a tenant is considered too new for diurnal baselines.
DIURNAL_PROFILE_HISTORY_DAYS: int = int(
    os.environ.get("DIURNAL_PROFILE_HISTORY_DAYS", "14")
)
DIURNAL_PROFILE_MIN_ROWS: int = int(
    os.environ.get("DIURNAL_PROFILE_MIN_ROWS", "288")
)

# Multivariate joint-implausibility check (rolling-window Mahalanobis).
# 18.47 = chi-square df=4 at p=0.999 — flags only strong joint outliers.
MAHALANOBIS_CHI2_THRESHOLD: float = float(
    os.environ.get("MAHALANOBIS_CHI2_THRESHOLD", "18.47")
)

# Bayesian online change-point detection (level shifts beside CUSUM).
# hazard_lambda = expected run length between changes; min_run = stable
# steps required before a run-length collapse counts as a change-point.
BOCPD_HAZARD_LAMBDA: float = float(os.environ.get("BOCPD_HAZARD_LAMBDA", "250.0"))
BOCPD_MIN_RUN: int = int(os.environ.get("BOCPD_MIN_RUN", "8"))
# A change-point must appear on at least this many sensors simultaneously
# to vote (a single channel's level shift is usually a control action).
BOCPD_MIN_SENSORS: int = int(os.environ.get("BOCPD_MIN_SENSORS", "2"))

# =============================================================================
# OT Network Security Detectors
# =============================================================================

# Max tenants kept in the per-tenant network detector registries (LRU).
NETWORK_REGISTRY_MAX_TENANTS: int = int(
    os.environ.get("NETWORK_REGISTRY_MAX_TENANTS", "500")
)

# RTT / Jitter.
# spec ref: theta_jitter=50ms (docs/anomaly_framework_spec.md §10); production
# uses 20ms (2.5x more sensitive). RTT baseline/multiplier match spec exactly.
RTT_BASELINE_MS: float = float(os.environ.get("RTT_BASELINE_MS", "2.0"))
RTT_MULTIPLIER: float = float(os.environ.get("RTT_MULTIPLIER", "3.0"))
JITTER_THRESHOLD_MS: float = float(os.environ.get("JITTER_THRESHOLD_MS", "20.0"))

# Inter-Packet Interval variation (IAT CV).
# spec ref: w=5 (docs/anomaly_framework_spec.md §11); production uses 20 for
# a steadier CV estimate on jittery links.
IAT_CV_WINDOW: int = int(os.environ.get("IAT_CV_WINDOW", "20"))
# spec ref: theta_CV=1.5, spacing multiplier=3.0 (docs/anomaly_framework_spec.md
# §11); production is more sensitive on both (CV 1.0, spacing 2.5x).
IAT_CV_THRESHOLD: float = float(os.environ.get("IAT_CV_THRESHOLD", "1.0"))
IAT_BASELINE_MEAN: float = float(os.environ.get("IAT_BASELINE_MEAN", "1.0"))
IAT_SPACING_MULTIPLIER: float = float(os.environ.get("IAT_SPACING_MULTIPLIER", "2.5"))

# TCP Handshake timing.
# spec ref: c=2.5 → 12.5ms limit (docs/anomaly_framework_spec.md §12);
# production uses 2.0 → 10ms limit (more sensitive).
TCP_BASELINE_MS: float = float(os.environ.get("TCP_BASELINE_MS", "5.0"))
TCP_DELAY_FACTOR: float = float(os.environ.get("TCP_DELAY_FACTOR", "2.0"))

# Service Response Duration.
# spec ref: N=100 (docs/anomaly_framework_spec.md §14); production uses a
# larger rolling window (1000) for a smoother dynamic P99 threshold.
SERVICE_RESPONSE_WINDOW: int = int(os.environ.get("SERVICE_RESPONSE_WINDOW", "1000"))
# spec ref: P99 (docs/anomaly_framework_spec.md §14); production uses P99.5.
SERVICE_RESPONSE_PERCENTILE: float = float(os.environ.get("SERVICE_RESPONSE_PERCENTILE", "99.5"))
SERVICE_RESPONSE_BASELINE_MS: float = float(os.environ.get("SERVICE_RESPONSE_BASELINE_MS", "120.0"))

# Isolation Forest threat scoring (19-feature).
# spec ref: theta_threat=70.0 (docs/anomaly_framework_spec.md §15); production
# uses 65.0 (more sensitive).
THREAT_SCORE_THRESHOLD: float = float(os.environ.get("THREAT_SCORE_THRESHOLD", "65.0"))
IF_THREAT_N_ESTIMATORS: int = int(os.environ.get("IF_THREAT_N_ESTIMATORS", "200"))
IF_THREAT_CONTAMINATION: float = float(os.environ.get("IF_THREAT_CONTAMINATION", "0.01"))

AUTH_ENABLED: bool = os.environ.get("AUTH_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
TENANT_API_KEYS: Dict[str, str] = _parse_tenant_api_keys()

# Single source of truth for the Redis connection default — previously
# duplicated as a literal in both src/api/main.py (FastAPICache init) and
# src/api/services/live_hub_provider.py (LiveHub publisher/subscriber clients).
REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_CACHE_EXPIRE_SECONDS: int = int(os.environ.get("REDIS_CACHE_EXPIRE_SECONDS", "300"))

# Server-side downsampling (migration plan G1) — readings endpoints.
DOWNSAMPLE_MAX_POINTS_FLOOR: int = int(os.environ.get("DOWNSAMPLE_MAX_POINTS_FLOOR", "100"))
DOWNSAMPLE_MAX_POINTS_CEIL: int = int(os.environ.get("DOWNSAMPLE_MAX_POINTS_CEIL", "10000"))
# Rows whose anomaly_score sits in this top quantile always survive downsampling.
DOWNSAMPLE_ANOMALY_QUANTILE: float = float(os.environ.get("DOWNSAMPLE_ANOMALY_QUANTILE", "0.90"))

# Anomaly aggregation endpoints (migration plan G3/G4).
ANOMALY_ENRICHED_DEFAULT_LIMIT: int = int(
    os.environ.get("ANOMALY_ENRICHED_DEFAULT_LIMIT", "50")
)
ANOMALY_ENRICHED_MAX_LIMIT: int = int(os.environ.get("ANOMALY_ENRICHED_MAX_LIMIT", "500"))
ANOMALY_GROUP_SAMPLE_IDS: int = int(os.environ.get("ANOMALY_GROUP_SAMPLE_IDS", "5"))

# Job progress SSE stream (migration plan G5).
JOB_EVENTS_POLL_SECONDS: float = float(os.environ.get("JOB_EVENTS_POLL_SECONDS", "2.0"))
JOB_EVENTS_MAX_SECONDS: float = float(os.environ.get("JOB_EVENTS_MAX_SECONDS", "3600"))

# === Live Monitoring (SSE) ===
# Mock SCADA data source for development and testing.
MOCK_SCADA_ENABLED: bool = _env_flag("MOCK_SCADA_ENABLED", True)
MOCK_SCADA_SCOPES: str = os.environ.get("MOCK_SCADA_SCOPES", "tenant_1:gh_A")
MOCK_SCADA_INTERVAL_SECONDS: float = float(os.environ.get("MOCK_SCADA_INTERVAL_SECONDS", "2.0"))
MOCK_SCADA_ANOMALY_RATE: float = float(os.environ.get("MOCK_SCADA_ANOMALY_RATE", "0.02"))

# Merge grace period: wait this long for all sources to report before
# emitting a merged reading (handles clock skew between vendors).
LIVE_MERGE_GRACE_SECONDS: float = float(os.environ.get("LIVE_MERGE_GRACE_SECONDS", "1.0"))

# Maximum entries retained in each Redis stream (approximate XADD trimming).
LIVE_REPLAY_BUFFER_SIZE: int = int(os.environ.get("LIVE_REPLAY_BUFFER_SIZE", "1200"))

# SSE heartbeat interval; the hub yields idle ticks at this cadence.
LIVE_HEARTBEAT_SECONDS: float = float(os.environ.get("LIVE_HEARTBEAT_SECONDS", "15.0"))

# Hard cap on a single SSE stream connection.
LIVE_MAX_STREAM_SECONDS: float = float(os.environ.get("LIVE_MAX_STREAM_SECONDS", "3600.0"))

# Whether to persist live readings to the database (disable for dry-run).
LIVE_PERSIST_ENABLED: bool = _env_flag("LIVE_PERSIST_ENABLED", True)

# Flush cadence for the live-to-DB persistence writer.
LIVE_PERSIST_FLUSH_SECONDS: float = float(os.environ.get("LIVE_PERSIST_FLUSH_SECONDS", "10.0"))

# Anomaly threshold for the live scoring path.
#
# IMPORTANT: this MUST NOT inherit ANOMALY_THRESHOLD (0.8, the batch/fused-
# detector scale).  Live scores are raw LSTM-VAE reconstruction errors whose
# noise floor is ~0.017 and demo peaks reach ~0.36 — a completely different
# regime from the batch path.  The default 0.05 is calibrated so that compound
# demo episodes (scoring ≥ 0.08) reliably fire while normal noise (≤ 0.02)
# does not.  Override via the LIVE_ANOMALY_THRESHOLD env var for per-deployment
# tuning; prod already pins 0.05.
LIVE_ANOMALY_THRESHOLD: float = float(
    os.environ.get("LIVE_ANOMALY_THRESHOLD", "0.05")
)

# Minimum seconds between re-emissions of a LiveAnomalyEvent while an anomaly
# episode persists (debounce to avoid flooding the SSE stream).
LIVE_ANOMALY_REEMIT_SECONDS: float = float(
    os.environ.get("LIVE_ANOMALY_REEMIT_SECONDS", "60.0")
)

# JSON list of external data-source definitions (parsed at startup).
LIVE_SOURCES_JSON: str = os.environ.get("LIVE_SOURCES_JSON", "")

# XREAD block timeout in milliseconds (Redis Streams consumer loop).
LIVE_HUB_BLOCK_MS: int = int(os.environ.get("LIVE_HUB_BLOCK_MS", "5000"))

# Forward-fill staleness: if a channel hasn't reported in this many ticks
# (multiples of the scorer's emit_interval), the window is treated as not-warm
# and scoring pauses until the channel returns.
LIVE_FORWARD_FILL_MAX_TICKS: int = int(os.environ.get("LIVE_FORWARD_FILL_MAX_TICKS", "5"))

# --- Demo mode: guaranteed anomaly injection ---
# When enabled, the mock climate source (which carries ML-scored channels)
# will schedule guaranteed episodes at random intervals to ensure the demo
# always shows anomaly activity.  Does not affect non-ML sources.
MOCK_SCADA_DEMO_MODE: bool = _env_flag("MOCK_SCADA_DEMO_MODE", True)
MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS: float = float(
    os.environ.get("MOCK_SCADA_DEMO_ANOMALY_MIN_SECONDS", "60.0")
)
MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS: float = float(
    os.environ.get("MOCK_SCADA_DEMO_ANOMALY_MAX_SECONDS", "120.0")
)
# Sigma multiplier for compound demo episodes — each affected channel is
# offset by DEMO_SPIKE_SIGMA × channel_noise_std.  Empirically tuned so that
# the resulting multi-channel compound episode reliably scores >= 0.08 on the
# baseline LSTM-VAE detector (comfortable margin over a 0.05 live threshold).
MOCK_SCADA_DEMO_SPIKE_SIGMA: float = float(
    os.environ.get("MOCK_SCADA_DEMO_SPIKE_SIGMA", "25.0")
)
# Duration range (ticks) for compound demo episodes. Longer durations fill
# more of the 20-step scoring window, increasing peak score.
MOCK_SCADA_DEMO_COMPOUND_MIN_TICKS: int = int(
    os.environ.get("MOCK_SCADA_DEMO_COMPOUND_MIN_TICKS", "8")
)
MOCK_SCADA_DEMO_COMPOUND_MAX_TICKS: int = int(
    os.environ.get("MOCK_SCADA_DEMO_COMPOUND_MAX_TICKS", "12")
)

# =============================================================================
# SCADA Operational Config (Phase P1)
# =============================================================================
import json as _json


def _parse_json_env(name: str, default: dict) -> dict:
    """Parse a JSON env var; fall back to *default* on missing or parse errors."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return _json.loads(raw)
    except Exception:
        return default


# Crop setpoint profiles: per-channel day/night [lo, hi] bands.
# ``night: null`` means no separate night band — callers fall back to the day band.
# Seeded from Table 1 (Tomato / Cucumber / Sweet Pepper / Strawberry).
# Env-overridable via CROP_SETPOINT_PROFILES (JSON string).
CROP_SETPOINT_PROFILES: dict = _parse_json_env(
    "CROP_SETPOINT_PROFILES",
    {
        "tomato": {
            "temperature": {"day": [18.0, 26.0], "night": [15.0, 20.0]},
            "humidity":    {"day": [60.0, 80.0], "night": None},
            "vpd":         {"day": [0.7, 1.0],   "night": None},
            "co2":         {"day": [600.0, 1100.0], "night": None},
        },
        "cucumber": {
            "temperature": {"day": [24.0, 28.0], "night": [19.0, 22.0]},
            "humidity":    {"day": [70.0, 90.0], "night": None},
            "vpd":         {"day": [0.5, 0.8],   "night": None},
            "co2":         {"day": [800.0, 1000.0], "night": None},
        },
        "sweet_pepper": {
            "temperature": {"day": [24.0, 28.0], "night": [18.0, 22.0]},
            "humidity":    {"day": [60.0, 75.0], "night": None},
            "vpd":         {"day": [0.6, 0.9],   "night": None},
            "co2":         {"day": [600.0, 1000.0], "night": None},
        },
        "strawberry": {
            "temperature": {"day": [18.0, 24.0], "night": [10.0, 16.0]},
            "humidity":    {"day": [65.0, 75.0], "night": None},
            "vpd":         {"day": [0.2, 0.4],   "night": None},
            "co2":         {"day": [800.0, 1000.0], "night": None},
        },
    },
)

# Month → season mapping (string keys "1".."12").
# Transition months 4 (April) and 10 (October) are conservatively mapped to
# 'winter' so season-dependent detector limits use tighter cold-season thresholds
# during ambiguous shoulder periods.
# Env-overridable via DEFAULT_SEASON_MAP (JSON string).
DEFAULT_SEASON_MAP: dict = _parse_json_env(
    "DEFAULT_SEASON_MAP",
    {
        "1": "winter",
        "2": "winter",
        "3": "winter",
        "4": "winter",   # transition — conservative
        "5": "summer",
        "6": "summer",
        "7": "summer",
        "8": "summer",
        "9": "summer",
        "10": "winter",  # transition — conservative
        "11": "winter",
        "12": "winter",
    },
)

# Photoperiod window defining "day" vs "night" setpoint selection (0–23 hours).
# Env-overridable via DEFAULT_DAY_WINDOW (JSON string).
DEFAULT_DAY_WINDOW: dict = _parse_json_env(
    "DEFAULT_DAY_WINDOW",
    {"start_hour": 6, "end_hour": 22},
)
