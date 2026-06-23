# AI Agronomist: API, Dashboard, and Adapter Architecture

This document outlines the architecture for the REST API, the Streamlit monitoring dashboard, the high-level database schema, and the vendor data adapter for the AI Agronomist platform.

## 1. REST API Architecture

The REST API provides endpoints for health checks, data ingestion, and model inference. It is designed to be multi-tenant and scalable.

### 1.1 Endpoints

#### `GET /health`

- **Description**: Performs a basic health check of the API service.
- **Request**: None
- **Response (200 OK)**:
  ```json
  {
    "status": "ok"
  }
  ```
- **Response (503 Service Unavailable)**:
  ```json
  {
    "status": "error",
    "reason": "Service is unhealthy"
  }
  ```

#### `GET /health/db`

- **Description**: Checks the health of the database connection.
- **Request**: None
- **Response (200 OK)**:
  ```json
  {
    "status": "ok"
  }
  ```
- **Response (503 Service Unavailable)**:
  ```json
  {
    "status": "error",
    "reason": "Database connection failed"
  }
  ```

#### `POST /api/upload`

- **Description**: Uploads a batch of sensor data.
- **Request Body**:
  ```json
  {
    "tenant_id": "customer_a",
    "readings": [
      {
        "timestamp": "2026-06-08T18:32:02Z",
        "sensor_id": "gh1-temp-01",
        "temperature": 25.5,
        "humidity": 60.1,
        "light_intensity": 55000,
        "co2_level": 450
      }
    ]
  }
  ```
- **Response (202 Accepted)**:
  ```json
  {
    "status": "queued",
    "message": "Data accepted for processing."
  }
  ```

#### `POST /api/inference`

- **Description**: Requests real-time anomaly detection for a sequence of sensor readings.
- **Request Body**:
  ```json
  {
    "tenant_id": "customer_a",
    "sensor_id": "gh1-temp-01",
    "readings": [
      { "temperature": 25.5, "humidity": 60.1, "light_intensity": 55000, "co2_level": 450 },
      ... 19 more readings
    ]
  }
  ```
- **Response (200 OK)**:
  ```json
  {
    "tenant_id": "customer_a",
    "sensor_id": "gh1-temp-01",
    "is_anomaly": true,
    "reconstruction_error": 0.87
  }
  ```

### 1.2 Error Handling

- **400 Bad Request**: For validation errors (e.g., missing fields, incorrect data types). The response body will contain a `details` field with specific error messages.
- **429 Too Many Requests**: When the rate limit is exceeded.
- **500 Internal Server Error**: For unhandled server-side exceptions.

### 1.3 Rate Limiting

A token bucket algorithm will be implemented using a middleware. Each `tenant_id` will have a separate bucket, allowing for customer-specific rate limits.

## 2. Streamlit Dashboard

The dashboard provides a user-friendly interface for monitoring sensor data, diagnosing anomalies, and labeling incidents for active learning.

### 2.1 Pages

| File | Purpose |
|------|---------|
| `app.py` | Main entry point — sidebar with tenant/greenhouse selector, date range, threshold, API key |
| `1_sensor_data.py` | 4-sensor time series charts with optional **Sync Graphs** mode (2×2 Plotly subplot, shared x-axis) |
| `2_anomalies.py` | Anomaly score timeline + top-20 anomaly table with per-sensor color highlights, hover tooltips, and ✓/✗ feedback buttons |
| `4_metrics.py` | Pipeline observability: detections, FP rate, pattern/confidence distributions from `GET /api/monitoring/metrics` |
| `6_operator_feedback.py` | Operator review queue (open incidents), sequence chart, diagnosis card, label form, learning progress |

### 2.2 Anomaly table (`anomaly_table.py`)

Renders anomalies as `st.columns` rows (not an HTML table) so native Streamlit buttons work alongside HTML cell highlights.

- **Sensor cell colors**: red = range violation, orange = attribution ≥ 0.25, amber = any attribution > 0. Uses `rgba` fills that blend with Streamlit light/dark themes.
- **Hover tooltip**: CSS `.ag-cell` / `.ag-tip` structure injected once via `st.markdown(unsafe_allow_html=True)`. Shows pattern type, affected sensors, and a human-readable hint. Appears after 80 ms, fades in over 120 ms.
- **Action column**: ✓/✗ buttons on every row. For rows with a matched incident, feedback is submitted directly. For unmatched rows, clicking auto-registers via `POST /api/inference/comprehensive` then submits.

### 2.3 Diagnosis enricher (`diagnosis_enricher.py`)

Joins anomaly rows with matched incidents (±15 min window) and produces per-sensor hints:

- With incident: uses `root_cause_details.attribution` for colors, `hints` for tooltip text.
- Without incident: z-score + range fallback for flags; heuristic pattern (`spike` / `drop` / `deviation`) from range violations; friendly hint text generated from actual value vs. range bounds.

### 2.4 State Management

- `st.session_state` — tenant/greenhouse/threshold, date range, feedback results keyed by incident ID or row timestamp.
- `st.cache_data(ttl=60)` — sensor readings and incident range queries cached 60 s.

### 2.5 Multi-Tenant UI

Tenant and greenhouse selectors in the app sidebar filter all data displayed across pages. All API calls include `customer_id` / `tenant_id` parameters.

## 3. Database Schema

### 3.1 `SensorReading` Table

```sql
CREATE TABLE SensorReadings (
    timestamp TIMESTAMPTZ NOT NULL,
    tenant_id VARCHAR(255) NOT NULL,
    sensor_id VARCHAR(255) NOT NULL,
    temperature FLOAT,
    humidity FLOAT,
    light_intensity INTEGER,
    co2_level INTEGER,
    PRIMARY KEY (timestamp, tenant_id, sensor_id)
);
```

### 3.2 Indexes

- A composite index on `(tenant_id, sensor_id, timestamp)` will be created to optimize queries for a specific sensor within a tenant.

## 4. Vendor Adapter Architecture

A modular adapter system will be designed to ingest data from various sensor vendors. Each adapter will be a separate Python module responsible for:

1.  Fetching data from the vendor's API or data source.
2.  Transforming the data into the canonical `SensorReading` format.
3.  Posting the normalized data to the `/api/upload` endpoint.
    This design decouples the core application from vendor-specific data formats.
