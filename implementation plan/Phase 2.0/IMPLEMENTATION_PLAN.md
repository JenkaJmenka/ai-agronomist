# IMPLEMENTATION PLAN: Two-Pass Anomaly Detection System

## 1. SYSTEM ARCHITECTURE

### 1.1 High-Level Overview

```
┌──────────────────────────────────────────────────────────────┐
│                         REST API Gateway                      │
├──────────────────────────────────────────────────────────────┤
│  POST /api/anomaly/train                                     │
│  POST /api/anomaly/detect                                    │
│  POST /api/anomaly/analyze                                   │
│  GET /api/anomaly/patterns/list                              │
│  GET /api/anomaly/patterns/{id}                              │
│  DELETE /api/anomaly/patterns/reset                          │
└──────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
        ↓                   ↓                   ↓
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ CSV Parser   │   │ Pass 1 Engine│   │ Pass 2 Engine│
│              │   │ (Learn)      │   │ (Detect)     │
├──────────────┤   ├──────────────┤   ├──────────────┤
│ • Column map │   │ • Stats calc │   │ • 5 detectors│
│ • Validation │   │ • Patterns   │   │ • GDN check  │
│ • Normalize  │   │ • Thresholds │   │ • LSTM-AE    │
└──────────────┘   │ • Save JSON  │   │ • Physics val│
                   └──────────────┘   │ • Voting     │
                                      └──────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
        ↓                   ↓                   ↓
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ Model Store  │   │ Results Store│   │ Database     │
│ /models/     │   │ /results/    │   │ SQLite/PG    │
└──────────────┘   └──────────────┘   └──────────────┘
```

### 1.2 Component Responsibilities

| Component | Responsibility |
|-----------|-----------------|
| **CSV Parser** | Map columns to sensors, validate format, normalize ranges |
| **Pass 1 (Learn)** | Calculate baseline patterns, thresholds, statistics from entire dataset |
| **Pass 2 (Detect)** | Scan entire dataset, run 5 detectors, aggregate votes, apply physics validation |
| **Model Store** | Persist pattern profiles (JSON) with versioning |
| **Results Store** | Export anomalies CSV, training metrics |
| **Database** | Track jobs, model history, metadata |

---

## 2. DATABASE SCHEMA

### 2.1 Tables

```sql
-- Customers/Organizations
CREATE TABLE customers (
  id TEXT PRIMARY KEY,
  name TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Pattern profiles (versioned)
CREATE TABLE patterns (
  id TEXT PRIMARY KEY,  -- pattern_v4_2026-08-20
  customer_id TEXT FOREIGN KEY,
  version INT,
  created_at TIMESTAMP,
  rows_analyzed INT,
  status TEXT,  -- 'active', 'archived'
  file_path TEXT,  -- /models/{customer_id}/pattern_{id}.json
  sensors TEXT[],  -- JSON array
  FOREIGN KEY (customer_id) REFERENCES customers(id)
);

-- Jobs (train/detect/analyze)
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,  -- job_2026-08-20_abc123
  customer_id TEXT,
  job_type TEXT,  -- 'train', 'detect', 'analyze'
  status TEXT,  -- 'queued', 'running', 'complete', 'failed'
  created_at TIMESTAMP,
  completed_at TIMESTAMP,
  input_file TEXT,  -- Original CSV filename
  rows_processed INT,
  output_file TEXT,  -- Results CSV path
  pattern_id_used TEXT,  -- For detect jobs
  pattern_id_created TEXT,  -- For train/analyze jobs
  error_message TEXT,
  FOREIGN KEY (customer_id) REFERENCES customers(id),
  FOREIGN KEY (pattern_id_used) REFERENCES patterns(id),
  FOREIGN KEY (pattern_id_created) REFERENCES patterns(id)
);

-- Anomalies (indexed for fast queries)
CREATE TABLE anomalies (
  id TEXT PRIMARY KEY,
  job_id TEXT,
  timestamp TEXT,
  sensor TEXT,
  fault_type TEXT,  -- spike, stuck, drift, precision, polling
  confidence FLOAT,  -- 0-1
  physics_valid BOOLEAN,
  detector_agreement INT,  -- 1-5
  details TEXT,  -- JSON with detector outputs
  FOREIGN KEY (job_id) REFERENCES jobs(id),
  INDEX (job_id, timestamp),
  INDEX (sensor, fault_type)
);
```

### 2.2 Example Data

```json
{
  "patterns": {
    "id": "pattern_v3_2026-08-15",
    "customer_id": "gh_001",
    "version": 3,
    "created_at": "2026-08-15T10:30:00Z",
    "rows_analyzed": 105121,
    "status": "active"
  },
  
  "jobs": {
    "id": "job_2026-08-20_abc123",
    "customer_id": "gh_001",
    "job_type": "analyze",
    "status": "complete",
    "rows_processed": 105121,
    "pattern_id_created": "pattern_v4_2026-08-20"
  },
  
  "anomalies": {
    "id": "anom_12345",
    "job_id": "job_2026-08-20_abc123",
    "timestamp": "2026-06-16T16:12:00Z",
    "sensor": "greenhouse_temp",
    "fault_type": "spike",
    "confidence": 0.95,
    "physics_valid": true,
    "detector_agreement": 5
  }
}
```

---

## 3. API SPECIFICATION

### 3.1 MODE 1: Train (PASS 1 Only)

```
POST /api/anomaly/train

Headers:
  Content-Type: multipart/form-data

Parameters:
  file: <CSV file>
  customer_id: string (required)

Response (200 OK):
{
  "status": "training_complete",
  "job_id": "job_2026-08-20_001",
  "pattern_id": "pattern_v4_2026-08-20",
  "rows_analyzed": 105121,
  "sensors_detected": ["temperature", "humidity", "light", "co2", "plant_temp"],
  
  "thresholds": {
    "spike": {
      "temperature": 2.5,
      "humidity": 15.0,
      "light": 50.0,
      "co2": 100.0,
      "plant_temp": 2.0
    },
    "stuck": {
      "temperature": 0.01,
      "humidity": 0.1,
      "light": 0.5,
      "co2": 1.0,
      "plant_temp": 0.01
    },
    "drift": {
      "temperature": 0.05,
      "humidity": 0.1
    },
    "precision": {
      "temperature": 0.1,
      "humidity": 0.5
    },
    "polling": {
      "null_tolerance": 0.02
    }
  },
  
  "statistics": {
    "temperature": {
      "mean": 22.5,
      "std": 4.2,
      "min": 15.0,
      "max": 35.0,
      "median": 23.0,
      "q25": 20.0,
      "q75": 25.0
    },
    "humidity": {...},
    "light": {...},
    "co2": {...},
    "plant_temp": {...}
  },
  
  "correlations": {
    "light_temperature": 0.78,
    "temperature_humidity": -0.65,
    "temperature_plant_temp": 0.92
  },
  
  "diurnal_patterns": {
    "temperature": {
      "peak_hour": 15,
      "peak_value": 28.5,
      "trough_hour": 6,
      "trough_value": 18.0
    },
    "light": {...}
  }
}

Errors:
  400: Invalid CSV format
  413: File too large
  422: No numeric columns detected
  500: Processing error
```

---

### 3.2 MODE 2: Detect (PASS 2 Only)

```
POST /api/anomaly/detect

Headers:
  Content-Type: multipart/form-data

Parameters:
  file: <CSV file>
  customer_id: string (required)
  pattern_id: string (optional, uses latest if not specified)

Response (200 OK):
{
  "status": "detection_complete",
  "job_id": "job_2026-08-20_002",
  "pattern_id_used": "pattern_v3_2026-08-15",
  "rows_scanned": 105121,
  "anomalies_found": 47,
  
  "results": {
    "csv_file": "/results/detect_2026-08-20_abc123.csv",
    "summary": {
      "spike": 12,
      "stuck": 8,
      "drift": 15,
      "precision": 7,
      "polling": 5
    },
    "confidence_distribution": {
      "high_confidence": 38,  // >= 0.75
      "medium_confidence": 6,  // 0.5-0.75
      "low_confidence": 3   // < 0.5
    },
    "physics_validation": {
      "valid": 40,
      "likely_false_positive": 7
    }
  },
  
  "sample_anomalies": [
    {
      "timestamp": "2026-06-16T16:12:00Z",
      "sensor": "greenhouse_temp",
      "fault_type": "spike",
      "confidence": 0.95,
      "physics_valid": true,
      "detector_agreement": 5,
      "details": "Temperature jumped 8°C in 5 minutes (expected range: 1-2°C)"
    },
    {
      "timestamp": "2026-06-17T10:30:00Z",
      "sensor": "humidity",
      "fault_type": "stuck",
      "confidence": 0.88,
      "physics_valid": false,
      "detector_agreement": 3,
      "details": "Humidity stuck at 29% for 2 hours (likely false positive: climate control action)"
    }
  ]
}

Errors:
  400: Invalid CSV format
  404: Pattern not found
  422: Sensor mismatch with pattern
  500: Detection error
```

---

### 3.3 MODE 3: Analyze (PASS 1 + PASS 2)

```
POST /api/anomaly/analyze

Headers:
  Content-Type: multipart/form-data

Parameters:
  file: <CSV file>
  customer_id: string (required)

Response (200 OK):
{
  "status": "analysis_complete",
  "job_id": "job_2026-08-20_003",
  
  "pass1_results": {
    "pattern_id": "pattern_v4_2026-08-20",
    "rows_analyzed": 105121,
    "sensors_detected": [...],
    "thresholds": {...},
    "statistics": {...},
    "correlations": {...}
  },
  
  "pass2_results": {
    "anomalies_found": 47,
    "results_csv": "/results/analyze_2026-08-20_abc123.csv",
    "summary": {...},
    "confidence_distribution": {...},
    "physics_validation": {...}
  }
}

Errors:
  400: Invalid CSV format
  413: File too large
  422: No numeric columns detected
  500: Processing error
```

---

### 3.4 Pattern Management

```
GET /api/anomaly/patterns/list

Response (200 OK):
{
  "customer_id": "gh_001",
  "patterns": [
    {
      "id": "pattern_v4_2026-08-20",
      "version": 4,
      "created_at": "2026-08-20T14:30:00Z",
      "rows_analyzed": 105121,
      "status": "active",
      "sensors": ["temperature", "humidity", "light", "co2", "plant_temp"]
    },
    {
      "id": "pattern_v3_2026-08-15",
      "version": 3,
      "created_at": "2026-08-15T10:30:00Z",
      "rows_analyzed": 105121,
      "status": "archived",
      "sensors": ["temperature", "humidity", "light", "co2", "plant_temp"]
    },
    {
      "id": "pattern_v2_2026-07-01",
      "version": 2,
      "created_at": "2026-07-01T09:00:00Z",
      "rows_analyzed": 102000,
      "status": "archived",
      "sensors": ["temperature", "humidity", "light", "co2"]
    }
  ]
}

---

GET /api/anomaly/patterns/{pattern_id}

Response (200 OK):
{
  "id": "pattern_v3_2026-08-15",
  "version": 3,
  "created_at": "2026-08-15T10:30:00Z",
  "rows_analyzed": 105121,
  "thresholds": {...},
  "statistics": {...},
  "correlations": {...},
  "diurnal_patterns": {...}
}

---

DELETE /api/anomaly/patterns/reset

Parameters:
  customer_id: string (required)
  confirm: boolean (must be true)

Response (200 OK):
{
  "status": "reset_complete",
  "message": "All patterns for customer gh_001 deleted",
  "patterns_deleted": 4,
  "timestamp": "2026-08-20T15:00:00Z"
}

Errors:
  400: Missing confirm parameter
  403: Unauthorized
  500: Reset failed
```

---

## 4. CODE STRUCTURE

### 4.1 Directory Layout

```
ai-agronomist/
├── backend/
│   ├── api/
│   │   ├── routes/
│   │   │   ├── anomaly.py          # Train/Detect/Analyze endpoints
│   │   │   ├── patterns.py         # Pattern management endpoints
│   │   │   └── health.py           # Health check
│   │   ├── middleware/
│   │   │   ├── auth.py
│   │   │   └── error_handler.py
│   │   └── main.py                 # FastAPI app
│   │
│   ├── core/
│   │   ├── config.py               # Settings, paths, constants
│   │   └── constants.py            # Detector parameters
│   │
│   ├── detectors/
│   │   ├── __init__.py
│   │   ├── base.py                 # BaseDetector class
│   │   ├── spike.py                # Spike detector
│   │   ├── stuck.py                # Stuck sensor detector
│   │   ├── drift.py                # Calibration drift detector
│   │   ├── precision.py            # Precision degradation detector
│   │   ├── polling.py              # Polling dropout detector
│   │   ├── multivariate.py         # GDN + LSTM-AE
│   │   └── physics_validator.py    # Thermodynamic checks
│   │
│   ├── engines/
│   │   ├── pass1_learn.py          # Pattern learning engine
│   │   ├── pass2_detect.py         # Anomaly detection engine
│   │   └── voting.py               # Detector aggregation & voting
│   │
│   ├── parsers/
│   │   ├── csv_parser.py           # Parse any CSV
│   │   └── column_mapper.py        # Map columns to sensors
│   │
│   ├── database/
│   │   ├── models.py               # SQLAlchemy models
│   │   ├── session.py              # DB connection
│   │   └── crud.py                 # CRUD operations
│   │
│   ├── storage/
│   │   ├── model_store.py          # Save/load pattern JSON
│   │   └── result_store.py         # Save anomalies CSV
│   │
│   ├── models/
│   │   ├── pattern.py              # Pattern profile dataclass
│   │   ├── anomaly.py              # Anomaly dataclass
│   │   └── job.py                  # Job dataclass
│   │
│   └── utils/
│       ├── logger.py
│       ├── validators.py
│       └── helpers.py
│
├── tests/
│   ├── unit/
│   │   ├── test_detectors.py
│   │   ├── test_engines.py
│   │   └── test_parsers.py
│   ├── integration/
│   │   ├── test_api.py
│   │   └── test_e2e.py
│   └── fixtures/
│       ├── sample_data.csv
│       └── expected_outputs.json
│
├── models/
│   └── {customer_id}/
│       ├── pattern_v1_*.json
│       ├── pattern_v2_*.json
│       └── pattern_latest.json
│
├── results/
│   └── {customer_id}/
│       ├── analyze_2026-08-20_*.csv
│       ├── detect_2026-08-20_*.csv
│       └── summary_*.json
│
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
│
└── requirements.txt
```

---

## 5. IMPLEMENTATION PHASES

### PHASE 1: Foundation (Week 1)

**Goal:** Core infrastructure & Pass 1 engine with seasonal support

**Deliverables:**
- [ ] FastAPI skeleton with error handling
- [ ] Database setup (SQLAlchemy models) with feedback tracking
- [ ] CSV parser (column detection, validation)
- [ ] Pass 1 engine (statistics calculation)
- [ ] **Seasonal pattern support** (4 baseline patterns: spring/summer/autumn/winter)
- [ ] Model store (JSON persistence with versioning)
- [ ] Train endpoint (POST /api/anomaly/train)

**Files to Create:**
```
api/main.py
api/routes/anomaly.py
core/config.py
parsers/csv_parser.py
engines/pass1_learn.py                    # UPDATED: Seasonal patterns
engines/pass1_seasonal_splitter.py        # NEW: Split data by season
storage/model_store.py
database/models.py                         # UPDATED: Add feedback table
database/crud.py
```

**Testing:**
- [ ] Unit tests for CSV parser
- [ ] Unit tests for Pass 1 engine (seasonal)
- [ ] Integration test: Upload CSV → Get 4 seasonal patterns
- [ ] Verify thresholds differ per season

---

### PHASE 2: Detectors & Active Learning (Week 2)

**Goal:** Implement 5 univariate detectors + operator feedback loop + adaptive thresholds

**Deliverables:**
- [ ] Spike detector (first-order difference)
- [ ] Stuck detector (variance analysis)
- [ ] Drift detector (CUSUM)
- [ ] Precision detector (noise floor)
- [ ] Polling detector (null pattern)
- [ ] Detector voting aggregator
- [ ] **Operator feedback collection** (mark true/false anomalies)
- [ ] **Feedback storage & processing**
- [ ] **Threshold optimization engine** (grid search for best params)

**Files to Create:**
```
detectors/base.py
detectors/spike.py
detectors/stuck.py
detectors/drift.py
detectors/precision.py
detectors/polling.py
engines/voting.py
models/active_learner.py                  # NEW: Operator feedback
engines/threshold_optimizer.py             # NEW: Adaptive thresholds
api/routes/feedback.py                     # NEW: Feedback endpoint
```

**Testing:**
- [ ] Unit test each detector independently
- [ ] Unit test voting logic
- [ ] Unit test feedback recording
- [ ] Unit test threshold optimization
- [ ] Test on synthetic anomalies (spike, stuck, etc.)

---

### PHASE 3: Advanced Features (Week 3)

**Goal:** Multivariate analysis + physics validation + explainability

**Deliverables:**
- [ ] GDN (Graph Deviation Network) placeholder/wrapper
- [ ] LSTM-AE (reconstruction error calculation)
- [ ] Physics validator (thermodynamics checks)
- [ ] Detector agreement scoring
- [ ] **SHAP-based explainability** (explain why anomaly flagged)
- [ ] **Anomaly clustering** (group related anomalies)

**Files to Create:**
```
detectors/multivariate.py
detectors/physics_validator.py
models/gdn_wrapper.py
models/lstm_ae_wrapper.py
models/explainability.py                  # NEW: SHAP explanations
engines/anomaly_clustering.py              # NEW: Cluster related anomalies
```

**Testing:**
- [ ] Unit test multivariate detectors
- [ ] Unit test physics validation
- [ ] Unit test SHAP explanations
- [ ] Unit test clustering logic
- [ ] Test combined scoring

---

### PHASE 4: Pass 2 & Detection API (Week 4)

**Goal:** Complete anomaly detection pipeline with multi-perspective analysis

**Deliverables:**
- [ ] Pass 2 engine (row-by-row scanning with seasonal awareness)
- [ ] Detect endpoint (POST /api/anomaly/detect)
- [ ] Analyze endpoint (POST /api/anomaly/analyze with seasonal patterns)
- [ ] Results export (anomalies CSV with clusters + explanations)
- [ ] Pattern list endpoint (GET /api/anomaly/patterns/list)
- [ ] Reset endpoint (DELETE /api/anomaly/patterns/reset)
- [ ] Feedback endpoint (POST /api/anomaly/feedback)
- [ ] Retraining trigger logic (auto-retrain on 20+ corrections)

**Files to Create:**
```
engines/pass2_detect.py                   # UPDATED: Seasonal + clustering
engines/pass2_detect_seasonal.py          # NEW: Season-aware detection
api/routes/patterns.py
storage/result_store.py                   # UPDATED: Export clusters + explanations
models/pattern.py                         # UPDATED: Seasonal patterns
models/anomaly.py                         # UPDATED: Add explanation + cluster
models/job.py
models/feedback.py                        # NEW: Feedback model
```

**Testing:**
- [ ] Unit test Pass 2 engine (seasonal)
- [ ] Integration test: Upload CSV → Get anomalies with clusters
- [ ] End-to-end test: Analyze (train + detect)
- [ ] Test pattern history
- [ ] Test feedback loop trigger

---

### PHASE 5: Transfer Learning & Optimization (Week 5)

**Goal:** Multi-customer learning + performance tuning

**Deliverables:**
- [ ] **Transfer learning framework** (shared knowledge across customers)
- [ ] **Model aggregation** (combine learning from all customers)
- [ ] **Fine-tuning pipeline** (adapt shared model to new customer)
- [ ] **Performance optimization** (caching, vectorization)
- [ ] **Comprehensive error handling**
- [ ] **Logging & monitoring**
- [ ] **Documentation**

**Files to Create:**
```
models/transfer_learning.py                # NEW: Cross-customer learning
engines/model_aggregation.py               # NEW: Combine customer patterns
engines/fine_tuning.py                     # NEW: Adapt to new customer
api/middleware/auth.py
api/middleware/error_handler.py
utils/logger.py
utils/cache.py                             # NEW: Pattern caching
```

**Testing:**
- [ ] Unit test transfer learning
- [ ] Integration test: Train on customer A, adapt to customer B
- [ ] Performance testing (caching effectiveness)
- [ ] Load testing (large CSV files)

---

### PHASE 6: Polish & Deployment (Week 6)

**Goal:** Production hardening and containerization

**Deliverables:**
- [ ] Docker containerization
- [ ] Security hardening (auth, rate limiting, input validation)
- [ ] API documentation (Swagger)
- [ ] Performance benchmarking
- [ ] Monitoring & alerting setup
- [ ] Deployment scripts

**Files to Create:**
```
docker/Dockerfile
docker/docker-compose.yml
docker/.env.example
deployment/kubernetes/deployment.yaml     # Optional
docs/API.md
docs/DEPLOYMENT.md
scripts/deploy.sh
tests/performance_benchmark.py
```

**Testing:**
- [ ] Security audit
- [ ] Load testing (1000 requests/sec)
- [ ] Error scenario testing
- [ ] Documentation review

---

## 6. KEY IMPLEMENTATION DETAILS

### 6.1 Pass 1: Seasonal Pattern Learning

**Algorithm:**
```python
def pass1_learn_seasonal(df: DataFrame, customer_id: str) -> Dict[str, PatternProfile]:
    """Learn separate patterns for each season (spring/summer/autumn/winter)"""
    
    # Step 0: Define seasons by month
    seasons = {
        'spring': [3, 4, 5],      # Mar-May (moderate growth)
        'summer': [6, 7, 8],      # Jun-Aug (peak growth, high cooling)
        'autumn': [9, 10, 11],    # Sep-Nov (decline, moderate temps)
        'winter': [12, 1, 2]      # Dec-Feb (dormancy, high heating)
    }
    
    df['month'] = pd.to_datetime(df['timestamp']).dt.month
    patterns = {}
    
    # Step 1: For each season, learn independent pattern
    for season_name, months in seasons.items():
        
        # Filter to seasonal data only
        df_season = df[df['month'].isin(months)]
        
        if len(df_season) < 100:  # Need minimum samples
            logger.warning(f"Insufficient {season_name} data: {len(df_season)} samples")
            continue
        
        # Detect numeric columns
        numeric_cols = df_season.select_dtypes(include=[float, int]).columns
        
        # Step 2: Calculate statistics (per season)
        season_stats = {}
        season_thresholds = {}
        
        for col in numeric_cols:
            season_stats[col] = {
                'mean': df_season[col].mean(),
                'std': df_season[col].std(),
                'min': df_season[col].min(),
                'max': df_season[col].max(),
                'median': df_season[col].median(),
                'q25': df_season[col].quantile(0.25),
                'q75': df_season[col].quantile(0.75)
            }
            
            # Calculate thresholds (learned from seasonal data)
            season_thresholds['spike'] = season_thresholds.get('spike', {})
            season_thresholds['spike'][col] = season_stats[col]['std'] * 2.5
            
            season_thresholds['stuck'] = season_thresholds.get('stuck', {})
            season_thresholds['stuck'][col] = season_stats[col]['std'] ** 2 * 0.01
            
            season_thresholds['drift'] = season_thresholds.get('drift', {})
            season_thresholds['drift'][col] = season_stats[col]['std'] * 3.0
        
        # Step 3: Detect seasonal diurnal patterns
        seasonal_diurnal = {}
        if 'timestamp' in df_season.columns:
            for col in numeric_cols:
                hourly_mean = df_season.groupby(df_season['timestamp'].dt.hour)[col].mean()
                seasonal_diurnal[col] = {
                    'peak_hour': int(hourly_mean.idxmax()),
                    'peak_value': float(hourly_mean.max()),
                    'trough_hour': int(hourly_mean.idxmin()),
                    'trough_value': float(hourly_mean.min()),
                    'hourly_profile': hourly_mean.tolist()  # Full 24h profile
                }
        
        # Step 4: Calculate seasonal correlations
        seasonal_correlations = df_season[numeric_cols].corr()
        
        # Step 5: Save seasonal pattern
        pattern = PatternProfile(
            season=season_name,
            sensors=numeric_cols.tolist(),
            statistics=season_stats,
            thresholds=season_thresholds,
            diurnal=seasonal_diurnal,
            correlations=seasonal_correlations,
            rows_analyzed=len(df_season),
            created_at=now()
        )
        
        patterns[season_name] = pattern
        logger.info(f"Learned {season_name} pattern from {len(df_season)} samples")
    
    return patterns
```

---

### 6.2 Pass 2: Seasonal-Aware Anomaly Detection

**Algorithm:**
```python
def pass2_detect_seasonal(df: DataFrame, 
                         patterns_seasonal: Dict[str, PatternProfile]) -> List[Anomaly]:
    """Detect anomalies using season-appropriate patterns"""
    
    anomalies = []
    df['month'] = pd.to_datetime(df['timestamp']).dt.month
    
    season_map = {
        1: 'winter', 2: 'winter',
        3: 'spring', 4: 'spring', 5: 'spring',
        6: 'summer', 7: 'summer', 8: 'summer',
        9: 'autumn', 10: 'autumn', 11: 'autumn',
        12: 'winter'
    }
    
    for idx, row in df.iterrows():
        timestamp = row.get('timestamp', idx)
        month = pd.to_datetime(timestamp).month
        season = season_map[month]
        
        # Get appropriate seasonal pattern
        pattern = patterns_seasonal.get(season)
        if not pattern:
            continue  # Skip if season has no pattern
        
        detectors_triggered = []
        
        # Run 5 univariate detectors with SEASONAL thresholds
        for sensor in pattern.sensors:
            if sensor not in row.index:
                continue
            
            value = row[sensor]
            
            # Detector 1: Spike (seasonal threshold)
            spike_threshold = pattern.thresholds['spike'][sensor]
            if abs(value - pattern.statistics[sensor]['mean']) > spike_threshold:
                detectors_triggered.append({
                    'type': 'spike',
                    'sensor': sensor,
                    'confidence': calculate_spike_confidence(value, pattern, sensor),
                    'details': f"Value {value} exceeds threshold {spike_threshold}"
                })
            
            # Detector 2: Stuck (seasonal variance)
            stuck_threshold = pattern.thresholds['stuck'][sensor]
            window_variance = calculate_window_variance(df, idx, sensor)
            if window_variance < stuck_threshold:
                detectors_triggered.append({
                    'type': 'stuck',
                    'sensor': sensor,
                    'confidence': 0.9,
                    'details': f"Variance {window_variance} below threshold {stuck_threshold}"
                })
            
            # Detectors 3-5: Similar seasonal logic
            # ... drift_detector, precision_detector, polling_detector
        
        # Multivariate checks
        if gdn_detector.check(row, pattern):
            detectors_triggered.append({
                'type': 'multivariate_gdn',
                'confidence': gdn_detector.confidence
            })
        
        if lstm_ae_detector.check(row, pattern):
            detectors_triggered.append({
                'type': 'multivariate_lstm',
                'confidence': lstm_ae_detector.confidence
            })
        
        # Aggregate votes
        if detectors_triggered:
            
            agreement = len(detectors_triggered)
            avg_confidence = aggregate_detector_votes(detectors_triggered)
            
            # Physics validation
            physics_valid = physics_validator.check(row, timestamp, pattern)
            if not physics_valid:
                avg_confidence *= 0.6  # Reduce if physics invalid
            
            # Determine primary fault type
            fault_type = Counter([d['type'] for d in detectors_triggered]).most_common(1)[0][0]
            
            # Generate explanation (SHAP)
            explanation = explainability.explain(row, pattern, detectors_triggered)
            
            anomaly = Anomaly(
                timestamp=timestamp,
                sensor=fault_type,  # Or primary affected sensor
                fault_type=fault_type,
                confidence=avg_confidence,
                physics_valid=physics_valid,
                detector_agreement=agreement,
                season=season,  # NEW: Track which season
                details=detectors_triggered,
                explanation=explanation  # NEW: Why flagged
            )
            
            # Only include if confidence > 0.3
            if anomaly.confidence > 0.3:
                anomalies.append(anomaly)
    
    # NEW: Cluster related anomalies
    anomaly_clusters = cluster_anomalies(anomalies)
    
    # Attach cluster info to anomalies
    for anomaly in anomalies:
        anomaly.cluster_id = find_cluster(anomaly, anomaly_clusters)
    
    return anomalies
```

---

### 6.3 Operator Feedback with Auto-Retrain

**Algorithm:**
```python
class OperatorFeedbackLearner:
    """Collect corrections, auto-retrain when criteria met"""
    
    def __init__(self, customer_id: str, retrain_threshold: int = 20):
        self.customer_id = customer_id
        self.retrain_threshold = retrain_threshold
        self.feedback_buffer = []
        self.operator_accuracy = {}
    
    def record_feedback(self,
                       job_id: str,
                       timestamp: str,
                       sensor: str,
                       is_true_anomaly: bool,
                       operator_id: str,
                       reason: str = None) -> Dict:
        """Operator marks anomaly: true/false positive"""
        
        feedback = {
            'job_id': job_id,
            'timestamp': timestamp,
            'sensor': sensor,
            'is_true_anomaly': is_true_anomaly,
            'operator_id': operator_id,
            'reason': reason,  # "vent_opening", "sensor_failure", etc.
            'created_at': now()
        }
        
        self.feedback_buffer.append(feedback)
        db.insert('operator_feedback', feedback)
        
        # Update operator accuracy (exponential moving average)
        if operator_id not in self.operator_accuracy:
            self.operator_accuracy[operator_id] = 0.5  # Start neutral
        
        # Will be updated when correctness verified
        # accuracy = 0.2 * is_correct + 0.8 * old_accuracy
        
        # Check: should we trigger retraining?
        if self._should_retrain():
            return self.trigger_retraining()
        
        return {
            'status': 'feedback_recorded',
            'total_feedback': len(self.feedback_buffer),
            'needed_for_retrain': max(0, self.retrain_threshold - len(self.feedback_buffer))
        }
    
    def _should_retrain(self) -> bool:
        """Retrain when conditions met"""
        
        # Condition 1: Enough feedback
        if len(self.feedback_buffer) < self.retrain_threshold:
            return False
        
        # Condition 2: Diverse feedback (multiple types)
        fault_types = set([f.get('reason') for f in self.feedback_buffer if f.get('reason')])
        if len(fault_types) < 2:
            return False
        
        # Condition 3: Operator consensus (>80% accurate)
        if not self.feedback_buffer:
            return False
        
        avg_accuracy = mean(self.operator_accuracy.values()) if self.operator_accuracy else 0.5
        if avg_accuracy < 0.80:
            return False
        
        return True
    
    def trigger_retraining(self) -> Dict:
        """Retrain with operator-corrected data"""
        
        # Get high-quality feedback only
        high_quality = [
            f for f in self.feedback_buffer
            if self.operator_accuracy.get(f['operator_id'], 0.5) > 0.80
        ]
        
        logger.info(f"Retraining with {len(high_quality)} corrected samples")
        
        # Re-learn patterns (Pass 1) excluding false positives
        # Re-detect (Pass 2) on corrected baseline
        # Validate improvement before accepting
        
        new_patterns = relearn_with_corrections(high_quality)
        
        # Validate: Does new model perform better?
        validation_accuracy = validate_model(new_patterns, high_quality)
        old_accuracy = validate_model(self.old_patterns, high_quality)
        
        if validation_accuracy > old_accuracy * 1.05:  # 5% improvement
            # Accept new model
            self.old_patterns = new_patterns
            self.feedback_buffer = []  # Reset feedback buffer
            
            return {
                'status': 'retrain_complete',
                'new_pattern_id': new_patterns.id,
                'accuracy_improvement': validation_accuracy - old_accuracy,
                'message': f'Model improved {(validation_accuracy - old_accuracy)*100:.1f}%'
            }
        else:
            # Reject (no improvement)
            return {
                'status': 'retrain_rejected',
                'message': 'New model not better than current',
                'improvement': validation_accuracy - old_accuracy
            }
```

---

### 6.4 Adaptive Threshold Optimization

**Algorithm:**
```python
def optimize_thresholds_from_feedback(feedback_history: List[Dict]) -> Dict[str, float]:
    """Grid-search best thresholds using operator feedback"""
    
    spike_multipliers = np.arange(1.5, 4.5, 0.5)      # [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    stuck_variances = [0.001, 0.005, 0.01, 0.05]
    drift_cusums = [0.02, 0.05, 0.1, 0.15]
    
    best_f1 = -1
    best_params = {}
    
    for spike_mult in spike_multipliers:
        for stuck_var in stuck_variances:
            for drift_cusum in drift_cusums:
                
                params = {
                    'spike_multiplier': spike_mult,
                    'stuck_variance': stuck_var,
                    'drift_cusum': drift_cusum
                }
                
                # Run detectors with these parameters on feedback data
                predictions = run_detectors_with_params(feedback_history, params)
                
                # Compare against ground truth (operator feedback)
                tp = sum(1 for p, f in zip(predictions, feedback_history) 
                        if p == True and f['is_true_anomaly'] == True)
                fp = sum(1 for p, f in zip(predictions, feedback_history) 
                        if p == True and f['is_true_anomaly'] == False)
                fn = sum(1 for p, f in zip(predictions, feedback_history) 
                        if p == False and f['is_true_anomaly'] == True)
                
                # Calculate F1 score
                precision = tp / (tp + fp) if (tp + fp) > 0 else 0
                recall = tp / (tp + fn) if (tp + fn) > 0 else 0
                f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0
                
                if f1 > best_f1:
                    best_f1 = f1
                    best_params = params
    
    logger.info(f"Optimal thresholds found with F1={best_f1:.3f}")
    return best_params
```

---

### 6.5 SHAP-Based Explainability

**Algorithm:**
```python
def explain_anomaly_with_shap(anomaly: Anomaly,
                             pattern: PatternProfile,
                             background_samples: np.ndarray) -> Dict:
    """Generate SHAP explanation for why anomaly was flagged"""
    
    from shap import KernelExplainer
    
    # Create feature vector
    feature_names = pattern.sensors
    anomaly_features = np.array([anomaly.values[s] for s in feature_names]).reshape(1, -1)
    
    # Create SHAP explainer (uses background normal samples)
    explainer = KernelExplainer(
        model=lambda x: run_detectors_fast(x, pattern),
        data=background_samples[:100]  # Background: normal samples
    )
    
    # Get SHAP values
    shap_values = explainer.shap_values(anomaly_features)
    
    # Rank features by importance
    feature_importance = sorted(
        zip(feature_names, abs(shap_values[0])),
        key=lambda x: x[1],
        reverse=True
    )
    
    # Generate explanation
    explanation = {
        'primary_cause': feature_importance[0][0],
        'confidence': anomaly.confidence,
        'contributing_features': [
            {
                'sensor': fname,
                'contribution': float(fval),
                'direction': 'increase' if shap_values[0][i] > 0 else 'decrease'
            }
            for i, (fname, fval) in enumerate(feature_importance[:5])
        ],
        'recommendation': get_recommendation_from_shap(feature_importance)
    }
    
    return explanation
```

---

### 6.6 Anomaly Clustering

**Algorithm:**
```python
def cluster_anomalies(anomalies: List[Anomaly]) -> List[AnomalyCluster]:
    """Group related anomalies by time + sensor"""
    
    from sklearn.cluster import DBSCAN
    from sklearn.preprocessing import StandardScaler
    
    # Create feature matrix
    X = []
    for anomaly in anomalies:
        timestamp_float = pd.to_datetime(anomaly.timestamp).timestamp()
        sensor_id = hash(anomaly.sensor) % 1000  # Encode sensor name
        
        X.append([
            timestamp_float,
            sensor_id,
            anomaly.confidence
        ])
    
    X = np.array(X)
    
    # Normalize
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    
    # Cluster with DBSCAN (time-aware)
    clustering = DBSCAN(eps=0.5, min_samples=2)
    labels = clustering.fit_predict(X_scaled)
    
    # Group anomalies by cluster
    clusters = {}
    for anomaly, label in zip(anomalies, labels):
        if label not in clusters:
            clusters[label] = []
        clusters[label].append(anomaly)
    
    # Create cluster summaries
    cluster_summaries = []
    for cluster_id, group in clusters.items():
        if cluster_id == -1:  # Noise
            continue
        
        cluster_summary = AnomalyCluster(
            id=f"cluster_{cluster_id}",
            anomalies=group,
            time_start=min(a.timestamp for a in group),
            time_end=max(a.timestamp for a in group),
            affected_sensors=list(set(a.sensor for a in group)),
            likely_cause=infer_root_cause(group),
            duration_minutes=calculate_duration(group),
            severity=calculate_cluster_severity(group),
            confidence=mean(a.confidence for a in group)
        )
        
        cluster_summaries.append(cluster_summary)
    
    # Sort by severity
    cluster_summaries.sort(key=lambda c: c.severity, reverse=True)
    
    return cluster_summaries
```

---

**Weighted Consensus:**
```python
def aggregate_detector_votes(detectors_triggered: List[Dict]) -> float:
    """
    Confidence scoring with consensus weighting:
    - 1 detector: confidence × 0.4
    - 2 detectors: confidence × 0.7
    - 3+ detectors: confidence × 1.0
    """
    
    if len(detectors_triggered) == 0:
        return 0.0
    
    avg_confidence = mean([d['confidence'] for d in detectors_triggered])
    
    if len(detectors_triggered) == 1:
        weight = 0.4
    elif len(detectors_triggered) == 2:
        weight = 0.7
    else:  # 3+
        weight = 1.0
    
    final_confidence = avg_confidence * weight
    
    return min(final_confidence, 1.0)  # Cap at 1.0
```

### 6.4 Physics Validation

**Thermodynamic Constraints:**
```python
def validate_against_physics(row: Series, timestamp: str, pattern: PatternProfile) -> bool:
    """
    Check if anomaly violates physical constraints.
    Return True if valid, False if likely false positive.
    """
    
    # Rule 1: Plant temp cannot exceed greenhouse temp by >5°C
    if row.get('plant_temp', 0) > row.get('greenhouse_temp', 0) + 5:
        return False  # Physically impossible
    
    # Rule 2: Humidity rate of change
    # Max expected change in 5 minutes: ±15 percentage points
    if abs(row['humidity_change_5min']) > 15:
        return False
    
    # Rule 3: Temperature rate of change
    # Max expected during vent operation: ±2°C per 5 minutes
    # During climate control: ±3°C allowed
    if abs(row['temp_change_5min']) > 3:
        return False
    
    # Rule 4: Vapor pressure consistency
    # Check: e_s >= e_a (saturation >= actual)
    saturation_vapor_pressure = calculate_saturation_vp(row['greenhouse_temp'])
    actual_vapor_pressure = calculate_actual_vp(row['humidity'], row['greenhouse_temp'])
    
    if actual_vapor_pressure > saturation_vapor_pressure:
        return False  # Impossible humidity
    
    # Rule 5: Light intensity drop-off at night
    # If time is night (0:00-6:00), light should be near 0
    hour = parse_hour_from_timestamp(timestamp)
    if hour < 6 and row['light_intensity'] > 10:
        return False  # Unlikely (unless grow lights)
    
    return True  # Passes all physical checks
```

---

## 7. TESTING STRATEGY

### 7.1 Unit Tests

```python
# tests/unit/test_detectors.py
def test_spike_detector_detects_jump():
    """Spike detector should flag 8°C jump in 5 minutes"""
    values = [22.0, 22.1, 22.2, 30.0]  # 8°C jump
    detector = SpikeDetector()
    result = detector.check(values[-1], pattern, 'temperature')
    assert result.triggered == True
    assert result.confidence > 0.9

def test_stuck_detector_detects_constant():
    """Stuck detector should flag zero variance"""
    values = [29.0, 29.0, 29.0, 29.0]
    detector = StuckDetector()
    result = detector.check(values, pattern, 'humidity')
    assert result.triggered == True

def test_drift_detector_detects_gradual_change():
    """Drift detector should flag gradual offset"""
    values = [22.0, 22.5, 23.0, 23.5, 24.0, 24.5]
    detector = DriftDetector()
    result = detector.check(values, pattern, 'temperature')
    assert result.triggered == True

def test_voting_aggregation():
    """Detector voting should weight by consensus"""
    detectors = [
        {'type': 'spike', 'confidence': 0.95},
        {'type': 'drift', 'confidence': 0.92}
    ]
    confidence = aggregate_detector_votes(detectors)
    assert 0.6 < confidence < 0.95  # 2 detectors: 0.7 weight

def test_physics_validation_rejects_impossible():
    """Physics validator should reject plant_temp > greenhouse_temp + 5"""
    row = Series({'plant_temp': 35, 'greenhouse_temp': 28})
    valid = physics_validator.validate(row, '2026-06-16 12:00', pattern)
    assert valid == False
```

### 7.2 Integration Tests

```python
# tests/integration/test_api.py
def test_train_api_complete_flow(client):
    """Upload CSV → Get pattern profile"""
    with open('tests/fixtures/sample_data.csv', 'rb') as f:
        response = client.post(
            '/api/anomaly/train',
            files={'file': f},
            data={'customer_id': 'test_001'}
        )
    
    assert response.status_code == 200
    data = response.json()
    assert 'pattern_id' in data
    assert data['rows_analyzed'] == 1000
    assert 'thresholds' in data
    assert 'statistics' in data

def test_detect_api_complete_flow(client, trained_pattern):
    """Upload CSV → Get anomalies"""
    with open('tests/fixtures/sample_data.csv', 'rb') as f:
        response = client.post(
            '/api/anomaly/detect',
            files={'file': f},
            data={
                'customer_id': 'test_001',
                'pattern_id': trained_pattern
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'detection_complete'
    assert 'results_csv' in data
    assert data['anomalies_found'] >= 0

def test_analyze_api_complete_flow(client):
    """Upload CSV → Train + Detect"""
    with open('tests/fixtures/sample_data.csv', 'rb') as f:
        response = client.post(
            '/api/anomaly/analyze',
            files={'file': f},
            data={'customer_id': 'test_001'}
        )
    
    assert response.status_code == 200
    data = response.json()
    assert 'pass1_results' in data
    assert 'pass2_results' in data
    assert 'pattern_id' in data['pass1_results']
    assert 'anomalies_found' in data['pass2_results']

def test_pattern_history_api(client):
    """List patterns → Get specific pattern"""
    response = client.get(
        '/api/anomaly/patterns/list',
        params={'customer_id': 'test_001'}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data['patterns']) >= 0
    
    if data['patterns']:
        pattern_id = data['patterns'][0]['id']
        
        response = client.get(f'/api/anomaly/patterns/{pattern_id}')
        assert response.status_code == 200
        assert 'thresholds' in response.json()

def test_reset_patterns_api(client):
    """Reset deletes all patterns"""
    response = client.delete(
        '/api/anomaly/patterns/reset',
        params={
            'customer_id': 'test_001',
            'confirm': True
        }
    )
    
    assert response.status_code == 200
    assert response.json()['status'] == 'reset_complete'
```

### 7.3 End-to-End Tests

```python
# tests/integration/test_e2e.py
def test_e2e_full_workflow():
    """Complete workflow: train → detect → reset"""
    
    # Step 1: Train on dataset 1
    response1 = client.post('/api/anomaly/train', ...)
    pattern_v1 = response1.json()['pattern_id']
    assert pattern_v1 is not None
    
    # Step 2: Detect on dataset 2 using pattern v1
    response2 = client.post('/api/anomaly/detect', ...)
    anomalies = response2.json()['anomalies_found']
    assert anomalies >= 0
    
    # Step 3: Train again (creates v2)
    response3 = client.post('/api/anomaly/train', ...)
    pattern_v2 = response3.json()['pattern_id']
    assert pattern_v1 != pattern_v2
    
    # Step 4: List patterns (should have v1 + v2)
    response4 = client.get('/api/anomaly/patterns/list')
    patterns = response4.json()['patterns']
    assert len(patterns) == 2
    
    # Step 5: Reset
    response5 = client.delete('/api/anomaly/patterns/reset', confirm=True)
    assert response5.json()['status'] == 'reset_complete'
    
    # Step 6: Verify reset (no patterns)
    response6 = client.get('/api/anomaly/patterns/list')
    patterns = response6.json()['patterns']
    assert len(patterns) == 0
```

---

## 8. DEPLOYMENT

### 8.1 Docker Setup

```dockerfile
# docker/Dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker/docker-compose.yml
version: '3.8'

services:
  api:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./models:/app/models
      - ./results:/app/results
    environment:
      DATABASE_URL: postgresql://postgres:password@db:5432/anomaly_db
      LOG_LEVEL: INFO
    depends_on:
      - db
  
  db:
    image: postgres:15
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
      POSTGRES_DB: anomaly_db
    volumes:
      - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
```

### 8.2 Environment Variables

```
DATABASE_URL=postgresql://user:pass@localhost:5432/anomaly_db
LOG_LEVEL=INFO
MAX_UPLOAD_SIZE_MB=500
MODEL_STORAGE_PATH=/models
RESULTS_STORAGE_PATH=/results
DETECTOR_SPIKE_MULTIPLIER=2.5
DETECTOR_STUCK_THRESHOLD=0.01
ANOMALY_CONFIDENCE_THRESHOLD=0.3
```

---

## 9. SUCCESS CRITERIA

### 9.1 Functional Requirements

- [ ] **Train API**: Upload CSV → Get pattern profile with learned thresholds
- [ ] **Detect API**: Upload CSV + pattern ID → Get anomalies list
- [ ] **Analyze API**: Upload CSV → Train + Detect on same dataset
- [ ] **Pattern History**: List, retrieve, reset patterns
- [ ] **5 Detectors**: Spike, stuck, drift, precision, polling all working
- [ ] **Multivariate**: GDN + LSTM-AE integrated
- [ ] **Physics Validation**: Reduces confidence for impossible physics
- [ ] **Voting**: Detector consensus scoring applied
- [ ] **CSV Export**: Anomalies exported with all fields

### 9.2 Non-Functional Requirements

- [ ] **Performance**: Train <5 min on 100K rows, Detect <2 min
- [ ] **Reliability**: 99.9% uptime, graceful error handling
- [ ] **Security**: Auth, rate limiting, input validation
- [ ] **Scalability**: Support 1M+ rows with pagination
- [ ] **Documentation**: API docs (Swagger), code comments

---

## 10. ROLLOUT PLAN

### Week-by-Week Timeline

**Week 1 - PHASE 1: Foundation + Seasonal Patterns**
- Deploy: FastAPI skeleton, DB, seasonal pattern learning
- Outcome: Train endpoint returns 4 seasonal patterns
- Status: Ready for Phase 2

**Week 2 - PHASE 2: Detectors + Active Learning**
- Deploy: 5 univariate detectors, operator feedback, adaptive thresholds
- Outcome: Feedback endpoint + auto-retrain logic working
- Status: Ready for Phase 3

**Week 3 - PHASE 3: Advanced Features**
- Deploy: GDN, LSTM-AE, SHAP explanations, anomaly clustering
- Outcome: Results include explanations + clustered anomalies
- Status: Ready for Phase 4

**Week 4 - PHASE 4: Detection Pipeline**
- Deploy: Pass 2 engine with seasonal awareness, all APIs live
- Outcome: Full analyze → train + detect + explain + cluster working
- Status: Ready for Phase 5

**Week 5 - PHASE 5: Transfer Learning**
- Deploy: Cross-customer learning, fine-tuning, performance optimization
- Outcome: New customer deployment 4x faster (1 week → 2 days)
- Status: Ready for Phase 6

**Week 6 - PHASE 6: Production Hardening**
- Deploy: Docker, security, monitoring, documentation
- Outcome: Production-ready system
- Status: **LIVE**

### Launch & Monitoring

**Beta Launch (Week 6):**
- Deploy with 1-2 pilot customers (Portugal + Netherlands)
- Daily monitoring: Error rates, inference speed, anomaly accuracy
- Weekly sync: Gather operator feedback on false positives

**Feedback Loop (Weeks 7-8):**
- Collect 50+ operator corrections
- Auto-retrain models with feedback
- Measure improvement: baseline vs. retrained accuracy
- Tune seasonal thresholds per greenhouse

**General Availability (Week 9+):**
- After 2-week production trial + accuracy validation
- Scale to N customers
- Establish SLA: <2min inference, <5% false positive rate

---

## 11. BACKLOG (Future, Post-Launch)

### 🔴 BACKLOG ITEM 1: Streaming/Real-Time Detection (Live Priva Connection)

**Status:** DEFERRED (No live customer data source currently)

**What it is:**
```
Real-time anomaly detection as data streams in:
CSV Upload Stream → Detection Worker → Alert immediately
(Instead of batch: wait 5 min for complete file)
```

**Implementation (When Live Data Available):**
```
Components:
├─ Kafka topics per customer (anomalies_customer_id)
├─ Streaming detection worker (Docker container)
├─ Real-time alert service (Email, SMS, Dashboard)
└─ WebSocket endpoint for live anomaly feed

Architecture:
Priva API (live data stream)
    ↓
Kafka (event queue)
    ↓
Detection Worker (processes events)
    ↓
Alert Channels (Email/SMS/Dashboard)
```

**Effort:** 1 week
**Trigger:** When first live customer onboarded
**Files:** `workers/streaming_detector.py`, `alert_service.py`, Kafka config

---

### 🟡 BACKLOG ITEM 2: Predictive Warnings

**Status:** NICE-TO-HAVE

**What it is:**
Detect degradation patterns before failure:
- "Sensor noise increasing 10% per day → likely failure in 7 days"
- "Temperature stability decreasing → imminent controller failure"

**Implementation:**
```
Trend analysis on detector outputs:
├─ Per detector, track over time (7-30 days)
├─ Fit linear regression to detector_score vs time
├─ If slope > threshold: Alert "degradation detected"
└─ Predict failure date: t = (1.0 - current_score) / slope
```

**Effort:** 3 days
**Trigger:** Customer requests "predict failures"

---

### 🟡 BACKLOG ITEM 3: Anomaly Type Prediction

**Status:** NICE-TO-HAVE

**What it is:**
Beyond "spike/stuck/drift" → predict root cause:
- "This spike is caused by: Ventilation failure (confidence 0.92)"
- "This stuck sensor is: Faulty humidity sensor (confidence 0.87)"

**Implementation:**
```
Train classifier on operator feedback + labels:
├─ Input: Anomaly features (spike profile, affected sensors, diurnal time)
├─ Output: Probable root cause (5-10 categories)
└─ Confidence score per cause

Uses: operator_feedback + cause labels
```

**Effort:** 1 week
**Trigger:** Collect 100+ operator labeled anomalies

---

### 🟡 BACKLOG ITEM 4: Benchmarking & SLA Scoring

**Status:** NICE-TO-HAVE

**What it is:**
Track greenhouse health over time:
- "2026 Q1: Uptime SLA 99.5% (anomaly-free)"
- "Compared to industry: Your anomaly rate 0.8% vs avg 1.2% ✓"

**Implementation:**
```
Track per greenhouse:
├─ Uptime: % time without critical anomalies
├─ MTBF: Mean Time Between Failures
├─ Anomaly density: Anomalies per 1000 sensor readings
└─ Benchmark: Compare to similar greenhouses
```

**Effort:** 3 days
**Trigger:** 3+ months historical data per customer

---

### 🟢 BACKLOG ITEM 5: Advanced Transfer Learning

**Status:** FUTURE

**What it is:**
Learn across customers to accelerate onboarding:
- Customer A has 6 months data
- Customer B is new (day 1)
- Use patterns from A to bootstrap B
- B converges in 1 week instead of 4 weeks

**Current Plan (Phase 5):**
- Basic transfer learning: Load shared model, fine-tune on new data

**Advanced Plan:**
- Meta-learning: Learn to learn faster (MAML)
- Few-shot anomaly detection: Detect with minimal examples
- Domain adaptation: Handle different greenhouse types

**Effort:** 2 weeks
**Trigger:** 5+ customers with diverse greenhouse types

---

### 🔵 BACKLOG ITEM 6: Mobile App & Alerting

**Status:** FUTURE

**What it is:**
Push notifications for operators:
- iOS/Android app
- Real-time alerts: "🚨 Spike in greenhouse_temp"
- Dashboard: View clusters, patterns, history
- Actions: Acknowledge anomaly, log notes

**Effort:** 2 weeks
**Trigger:** Streaming detection working + 3+ live customers

---

### ⚪ BACKLOG ITEM 7: Priva Native Integration (If Partnership)

**Status:** FUTURE (Requires Priva partnership)

**What it is:**
If Priva white-labels anomaly detection:
- Embed directly in Priva UI
- Single sign-on with Priva credentials
- Real-time syncing of greenhouse configs

**Effort:** 1 week (with Priva support)
**Trigger:** Partnership agreement with Priva

---

## 12. SUMMARY: SCOPE BY RELEASE

### 🚀 MVP (Week 6 - GO LIVE)
✅ Seasonal baselines (4 patterns per greenhouse)
✅ 5 univariate detectors (spike, stuck, drift, precision, polling)
✅ Operator feedback loop with auto-retrain
✅ Adaptive thresholds (learn from data)
✅ SHAP explanations (why flagged)
✅ Anomaly clustering (group related)
✅ Transfer learning (multi-customer sharing)
✅ All 3 modes: Train, Detect, Analyze

### 📦 v1.1 (Month 2)
❌ Streaming detection (BACKLOG - waiting for live data)
✅ Predictive warnings (optional)
✅ Anomaly type classification (if labeled data exists)

### 📱 v1.2+ (Month 3+)
✅ Mobile app + push alerts
✅ Advanced transfer learning
✅ Priva white-label integration (if partnership)

---
