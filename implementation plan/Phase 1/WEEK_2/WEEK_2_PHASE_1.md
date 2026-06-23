Build Week 2 Phase 1 - Design and infrastructure setup for REST API, dashboard, and vendor abstraction.

Phase 1 (parallel):

- architect: Design REST API, Streamlit dashboard, and vendor adapter architecture.
  Output: docs/architecture/api_dashboard_design.md, docs/architecture/vendor_adapter_design.md
  
  Produce two design documents:
  
  1. docs/architecture/api_dashboard_design.md
     Content:
     - REST API endpoint specifications
       * GET /health (simple health check)
       * GET /health/db (database connectivity check)
       * POST /api/upload (CSV file upload)
       * POST /api/inference (model inference on sequences)
     - Request/response schemas for each endpoint
     - Error handling strategy (validation errors, server errors, etc.)
     - Rate limiting strategy (prepare for Phase 1.5 multi-customer)
     
     - Streamlit Dashboard page specifications
       * Pages: sensor_data.py, anomalies.py, thresholds.py, metrics.py
       * State management (session state, caching)
       * Real-time refresh strategy
       * Multi-tenant filtering UI
       * Threshold adjustment flow (how user changes threshold, saves to DB)
     
     - Database schema (high-level)
       * SensorReading table structure
       * Indexes needed
       * Multi-tenant support (tenant_id)
  
  2. docs/architecture/vendor_adapter_design.md
     Content:
     - Abstract vendor interface specification
       * What methods must each adapter implement?
       * Input/output contracts
       * Error handling per vendor
     - CSV adapter (current implementation)
     - Priva adapter (stub for Phase 1.5)
     - Data normalization layer (how different vendors' data gets standardized)
     - Future extensibility (what other vendors might integrate?)

- backend-engineer: Implement FastAPI application, SQLAlchemy models, Pydantic schemas, and Alembic setup.
  Output: src/api/main.py, src/db/models.py, src/api/schemas/sensor_data.py, src/db/migrations/env.py
  
  Deliverables:
  
  1. src/api/main.py
     - FastAPI() app instantiation
     - CORS middleware (allow localhost:3000, :8501 for Streamlit)
     - Dependency injection for database session
     - Global exception handlers (422, 404, 500)
     - Startup hook: initialize database (create tables if not exist)
     - Shutdown hook: cleanup resources
     - Request ID middleware (for logging)
  
  2. src/db/models.py
     - SQLAlchemy ORM class: SensorReading
       * id (primary key, int)
       * tenant_id (str, indexed) - for multi-tenancy
       * greenhouse_id (str)
       * timestamp (datetime, indexed)
       * temperature (float)
       * humidity (float)
       * co2 (float)
       * light (float)
       * anomaly_score (Optional[float])
       * created_at (datetime, default=now)
     - Base class for all models
     - Table creation via SQLAlchemy declarative
  
  3. src/api/schemas/sensor_data.py
     - Pydantic v2 models:
       * SensorReadingCreate (for POST requests)
       * SensorReadingResponse (for GET responses)
       * UploadRequest (CSV file metadata)
       * UploadResponse (upload result summary)
       * InferenceRequest (sequence data for prediction)
       * InferenceResponse (anomaly scores + metadata)
     - All models must have type hints and docstrings
  
  4. src/db/migrations/ (Alembic setup)
     - Initialize: alembic init
     - Configure env.py to use SQLAlchemy models
     - Setup for auto-generating migrations from models

- ml-engineer: Implement improved inference engine and vendor adapter base class.
  Output: src/models/inference.py (updated), src/vendors/base.py, src/vendors/csv_adapter.py
  
  Deliverables:
  
  1. src/models/inference.py (enhanced from Week 1)
     - class AnomalyDetector:
       * __init__(checkpoint_path: str, device: str = 'cpu')
       * load_checkpoint() - load trained LSTM-VAE model
       * predict(sequences: torch.Tensor) -> np.ndarray
         - Input: (batch, seq_len, input_dim) tensor
         - Output: (batch,) anomaly scores [0, 1]
       * predict_batch(sequences: List[np.ndarray]) -> np.ndarray
         - Handle variable-length batches
       * get_reconstruction_error(x, x_recon) -> float
       * cache_model_in_memory() - keep loaded model across requests
     - Error handling:
       * Model not found → clear error message
       * Invalid input shapes → reshape or fail gracefully
       * NaN/Inf in output → return error
  
  2. src/vendors/base.py
     - Abstract base class: DataSourceAdapter
       * async def read_data(source: str, start: datetime, end: datetime) -> np.ndarray
         - Returns: (num_sequences, seq_len, input_dim)
       * async def normalize_data(data: np.ndarray) -> np.ndarray
         - Standardize to [0, 1] range
       * async def validate_data(data: np.ndarray) -> Tuple[bool, str]
         - Check for NaN, Inf, out-of-range values
       * Error handling strategy per vendor
  
  3. src/vendors/csv_adapter.py
     - class CSVAdapter(DataSourceAdapter):
       * __init__() - no special config for CSV
       * read_data(file: UploadFile) -> np.ndarray
         - Parse CSV (columns: timestamp, temp, humidity, co2, light)
         - Return shape (N, 20, 4) sequences
       * validate_data() - check for required columns, data types
       * Normalize using config ranges

- devops-lead: Setup structured logging and monitoring infrastructure.
  Output: src/logging/config.py, src/api/middleware/logging.py, pyproject.toml (updated)
  
  Deliverables:
  
  1. src/logging/config.py
     - Logging configuration (using structlog or logging)
     - Output format: JSON (not pretty-printed)
     - Log levels: DEBUG, INFO, WARNING, ERROR
     - Log file rotation (daily, max 10 files)
     - Sensitive field redaction (hide API keys, tokens)
     - Example log entry:
       {
         "timestamp": "2026-06-08T10:30:00Z",
         "level": "INFO",
         "logger": "api.routes.inference",
         "message": "inference completed",
         "request_id": "abc-123",
         "duration_ms": 45,
         "model": "lstm_vae_v1",
         "batch_size": 32,
         "anomaly_detected": true
       }
  
  2. src/api/middleware/logging.py
     - FastAPI middleware for request/response logging
     - Capture:
       * Request: method, path, headers (without secrets)
       * Response: status code, duration, size
       * Request ID (propagate through logs)
     - Don't log request body (can be large)
  
  3. Metrics collection in inference:
     - Inference latency (milliseconds)
     - Batch size processed
     - Model throughput (sequences/sec)
     - Error count (failed inferences)
     - These metrics exported to logs as JSON
  
  4. pyproject.toml (or requirements.txt)
     - Add: structlog (or logging library of choice)
     - Add: python-json-logger
     - Ensure all dependencies pinned

auto-proceed