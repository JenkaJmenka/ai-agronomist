Continue Week 2 Phase 2 - Implement API endpoints and database migrations.

Phase 2 (after Phase 1):

- backend-engineer: Implement CSV upload endpoint, model inference endpoint, and database seeding.
  Output: src/api/routes/upload.py, src/api/routes/inference.py, src/api/routes/health.py, src/db/seeds.py, alembic/versions/001_initial.py
  
  Deliverables:
  
  1. src/api/routes/health.py
     Implement endpoints:
     
     - GET /health
       Response: {"status": "ok"}
       Status code: 200
     
     - GET /health/db
       Check database connection
       Try: SELECT 1 FROM sensor_reading LIMIT 1
       Response on success: {"status": "ok", "database": "connected"}
       Response on failure: {"status": "error", "database": "disconnected"}
       Status code: 200 if ok, 503 if db down
  
  2. src/api/routes/upload.py
     Implement endpoint:
     
     - POST /api/upload
       Accept multipart form:
       - file: CSV file (required)
       - tenant_id: str (from query param or form, required)
       - greenhouse_id: str (optional, default='default')
       
       CSV format:
       - Columns: timestamp, temperature, humidity, co2, light
       - Row example: 2026-06-08T10:30:00Z, 25.5, 65.2, 450, 800
       
       Process:
       1. Parse CSV
       2. Validate data:
          - All rows have 5 columns
          - No NaN values
          - Temperature in range 0-50
          - Humidity in range 0-100
          - CO2 in range 0-1000
          - Light in range 0-2000
       3. Group into sequences of 20 timesteps
       4. Create SensorReading entries in database
       5. Return summary
       
       Response:
       {
         "uploaded": 1000,
         "errors": 0,
         "skipped": 0,
         "ranges": {
           "temperature": {"min": 20.1, "max": 28.5},
           "humidity": {"min": 45.0, "max": 78.0},
           "co2": {"min": 350, "max": 520},
           "light": {"min": 0, "max": 1200}
         },
         "tenant_id": "customer_1",
         "greenhouse_id": "greenhouse_1"
       }
       
       Status code: 200 (success), 400 (bad file), 422 (validation error)
  
  3. src/api/routes/inference.py
     Implement endpoint:
     
     - POST /api/inference
       Request body:
       {
         "tenant_id": "customer_1",
         "greenhouse_id": "greenhouse_1",
         "sequences": [
           [20.5, 65.0, 450, 800, ...20 values per sequence],
           [21.0, 64.5, 455, 750, ...],
           ...
         ]
       }
       
       Process:
       1. Validate input (sequences shape, tenant_id)
       2. Load trained LSTM-VAE model (use inference.py)
       3. Predict anomaly scores for each sequence
       4. Save scores to SensorReading table (anomaly_score column)
       5. Return results
       
       Response:
       {
         "tenant_id": "customer_1",
         "greenhouse_id": "greenhouse_1",
         "sequences_processed": 150,
         "anomalies_detected": 3,
         "anomaly_scores": [0.02, 0.05, 0.95, 0.03, ...],
         "high_anomalies": [
           {"sequence_index": 2, "score": 0.95, "timestamp": "2026-06-08T10:35:00Z"}
         ]
       }
       
       Status code: 200 (success), 400 (bad input), 503 (model not loaded)
  
  4. src/db/seeds.py
     Implement test data seeding:
     
     - Function: seed_test_data()
       Creates sample SensorReading entries for testing
       
       - 100 normal sequences (anomaly_score < 0.3)
       - 10 high-anomaly sequences (anomaly_score > 0.8)
       - Multiple tenants (tenant_1, tenant_2, tenant_3)
       - Multiple greenhouses per tenant
       
       Use: from src.db.seeds import seed_test_data
            seed_test_data(session, num_samples=100)
  
  5. alembic/versions/001_initial.py
     Create initial migration:
     
     - Use auto-generate: alembic revision --autogenerate -m "Initial schema"
     - The migration should:
       * Create sensor_reading table
       * Set up indexes on (tenant_id, timestamp)
       * Add constraints (NOT NULL on required fields)
     
     Verify migration works:
     - alembic upgrade head
     - Table created in database
  
  6. src/api/main.py (UPDATED)
     - Add route imports:
       from src.api.routes import health, upload, inference
     - Register routes:
       app.include_router(health.router)
       app.include_router(upload.router)
       app.include_router(inference.router)
     - Add startup hook to run migrations:
       @app.on_event("startup")
       async def startup():
           # Run alembic upgrade head
           from alembic.config import Config
           alembic_cfg = Config("alembic.ini")
           command.upgrade(alembic_cfg, "head")
  
  Requirements:
  - Type hints on all functions
  - Docstrings on all endpoints
  - Error handling (try/except, meaningful error messages)
  - Database transactions (rollback on error)
  - No hardcoded paths (use config.py)
  - Logging on each endpoint (info, warnings, errors)

auto-proceed