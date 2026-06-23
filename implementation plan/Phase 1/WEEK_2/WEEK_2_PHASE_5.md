Continue Week 2 Phase 5 - Write integration tests for complete API workflow and coverage reporting.

Phase 5 (after Phase 4):

- qa-engineer: Write integration and API tests covering full workflow.
  Output: tests/integration/test_end_to_end.py, tests/api/test_health.py, tests/api/test_upload.py, tests/api/test_inference.py, pytest.ini (updated), coverage_report.html
  
  Deliverables:
  
  1. tests/api/test_health.py
     Health endpoint tests:
     
     - test_health_endpoint_returns_ok
       GET /health → status 200, body {"status": "ok"}
     
     - test_health_db_endpoint_connection_ok
       GET /health/db → status 200, {"status": "ok", "database": "connected"}
     
     - test_health_db_endpoint_connection_fails
       (Simulate DB down) → status 503, {"status": "error", "database": "disconnected"}
     
     - test_health_endpoint_no_auth_needed
       No Authorization header → still returns 200
  
  2. tests/api/test_upload.py
     CSV upload endpoint tests:
     
     - test_upload_valid_csv
       POST /api/upload with valid CSV
       Expected: 200, {"uploaded": 1000, "errors": 0, ...}
     
     - test_upload_missing_tenant_id
       POST without tenant_id
       Expected: 400, error message
     
     - test_upload_invalid_csv_format
       POST with malformed CSV (wrong columns)
       Expected: 422, validation error
     
     - test_upload_nan_values
       CSV with NaN in temperature column
       Expected: 400 or 422 with error message
     
     - test_upload_out_of_range
       Temperature = 55°C (max 50)
       Expected: 400, validation error
     
     - test_upload_empty_file
       POST with 0-byte CSV
       Expected: 400, "File is empty"
     
     - test_upload_large_file
       POST 100,000 rows CSV
       Expected: 200, all rows uploaded
     
     - test_upload_data_persists
       Upload CSV, query database
       Expected: SensorReading entries created
     
     - test_upload_multi_tenant_isolation
       Upload for tenant_1, then tenant_2
       Query: tenant_1 should NOT see tenant_2's data
       Expected: Proper filtering
     
     - test_upload_response_ranges
       Check response has min/max for each sensor
       Expected: ranges.temperature, ranges.humidity, etc. present
  
  3. tests/api/test_inference.py
     Model inference endpoint tests:
     
     - test_inference_returns_scores
       POST /api/inference with sequences
       Expected: 200, anomaly_scores array returned
     
     - test_inference_score_shape
       Input 150 sequences → output 150 scores
       Expected: len(anomaly_scores) == 150
     
     - test_inference_score_range
       All scores in [0, 1]
       Expected: min=0, max=1
     
     - test_inference_missing_tenant_id
       POST without tenant_id
       Expected: 400, error
     
     - test_inference_invalid_sequence_shape
       POST with wrong sequence dimensions
       Expected: 422, validation error
     
     - test_inference_nan_in_input
       Input contains NaN
       Expected: 400, validation error
     
     - test_inference_saves_to_database
       Run inference, check anomaly_score column updated
       Expected: SensorReading.anomaly_score populated
     
     - test_inference_model_not_found
       (Simulate missing checkpoint)
       Expected: 503, "Model not loaded"
     
     - test_inference_response_includes_high_anomalies
       Response has high_anomalies field with flagged sequences
       Expected: high_anomalies list present
  
  4. tests/integration/test_end_to_end.py
     End-to-end workflow test:
     
     - test_csv_upload_to_inference_to_dashboard
       
       Step 1: Upload CSV via /api/upload
         Input: 1000 rows sensor data (CSV file)
         Expected: 200, 1000 uploaded
       
       Step 2: Verify data in database
         Query: SELECT COUNT(*) FROM sensor_reading
         Expected: count >= 1000
       
       Step 3: Run inference via /api/inference
         Input: sequences from uploaded data
         Expected: 200, anomaly scores
       
       Step 4: Verify scores in database
         Query: SELECT * WHERE anomaly_score IS NOT NULL
         Expected: scores populated
       
       Step 5: Query dashboard data
         GET from database (what dashboard uses)
         Expected: data ready for visualization
       
       Step 6: Check multi-tenant isolation
         Create tenant_1 and tenant_2 data
         Query as tenant_1
         Expected: Only see tenant_1's data
     
     - test_workflow_with_vendor_adapter
       Use CSVAdapter.fetch_data() instead of raw CSV
       Expected: Same results, vendor abstraction works
     
     - test_error_recovery
       Upload invalid CSV → error → upload valid CSV → success
       Expected: Second upload succeeds (no leftover state)
  
  5. pytest.ini (UPDATED)
     Configuration:
     
     [pytest]
     testpaths = tests
     python_files = test_*.py
     python_classes = Test*
     python_functions = test_*
     markers =
         unit: Unit tests
         integration: Integration tests
         api: API endpoint tests
         slow: Slow tests (skipped by default)
         requires_db: Tests requiring database
     
     addopts = 
         -v
         --tb=short
         --cov=src
         --cov-report=html:htmlcov
         --cov-report=term-missing
         --cov-fail-under=85
         --strict-markers
  
  6. Coverage report:
     Run: pytest tests/ --cov=src --cov-report=html
     Verify:
       - src/api/ >85% coverage
       - src/db/ >85% coverage
       - src/vendors/ >85% coverage
       - src/models/inference.py >85% coverage
     
     Generate: htmlcov/index.html (viewable in browser)
  
  Test fixtures needed:
  - @pytest.fixture
    def test_client():
      FastAPI TestClient for endpoint testing
    
  - @pytest.fixture
    def sample_csv_file(tmp_path):
      Create valid CSV in temp directory
    
  - @pytest.fixture
    def test_database():
      Create in-memory SQLite for tests
    
  - @pytest.fixture
    def test_sequences():
      Generate 150 valid (20, 4) sequences
    
  - @pytest.fixture
    def multiple_tenants(test_database):
      Create test data for tenant_1, tenant_2, tenant_3
  
  Requirements:
  - All tests pass: pytest tests/ -v
  - Coverage >= 85% on new code
  - Integration test covers full CSV → inference → dashboard workflow
  - Multi-tenant isolation tested
  - Error cases covered (invalid input, missing fields, etc.)
  - No hardcoded paths (use fixtures, config)

auto-proceed