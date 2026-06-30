# PHASE 4: PASS 2 DETECTION PIPELINE
## Timeline: 1 week | Priority: Complete anomaly detection with all features

---

## 🎯 OBJECTIVE

Complete anomaly detection pipeline (Pass 2):

**Input:** CSV file + seasonal pattern(s)
↓
**Process:** Row-by-row anomaly detection with seasonal thresholds
↓
**Output:** Anomalies CSV with:
- Timestamp, sensor, fault type
- Confidence scores
- Explanations (SHAP)
- Cluster assignments
- Root cause inference
- Recommendations

**Three Operating Modes:**
1. `POST /api/anomaly/detect` - Detect only (use stored pattern)
2. `POST /api/anomaly/analyze` - Train + Detect all-in-one
3. `GET /api/anomaly/patterns/{id}` - Retrieve saved patterns

---

## 📋 DELIVERABLES

- [ ] `engines/pass2_detect.py` - Row-by-row detection
- [ ] `engines/pass2_detect_seasonal.py` - Season-aware detection
- [ ] `engines/pass2_result_aggregator.py` - Compile final output
- [ ] `api/routes/patterns.py` - Pattern management API
- [ ] Updated: `api/routes/anomaly.py` - Detect + Analyze endpoints
- [ ] Updated: `storage/result_store.py` - Export with clusters + explanations
- [ ] `tests/integration/test_detect_api.py`
- [ ] `tests/integration/test_analyze_api.py`
- [ ] Result CSV schema documentation

---

## 🔧 IMPLEMENTATION

### Step 1: `engines/pass2_detect_seasonal.py`

```python
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple
from datetime import datetime
import logging

from detectors.spike import SpikeDetector
from detectors.stuck import StuckDetector
from detectors.drift import DriftDetector
from detectors.precision import PrecisionDetector
from detectors.polling import PollingDetector
from detectors.multivariate import MultivariateStat, GDNWrapper
from detectors.physics_validator import PhysicsValidator
from models.explainability import SHAPExplainer, Explanation
from engines.anomaly_clustering import AnomalyClustering
from engines.voting import aggregate_detector_votes, get_consensus_level
from models.anomaly import Anomaly

logger = logging.getLogger(__name__)

class Pass2DetectorSeasonal:
    """Row-by-row anomaly detection with seasonal pattern awareness"""
    
    def __init__(self, df: pd.DataFrame, patterns_seasonal: Dict[str, Dict], 
                 timestamp_column: str = None):
        """
        Args:
            df: Input dataframe
            patterns_seasonal: {'spring': pattern, 'summer': pattern, ...}
            timestamp_column: Name of timestamp column
        """
        
        self.df = df
        self.patterns_seasonal = patterns_seasonal
        self.timestamp_column = timestamp_column
        
        # Initialize detectors
        self.spike_detector = SpikeDetector()
        self.stuck_detector = StuckDetector()
        self.drift_detector = DriftDetector()
        self.precision_detector = PrecisionDetector()
        self.polling_detector = PollingDetector()
        
        # Physics validator
        self.physics_validator = PhysicsValidator()
        
        # Season map
        self.season_map = {
            1: 'winter', 2: 'winter',
            3: 'spring', 4: 'spring', 5: 'spring',
            6: 'summer', 7: 'summer', 8: 'summer',
            9: 'autumn', 10: 'autumn', 11: 'autumn',
            12: 'winter'
        }
    
    def detect(self) -> Tuple[List[Anomaly], Dict]:
        """
        Run Pass 2 detection on all rows
        
        Returns:
            (anomalies_list, statistics)
        """
        
        anomalies = []
        stats = {
            'total_rows': len(self.df),
            'rows_processed': 0,
            'anomalies_found': 0,
            'by_season': {},
            'by_fault_type': {}
        }
        
        # Extract month if timestamp available
        if self.timestamp_column:
            self.df['month'] = pd.to_datetime(self.df[self.timestamp_column]).dt.month
        else:
            # Fallback: cycle through months
            self.df['month'] = (np.arange(len(self.df)) % 12) + 1
        
        # Iterate rows
        for idx, row in self.df.iterrows():
            
            # Get timestamp
            timestamp = row.get(self.timestamp_column, f"row_{idx}")
            if self.timestamp_column:
                timestamp = pd.to_datetime(timestamp).isoformat()
            
            # Get season
            month = row.get('month', 1)
            season = self.season_map.get(int(month), 'winter')
            
            # Get appropriate pattern
            pattern = self.patterns_seasonal.get(season)
            if not pattern:
                continue
            
            # Run detectors
            detectors_triggered = []
            
            for sensor in pattern.get('sensors', []):
                if sensor not in row.index:
                    continue
                
                value = row[sensor]
                
                # Detector 1: Spike
                result = self.spike_detector.check(value, pattern, sensor)
                if result.triggered:
                    detectors_triggered.append({
                        'type': 'spike',
                        'sensor': sensor,
                        'confidence': result.confidence,
                        'details': result.details
                    })
                
                # Detector 2: Stuck
                result = self.stuck_detector.check(value, pattern, sensor)
                if result.triggered:
                    detectors_triggered.append({
                        'type': 'stuck',
                        'sensor': sensor,
                        'confidence': result.confidence,
                        'details': result.details
                    })
                
                # Detector 3: Drift
                result = self.drift_detector.check(value, pattern, sensor)
                if result.triggered:
                    detectors_triggered.append({
                        'type': 'drift',
                        'sensor': sensor,
                        'confidence': result.confidence,
                        'details': result.details
                    })
                
                # Detector 4: Precision
                result = self.precision_detector.check(value, pattern, sensor)
                if result.triggered:
                    detectors_triggered.append({
                        'type': 'precision',
                        'sensor': sensor,
                        'confidence': result.confidence,
                        'details': result.details
                    })
                
                # Detector 5: Polling
                result = self.polling_detector.check(value, pattern, sensor)
                if result.triggered:
                    detectors_triggered.append({
                        'type': 'polling',
                        'sensor': sensor,
                        'confidence': result.confidence,
                        'details': result.details
                    })
            
            # Multivariate checks
            triggered, confidence, details = MultivariateStat.check_mahalanobis(
                dict(row), pattern
            )
            if triggered:
                detectors_triggered.append({
                    'type': 'multivariate_mahalanobis',
                    'sensor': 'multivariate',
                    'confidence': confidence,
                    'details': details
                })
            
            # Process if any detector triggered
            if detectors_triggered:
                
                # Aggregate votes
                avg_confidence = aggregate_detector_votes(detectors_triggered)
                agreement = len(detectors_triggered)
                
                # Physics validation
                physics_valid = self.physics_validator.validate(
                    dict(row), timestamp, pattern
                )
                
                if not physics_valid:
                    avg_confidence *= 0.6  # Reduce if invalid
                
                # Determine fault type
                from collections import Counter
                fault_type = Counter(
                    [d['type'] for d in detectors_triggered]
                ).most_common(1)[0][0]
                
                # Get primary sensor
                primary_sensor = Counter(
                    [d['sensor'] for d in detectors_triggered]
                ).most_common(1)[0][0]
                
                # Only include if confidence > 0.3
                if avg_confidence > 0.3:
                    
                    # Generate explanation (SHAP)
                    explainer = SHAPExplainer(pattern)
                    explanation = explainer.explain(dict(row), detectors_triggered)
                    
                    # Create anomaly
                    anomaly = Anomaly(
                        timestamp=timestamp,
                        sensor=primary_sensor,
                        fault_type=fault_type,
                        confidence=avg_confidence,
                        physics_valid=physics_valid,
                        detector_agreement=agreement,
                        season=season,
                        details=detectors_triggered,
                        explanation=explanation.to_dict() if explanation else None
                    )
                    
                    anomalies.append(anomaly)
                    
                    # Update stats
                    stats['anomalies_found'] += 1
                    stats['by_fault_type'][fault_type] = stats['by_fault_type'].get(
                        fault_type, 0
                    ) + 1
            
            stats['rows_processed'] += 1
            if season not in stats['by_season']:
                stats['by_season'][season] = {'processed': 0, 'anomalies': 0}
            stats['by_season'][season]['processed'] += 1
        
        # Cluster anomalies
        clusters = AnomalyClustering.cluster([a.to_dict() for a in anomalies])
        
        # Assign cluster info back to anomalies
        for anomaly in anomalies:
            for cluster in clusters:
                for cluster_anomaly in cluster.anomalies:
                    if (cluster_anomaly['timestamp'] == anomaly.timestamp and
                        cluster_anomaly['sensor'] == anomaly.sensor):
                        anomaly.cluster_id = cluster.cluster_id
                        anomaly.cluster_severity = cluster.severity
                        break
        
        return anomalies, stats
```

### Step 2: `engines/pass2_result_aggregator.py`

```python
import pandas as pd
from typing import List, Dict
from models.anomaly import Anomaly

class ResultAggregator:
    """Compile detection results into exportable format"""
    
    @staticmethod
    def to_csv(anomalies: List[Anomaly], output_path: str) -> str:
        """
        Export anomalies to CSV
        """
        
        records = []
        
        for anomaly in anomalies:
            
            # Extract explanation
            explanation = anomaly.explanation or {}
            
            record = {
                'timestamp': anomaly.timestamp,
                'sensor': anomaly.sensor,
                'fault_type': anomaly.fault_type,
                'confidence': round(anomaly.confidence, 3),
                'physics_valid': anomaly.physics_valid,
                'detector_agreement': anomaly.detector_agreement,
                'season': anomaly.season,
                'cluster_id': anomaly.cluster_id or 'unclustered',
                'cluster_severity': anomaly.cluster_severity or 'N/A',
                'primary_cause': explanation.get('primary_cause', 'unknown'),
                'recommendation': explanation.get('recommendation', ''),
                'contributing_sensors': ', '.join([
                    f[0] for f in explanation.get('contributing_features', [])[:3]
                ])
            }
            
            records.append(record)
        
        # Create dataframe
        df = pd.DataFrame(records)
        
        # Save to CSV
        df.to_csv(output_path, index=False)
        
        return output_path
    
    @staticmethod
    def to_dict(anomalies: List[Anomaly], include_details: bool = False) -> Dict:
        """
        Export anomalies as dictionary
        """
        
        summary = {
            'total_anomalies': len(anomalies),
            'anomalies': []
        }
        
        # Group by fault type
        by_fault_type = {}
        by_severity = {}
        
        for anomaly in anomalies:
            
            fault_type = anomaly.fault_type
            severity = anomaly.cluster_severity or 'N/A'
            
            by_fault_type[fault_type] = by_fault_type.get(fault_type, 0) + 1
            by_severity[severity] = by_severity.get(severity, 0) + 1
            
            record = {
                'timestamp': anomaly.timestamp,
                'sensor': anomaly.sensor,
                'fault_type': fault_type,
                'confidence': round(anomaly.confidence, 3),
                'cluster_id': anomaly.cluster_id,
                'severity': severity
            }
            
            if include_details:
                record['explanation'] = anomaly.explanation
                record['details'] = anomaly.details
            
            summary['anomalies'].append(record)
        
        summary['by_fault_type'] = by_fault_type
        summary['by_severity'] = by_severity
        
        return summary
```

### Step 3: `api/routes/patterns.py`

```python
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from database.session import get_db
from database.models import PatternProfile
from storage.model_store import ModelStore
from typing import List, Dict

router = APIRouter(prefix="/api/anomaly/patterns", tags=["patterns"])

@router.get("/list")
async def list_patterns(customer_id: str, db: Session = Depends(get_db)) -> Dict:
    """List all patterns for customer"""
    
    # Query database
    patterns = db.query(PatternProfile).filter(
        PatternProfile.customer_id == customer_id
    ).all()
    
    return {
        'customer_id': customer_id,
        'total_patterns': len(patterns),
        'patterns': [
            {
                'id': p.id,
                'season': p.season,
                'version': p.version,
                'created_at': p.created_at.isoformat(),
                'rows_analyzed': p.rows_analyzed,
                'sensors': p.sensors
            }
            for p in patterns
        ]
    }

@router.get("/{pattern_id}")
async def get_pattern(pattern_id: str, db: Session = Depends(get_db)) -> Dict:
    """Retrieve pattern by ID"""
    
    pattern = db.query(PatternProfile).filter(
        PatternProfile.id == pattern_id
    ).first()
    
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    
    # Load from file
    pattern_data = ModelStore.load(pattern.file_path)
    
    return {
        'id': pattern.id,
        'season': pattern.season,
        'created_at': pattern.created_at.isoformat(),
        'data': pattern_data
    }

@router.delete("/reset")
async def reset_patterns(customer_id: str, confirm: bool = False, 
                        db: Session = Depends(get_db)) -> Dict:
    """Hard reset: delete all patterns for customer"""
    
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirm reset by setting confirm=true"
        )
    
    # Delete from database
    deleted = db.query(PatternProfile).filter(
        PatternProfile.customer_id == customer_id
    ).delete()
    
    db.commit()
    
    return {
        'status': 'reset_complete',
        'customer_id': customer_id,
        'patterns_deleted': deleted
    }
```

### Step 4: Updated `api/routes/anomaly.py` - Add Detect + Analyze

```python
# ADD to existing anomaly.py:

@router.post("/detect")
async def detect(
    file: UploadFile = File(...),
    customer_id: str = None,
    pattern_id: str = None,
    db: Session = Depends(get_db)
) -> Dict:
    """
    Upload CSV + Use stored pattern → Detect anomalies
    """
    
    if not customer_id or not pattern_id:
        raise HTTPException(status_code=400, 
                          detail="customer_id and pattern_id required")
    
    # Create job
    job_id = create_job(db, customer_id, "detect", file.filename)
    
    try:
        # Save file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            contents = await file.read()
            tmp.write(contents)
            tmp_path = tmp.name
        
        # Parse CSV
        parser = CSVParser(tmp_path)
        df, numeric_cols, timestamp_col = parser.parse()
        
        # Load pattern
        pattern_record = db.query(PatternProfile).filter(
            PatternProfile.id == pattern_id
        ).first()
        
        if not pattern_record:
            raise ValueError(f"Pattern {pattern_id} not found")
        
        # Load all seasonal patterns
        patterns_by_season = {}
        for season in ['spring', 'summer', 'autumn', 'winter']:
            p = db.query(PatternProfile).filter(
                PatternProfile.customer_id == customer_id,
                PatternProfile.season == season
            ).order_by(PatternProfile.created_at.desc()).first()
            
            if p:
                patterns_by_season[season] = ModelStore.load(p.file_path)
        
        if not patterns_by_season:
            raise ValueError("No seasonal patterns found")
        
        # Run Pass 2 detection
        detector = Pass2DetectorSeasonal(df, patterns_by_season, timestamp_col)
        anomalies, stats = detector.detect()
        
        # Export results
        output_path = str(RESULTS_DIR / f"anomalies_{customer_id}_{uuid.uuid4().hex[:8]}.csv")
        ResultAggregator.to_csv(anomalies, output_path)
        
        # Update job
        update_job(db, job_id, "complete", 
                  rows_processed=len(df),
                  output_file=output_path)
        
        return {
            'status': 'detection_complete',
            'job_id': job_id,
            'customer_id': customer_id,
            'total_rows': stats['total_rows'],
            'anomalies_found': stats['anomalies_found'],
            'by_fault_type': stats['by_fault_type'],
            'by_severity': {
                'HIGH': sum(1 for a in anomalies if a.cluster_severity == 'HIGH'),
                'MEDIUM': sum(1 for a in anomalies if a.cluster_severity == 'MEDIUM'),
                'LOW': sum(1 for a in anomalies if a.cluster_severity == 'LOW')
            },
            'output_file': output_path
        }
    
    except Exception as e:
        logger.error(f"Detection failed: {str(e)}")
        update_job(db, job_id, "failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/analyze")
async def analyze(
    file: UploadFile = File(...),
    customer_id: str = None,
    db: Session = Depends(get_db)
) -> Dict:
    """
    Upload CSV → Train seasonal patterns + Detect anomalies all-in-one
    """
    
    if not customer_id:
        raise HTTPException(status_code=400, detail="customer_id required")
    
    # Create job
    job_id = create_job(db, customer_id, "analyze", file.filename)
    
    try:
        # Save file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            contents = await file.read()
            tmp.write(contents)
            tmp_path = tmp.name
        
        # Parse CSV
        parser = CSVParser(tmp_path)
        df, numeric_cols, timestamp_col = parser.parse()
        
        # PASS 1: Learn seasonal patterns
        splitter = SeasonalSplitter(df, timestamp_col)
        seasonal_data = splitter.split()
        
        patterns_result = {}
        patterns_by_season = {}
        
        for season_name, season_df in seasonal_data.items():
            
            learner = PatternLearner(season_df, numeric_cols, timestamp_col)
            pattern = learner.learn()
            
            # Save pattern
            file_path = ModelStore.save(customer_id, season_name, pattern)
            pattern_id = save_pattern(db, customer_id, season_name, pattern, file_path)
            
            patterns_by_season[season_name] = pattern
            patterns_result[season_name] = pattern_id
        
        # PASS 2: Detect anomalies
        detector = Pass2DetectorSeasonal(df, patterns_by_season, timestamp_col)
        anomalies, stats = detector.detect()
        
        # Export results
        output_path = str(RESULTS_DIR / f"anomalies_{customer_id}_{uuid.uuid4().hex[:8]}.csv")
        ResultAggregator.to_csv(anomalies, output_path)
        
        # Update job
        update_job(db, job_id, "complete",
                  rows_processed=len(df),
                  output_file=output_path)
        
        return {
            'status': 'analysis_complete',
            'job_id': job_id,
            'customer_id': customer_id,
            'patterns': patterns_result,
            'total_rows': stats['total_rows'],
            'anomalies_found': stats['anomalies_found'],
            'by_fault_type': stats['by_fault_type'],
            'output_file': output_path
        }
    
    except Exception as e:
        logger.error(f"Analysis failed: {str(e)}")
        update_job(db, job_id, "failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
```

### Step 5: `tests/integration/test_detect_api.py`

```python
import pytest
import tempfile
import pandas as pd
import numpy as np
from fastapi.testclient import TestClient
from api.main import app
from database.session import SessionLocal

client = TestClient(app)

@pytest.fixture
def sample_csv_with_anomaly():
    """Create CSV with artificial anomaly"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        timestamps = pd.date_range('2026-06-01', periods=5000, freq='5min')
        
        # Normal data
        temperature = 22 + np.sin(np.arange(5000) * 2*np.pi/288) * 3 + np.random.randn(5000) * 0.3
        
        # Inject anomaly: spike at row 2500
        temperature[2500] = 45.0
        
        df = pd.DataFrame({
            'timestamp': timestamps,
            'temperature': temperature,
            'humidity': 65 + np.cos(np.arange(5000) * 2*np.pi/288) * 8 + np.random.randn(5000) * 0.5,
            'light': 200 * (1 + np.sin(np.arange(5000) * 2*np.pi/288)) + np.random.randn(5000) * 10,
            'co2': 400 + np.random.randn(5000) * 10
        })
        
        df.to_csv(f.name, index=False)
        return f.name

def test_detect_api_finds_anomaly(sample_csv_with_anomaly):
    """Should detect the injected spike anomaly"""
    
    db = SessionLocal()
    customer_id = "test_detect_customer"
    
    # First: train to get pattern
    with open(sample_csv_with_anomaly, 'rb') as f:
        train_response = client.post(
            "/api/anomaly/train",
            files={"file": f},
            params={"customer_id": customer_id}
        )
    
    assert train_response.status_code == 200
    train_data = train_response.json()
    pattern_id = list(train_data['patterns'].values())[0]['pattern_id']
    
    # Second: detect using pattern
    with open(sample_csv_with_anomaly, 'rb') as f:
        detect_response = client.post(
            "/api/anomaly/detect",
            files={"file": f},
            params={
                "customer_id": customer_id,
                "pattern_id": pattern_id
            }
        )
    
    assert detect_response.status_code == 200
    detect_data = detect_response.json()
    
    # Should find the spike
    assert detect_data['anomalies_found'] > 0
    assert 'spike' in detect_data['by_fault_type']
```

### Step 6: `tests/integration/test_analyze_api.py`

```python
import pytest
import tempfile
import pandas as pd
import numpy as np
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)

def test_analyze_api_train_and_detect():
    """Analyze should train + detect in one call"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        timestamps = pd.date_range('2026-01-01', periods=10000, freq='5min')
        
        df = pd.DataFrame({
            'timestamp': timestamps,
            'temperature': 22 + np.sin(np.arange(10000) * 2*np.pi/288) * 4 + np.random.randn(10000) * 0.5,
            'humidity': 65 + np.random.randn(10000) * 3,
            'light': 200 * (1 + np.sin(np.arange(10000) * 2*np.pi/288)) + np.random.randn(10000) * 20,
            'co2': 400 + np.random.randn(10000) * 15
        })
        
        df.to_csv(f.name, index=False)
        csv_path = f.name
    
    with open(csv_path, 'rb') as f:
        response = client.post(
            "/api/anomaly/analyze",
            files={"file": f},
            params={"customer_id": "test_analyze"}
        )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should have patterns
    assert 'patterns' in data
    assert len(data['patterns']) == 4  # 4 seasons
    
    # Should have anomalies
    assert 'anomalies_found' in data
```

---

## ✅ SUCCESS CRITERIA

- [ ] Pass 2 detector runs all 5 detectors per row
- [ ] Seasonal patterns apply correctly (spring ≠ summer thresholds)
- [ ] Anomalies have explanations (SHAP)
- [ ] Anomalies grouped into clusters
- [ ] CSV export includes all fields
- [ ] `/detect` endpoint works with stored patterns
- [ ] `/analyze` endpoint trains + detects
- [ ] `/patterns/list` returns customer patterns
- [ ] `/patterns/{id}` retrieves pattern data
- [ ] `/patterns/reset` hard-resets patterns
- [ ] 50+/50+ tests passing

---

## 🧪 TESTING COMMAND

```bash
pytest tests/integration/test_detect_api.py tests/integration/test_analyze_api.py -v
# Expected: 50+/50+ passing
```

---

## ⏭️ NEXT PHASE

After this completes → **PHASE 5: Transfer Learning & Optimization**

**auto-proceed**
