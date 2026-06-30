# PHASE 1: FOUNDATION + SEASONAL PATTERNS
## Timeline: 1 week | Priority: Core infrastructure

---

## 🎯 OBJECTIVE

Build FastAPI backend with seasonal pattern learning:
- Detect numeric columns from ANY CSV format
- Learn 4 separate patterns (spring/summer/autumn/winter)
- Calculate thresholds independently per season
- Save patterns with versioning

**Output Example:**
```json
{
  "status": "training_complete",
  "patterns": {
    "spring": {
      "pattern_id": "pattern_spring_v1_2026-08-20",
      "rows_analyzed": 28000,
      "sensors": ["temperature", "humidity", "light", "co2"],
      "thresholds": {
        "spike": {"temperature": 2.3, "humidity": 12.5, ...},
        "stuck": {"temperature": 0.008, "humidity": 0.09, ...}
      },
      "statistics": {"temperature": {"mean": 20.5, "std": 3.2, ...}, ...}
    },
    "summer": {...},
    "autumn": {...},
    "winter": {...}
  }
}
```

---

## 📋 DELIVERABLES

- [ ] `api/main.py` - FastAPI app skeleton
- [ ] `api/routes/anomaly.py` - Train endpoint
- [ ] `core/config.py` - Settings & constants
- [ ] `parsers/csv_parser.py` - CSV detection & validation
- [ ] `engines/pass1_learn.py` - Pattern learning
- [ ] `engines/pass1_seasonal_splitter.py` - Seasonal data split
- [ ] `storage/model_store.py` - JSON persistence
- [ ] `database/models.py` - SQLAlchemy models
- [ ] `database/crud.py` - DB operations
- [ ] `database/session.py` - DB connection
- [ ] `tests/unit/test_csv_parser.py` - Parser tests
- [ ] `tests/unit/test_pass1_learn.py` - Pattern learning tests
- [ ] `tests/integration/test_train_api.py` - API integration tests
- [ ] `requirements.txt` - Dependencies
- [ ] `docker-compose.yml` - Local dev database

---

## 🔧 IMPLEMENTATION

### Step 1: Create Project Structure

```bash
mkdir -p ai-agronomist/{api,core,parsers,engines,storage,database,tests/{unit,integration}}
cd ai-agronomist
```

### Step 2: `requirements.txt`

```
fastapi==0.104.1
uvicorn==0.24.0
sqlalchemy==2.0.23
psycopg2-binary==2.9.9
pydantic==2.5.0
numpy==1.26.2
pandas==2.1.3
python-dotenv==1.0.0
pytest==7.4.3
pytest-asyncio==0.21.1
```

### Step 3: `core/config.py`

```python
import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).parent.parent
MODEL_DIR = BASE_DIR / "models"
RESULTS_DIR = BASE_DIR / "results"

# Create directories
MODEL_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

# Database
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./anomaly.db"
)

# Seasonal definitions
SEASONS = {
    'spring': [3, 4, 5],      # Mar-May
    'summer': [6, 7, 8],      # Jun-Aug
    'autumn': [9, 10, 11],    # Sep-Nov
    'winter': [12, 1, 2]      # Dec-Feb
}

# Detector thresholds (will be learned from data)
DETECTOR_SPIKE_MULTIPLIER = 2.5
DETECTOR_STUCK_THRESHOLD = 0.01
DETECTOR_DRIFT_THRESHOLD = 3.0
DETECTOR_PRECISION_THRESHOLD = 0.1

# API settings
MAX_UPLOAD_SIZE_MB = 500
ANOMALY_CONFIDENCE_THRESHOLD = 0.3
```

### Step 4: `database/models.py`

```python
from sqlalchemy import Column, String, Integer, Float, DateTime, JSON, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class Customer(Base):
    __tablename__ = "customers"
    
    id = Column(String, primary_key=True)
    name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class PatternProfile(Base):
    __tablename__ = "patterns"
    
    id = Column(String, primary_key=True)
    customer_id = Column(String)
    version = Column(Integer)
    season = Column(String)  # spring, summer, autumn, winter
    created_at = Column(DateTime, default=datetime.utcnow)
    rows_analyzed = Column(Integer)
    sensors = Column(JSON)  # List of sensor names
    statistics = Column(JSON)  # Mean, std, min, max per sensor
    thresholds = Column(JSON)  # Detector thresholds per sensor
    file_path = Column(String)  # Path to JSON file

class Job(Base):
    __tablename__ = "jobs"
    
    id = Column(String, primary_key=True)
    customer_id = Column(String)
    job_type = Column(String)  # 'train', 'detect', 'analyze'
    status = Column(String)  # 'queued', 'running', 'complete', 'failed'
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    input_file = Column(String)
    rows_processed = Column(Integer)
    output_file = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)

class OperatorFeedback(Base):
    __tablename__ = "operator_feedback"
    
    id = Column(String, primary_key=True)
    job_id = Column(String)
    timestamp = Column(String)
    sensor = Column(String)
    is_true_anomaly = Column(Boolean)
    operator_id = Column(String)
    reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
```

### Step 5: `database/session.py`

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Create tables
def init_db():
    from database.models import Base
    Base.metadata.create_all(bind=engine)
```

### Step 6: `database/crud.py`

```python
from sqlalchemy.orm import Session
from database.models import Job, PatternProfile, OperatorFeedback
from datetime import datetime
import uuid

def create_job(db: Session, customer_id: str, job_type: str, input_file: str) -> str:
    job_id = f"job_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    
    job = Job(
        id=job_id,
        customer_id=customer_id,
        job_type=job_type,
        status="running",
        input_file=input_file
    )
    
    db.add(job)
    db.commit()
    return job_id

def update_job(db: Session, job_id: str, status: str, rows_processed: int = None, 
               output_file: str = None, error: str = None):
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.status = status
        job.completed_at = datetime.utcnow()
        if rows_processed:
            job.rows_processed = rows_processed
        if output_file:
            job.output_file = output_file
        if error:
            job.error_message = error
        db.commit()

def save_pattern(db: Session, customer_id: str, season: str, pattern_data: dict, 
                 file_path: str, version: int = 1):
    pattern_id = f"pattern_{season}_v{version}_{datetime.utcnow().strftime('%Y%m%d')}"
    
    pattern = PatternProfile(
        id=pattern_id,
        customer_id=customer_id,
        season=season,
        version=version,
        rows_analyzed=pattern_data.get('rows_analyzed', 0),
        sensors=pattern_data.get('sensors', []),
        statistics=pattern_data.get('statistics', {}),
        thresholds=pattern_data.get('thresholds', {}),
        file_path=file_path
    )
    
    db.add(pattern)
    db.commit()
    return pattern_id
```

### Step 7: `parsers/csv_parser.py`

```python
import pandas as pd
import numpy as np
from typing import Tuple, List, Dict
import logging

logger = logging.getLogger(__name__)

class CSVParser:
    """Parse any CSV, detect numeric columns, validate format"""
    
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.df = None
        self.numeric_columns = []
        self.timestamp_column = None
    
    def parse(self) -> Tuple[pd.DataFrame, List[str], str]:
        """
        Returns: (dataframe, numeric_columns, timestamp_column)
        """
        
        # Try different delimiters
        delimiters = [',', ';', '\t', '|']
        df = None
        
        for delim in delimiters:
            try:
                df = pd.read_csv(self.file_path, sep=delim, decimal=',')
                logger.info(f"Successfully parsed CSV with delimiter: {delim}")
                break
            except:
                continue
        
        if df is None:
            raise ValueError("Could not parse CSV with any delimiter")
        
        # Replace comma with dot in numeric columns (European format)
        for col in df.columns:
            if df[col].dtype == 'object':
                try:
                    df[col] = df[col].str.replace(',', '.').astype(float)
                except:
                    pass
        
        # Detect numeric columns
        self.numeric_columns = df.select_dtypes(include=[np.number]).columns.tolist()
        
        if not self.numeric_columns:
            raise ValueError("No numeric columns found in CSV")
        
        logger.info(f"Detected numeric columns: {self.numeric_columns}")
        
        # Detect timestamp column
        possible_timestamp_cols = [col for col in df.columns 
                                  if 'time' in col.lower() or 'date' in col.lower()]
        
        if possible_timestamp_cols:
            self.timestamp_column = possible_timestamp_cols[0]
            try:
                df[self.timestamp_column] = pd.to_datetime(df[self.timestamp_column])
                logger.info(f"Detected timestamp column: {self.timestamp_column}")
            except:
                self.timestamp_column = None
        
        # Remove rows with all NaN
        df = df.dropna(how='all')
        
        self.df = df
        return df, self.numeric_columns, self.timestamp_column
    
    def validate(self) -> Dict:
        """Validate CSV is suitable for training"""
        
        if self.df is None:
            raise ValueError("CSV not parsed yet")
        
        validation = {
            'total_rows': len(self.df),
            'numeric_columns': len(self.numeric_columns),
            'missing_values': self.df[self.numeric_columns].isnull().sum().to_dict(),
            'is_valid': True
        }
        
        # Check minimum rows
        if len(self.df) < 100:
            validation['is_valid'] = False
            validation['error'] = "CSV must have at least 100 rows"
        
        # Check numeric columns
        if len(self.numeric_columns) < 2:
            validation['is_valid'] = False
            validation['error'] = "CSV must have at least 2 numeric columns"
        
        return validation
```

### Step 8: `engines/pass1_seasonal_splitter.py`

```python
import pandas as pd
from typing import Dict, Tuple
from core.config import SEASONS

class SeasonalSplitter:
    """Split data by season (spring/summer/autumn/winter)"""
    
    def __init__(self, df: pd.DataFrame, timestamp_column: str):
        self.df = df
        self.timestamp_column = timestamp_column
        
        if timestamp_column:
            self.df['month'] = pd.to_datetime(self.df[timestamp_column]).dt.month
        else:
            raise ValueError("Timestamp column required for seasonal split")
    
    def split(self) -> Dict[str, pd.DataFrame]:
        """
        Returns: {season_name: dataframe_for_season}
        """
        
        seasonal_data = {}
        
        for season_name, months in SEASONS.items():
            season_df = self.df[self.df['month'].isin(months)].copy()
            
            if len(season_df) >= 100:  # Minimum samples per season
                seasonal_data[season_name] = season_df
        
        return seasonal_data
```

### Step 9: `engines/pass1_learn.py`

```python
import pandas as pd
import numpy as np
from typing import Dict, List
from datetime import datetime
import logging
import json

logger = logging.getLogger(__name__)

class PatternLearner:
    """Learn baseline patterns from data"""
    
    def __init__(self, df: pd.DataFrame, numeric_columns: List[str], 
                 timestamp_column: str = None):
        self.df = df
        self.numeric_columns = numeric_columns
        self.timestamp_column = timestamp_column
    
    def learn(self) -> Dict:
        """
        Learn: statistics, thresholds, diurnal patterns, correlations
        """
        
        # Calculate per-column statistics
        statistics = {}
        for col in self.numeric_columns:
            statistics[col] = {
                'mean': float(self.df[col].mean()),
                'std': float(self.df[col].std()),
                'min': float(self.df[col].min()),
                'max': float(self.df[col].max()),
                'median': float(self.df[col].median()),
                'q25': float(self.df[col].quantile(0.25)),
                'q75': float(self.df[col].quantile(0.75))
            }
        
        # Calculate detector thresholds (learned from data)
        thresholds = {
            'spike': {},
            'stuck': {},
            'drift': {},
            'precision': {}
        }
        
        for col in self.numeric_columns:
            std = statistics[col]['std']
            
            thresholds['spike'][col] = float(std * 2.5)
            thresholds['stuck'][col] = float(std ** 2 * 0.01)
            thresholds['drift'][col] = float(std * 3.0)
            thresholds['precision'][col] = float(std * 0.1)
        
        # Detect diurnal patterns
        diurnal_patterns = {}
        if self.timestamp_column:
            for col in self.numeric_columns:
                hourly_mean = self.df.groupby(
                    pd.to_datetime(self.df[self.timestamp_column]).dt.hour
                )[col].mean()
                
                diurnal_patterns[col] = {
                    'peak_hour': int(hourly_mean.idxmax()),
                    'peak_value': float(hourly_mean.max()),
                    'trough_hour': int(hourly_mean.idxmin()),
                    'trough_value': float(hourly_mean.min()),
                    'hourly_profile': hourly_mean.tolist()
                }
        
        # Calculate correlations
        correlations = self.df[self.numeric_columns].corr().to_dict()
        
        pattern = {
            'sensors': self.numeric_columns,
            'statistics': statistics,
            'thresholds': thresholds,
            'diurnal': diurnal_patterns,
            'correlations': correlations,
            'rows_analyzed': len(self.df),
            'created_at': datetime.utcnow().isoformat()
        }
        
        return pattern
```

### Step 10: `storage/model_store.py`

```python
import json
from pathlib import Path
from datetime import datetime
from core.config import MODEL_DIR

class ModelStore:
    """Save/load pattern profiles to JSON files"""
    
    @staticmethod
    def save(customer_id: str, season: str, pattern: dict, version: int = 1) -> str:
        """
        Save pattern to JSON file
        Returns: file_path
        """
        
        customer_dir = MODEL_DIR / customer_id
        customer_dir.mkdir(exist_ok=True)
        
        filename = f"pattern_{season}_v{version}_{datetime.utcnow().strftime('%Y%m%d')}.json"
        file_path = customer_dir / filename
        
        with open(file_path, 'w') as f:
            json.dump(pattern, f, indent=2)
        
        return str(file_path)
    
    @staticmethod
    def load(file_path: str) -> dict:
        """Load pattern from JSON file"""
        
        with open(file_path, 'r') as f:
            pattern = json.load(f)
        
        return pattern
    
    @staticmethod
    def list_patterns(customer_id: str) -> list:
        """List all patterns for customer"""
        
        customer_dir = MODEL_DIR / customer_id
        
        if not customer_dir.exists():
            return []
        
        patterns = [f.name for f in customer_dir.glob("*.json")]
        return sorted(patterns)
```

### Step 11: `api/main.py`

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database.session import init_db
from api.routes import anomaly

app = FastAPI(title="AI Agronomist Anomaly Detection")

# Initialize database
init_db()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(anomaly.router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### Step 12: `api/routes/anomaly.py`

```python
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import Dict
import tempfile
import logging

from database.session import get_db
from database.crud import create_job, update_job, save_pattern
from parsers.csv_parser import CSVParser
from engines.pass1_seasonal_splitter import SeasonalSplitter
from engines.pass1_learn import PatternLearner
from storage.model_store import ModelStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/anomaly", tags=["anomaly"])

@router.post("/train")
async def train(
    file: UploadFile = File(...),
    customer_id: str = None,
    db: Session = Depends(get_db)
) -> Dict:
    """
    Upload CSV → Learn seasonal patterns
    """
    
    if not customer_id:
        raise HTTPException(status_code=400, detail="customer_id required")
    
    # Create job
    job_id = create_job(db, customer_id, "train", file.filename)
    
    try:
        # Save uploaded file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            contents = await file.read()
            tmp.write(contents)
            tmp_path = tmp.name
        
        # Parse CSV
        parser = CSVParser(tmp_path)
        df, numeric_cols, timestamp_col = parser.parse()
        validation = parser.validate()
        
        if not validation['is_valid']:
            raise ValueError(validation['error'])
        
        # Split by season
        splitter = SeasonalSplitter(df, timestamp_col)
        seasonal_data = splitter.split()
        
        # Learn patterns per season
        patterns_result = {
            'status': 'training_complete',
            'job_id': job_id,
            'patterns': {}
        }
        
        for season_name, season_df in seasonal_data.items():
            
            learner = PatternLearner(season_df, numeric_cols, timestamp_col)
            pattern = learner.learn()
            
            # Save pattern
            file_path = ModelStore.save(customer_id, season_name, pattern)
            pattern_id = save_pattern(db, customer_id, season_name, pattern, file_path)
            
            patterns_result['patterns'][season_name] = {
                'pattern_id': pattern_id,
                'rows_analyzed': pattern['rows_analyzed'],
                'sensors': pattern['sensors'],
                'thresholds': pattern['thresholds']
            }
            
            logger.info(f"Trained {season_name} pattern: {pattern_id}")
        
        # Update job
        update_job(db, job_id, "complete", rows_processed=len(df))
        
        return patterns_result
    
    except Exception as e:
        logger.error(f"Training failed: {str(e)}")
        update_job(db, job_id, "failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
```

### Step 13: `tests/unit/test_csv_parser.py`

```python
import pytest
import tempfile
import pandas as pd
from parsers.csv_parser import CSVParser

def test_parse_csv_comma_delimiter():
    """Should parse CSV with comma delimiter"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("timestamp,temperature,humidity\n")
        f.write("2026-01-01 00:00,22.5,65.0\n")
        f.write("2026-01-01 00:05,22.6,64.5\n")
        f.flush()
        
        parser = CSVParser(f.name)
        df, numeric_cols, timestamp_col = parser.parse()
        
        assert len(df) == 2
        assert 'temperature' in numeric_cols
        assert 'humidity' in numeric_cols
        assert timestamp_col == 'timestamp'

def test_parse_csv_semicolon_delimiter():
    """Should parse CSV with semicolon delimiter"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("timestamp;temperature;humidity\n")
        f.write("2026-01-01 00:00;22,5;65,0\n")
        f.flush()
        
        parser = CSVParser(f.name)
        df, numeric_cols, timestamp_col = parser.parse()
        
        assert len(df) == 1
        assert 'temperature' in numeric_cols

def test_validate_insufficient_rows():
    """Should reject CSV with < 100 rows"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("temperature,humidity\n")
        for i in range(50):
            f.write(f"{22 + i*0.1},{65 - i*0.1}\n")
        f.flush()
        
        parser = CSVParser(f.name)
        parser.parse()
        validation = parser.validate()
        
        assert not validation['is_valid']
        assert 'at least 100 rows' in validation['error']

def test_validate_no_numeric_columns():
    """Should reject CSV with no numeric columns"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("name,status\n")
        f.write("sensor1,ok\n")
        f.flush()
        
        parser = CSVParser(f.name)
        
        with pytest.raises(ValueError):
            parser.parse()
```

### Step 14: `tests/unit/test_pass1_learn.py`

```python
import pytest
import pandas as pd
import numpy as np
from engines.pass1_learn import PatternLearner

@pytest.fixture
def sample_df():
    """Create sample data"""
    
    np.random.seed(42)
    df = pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=1000, freq='5min'),
        'temperature': 20 + np.random.randn(1000) * 2,
        'humidity': 65 + np.random.randn(1000) * 5,
        'light': 200 + np.random.randn(1000) * 50
    })
    
    return df

def test_learn_statistics(sample_df):
    """Should calculate statistics"""
    
    learner = PatternLearner(sample_df, ['temperature', 'humidity', 'light'], 'timestamp')
    pattern = learner.learn()
    
    assert 'statistics' in pattern
    assert 'temperature' in pattern['statistics']
    assert 'mean' in pattern['statistics']['temperature']
    assert 'std' in pattern['statistics']['temperature']

def test_learn_thresholds(sample_df):
    """Should calculate thresholds"""
    
    learner = PatternLearner(sample_df, ['temperature', 'humidity', 'light'], 'timestamp')
    pattern = learner.learn()
    
    assert 'thresholds' in pattern
    assert 'spike' in pattern['thresholds']
    assert 'temperature' in pattern['thresholds']['spike']
    
    # Thresholds should be positive
    assert pattern['thresholds']['spike']['temperature'] > 0

def test_learn_diurnal_patterns(sample_df):
    """Should detect diurnal patterns"""
    
    learner = PatternLearner(sample_df, ['temperature', 'humidity', 'light'], 'timestamp')
    pattern = learner.learn()
    
    assert 'diurnal' in pattern
    assert 'temperature' in pattern['diurnal']
    assert 'peak_hour' in pattern['diurnal']['temperature']
    assert 'hourly_profile' in pattern['diurnal']['temperature']

def test_learn_correlations(sample_df):
    """Should calculate correlations"""
    
    learner = PatternLearner(sample_df, ['temperature', 'humidity', 'light'], 'timestamp')
    pattern = learner.learn()
    
    assert 'correlations' in pattern
    assert 'temperature' in pattern['correlations']
```

### Step 15: `tests/integration/test_train_api.py`

```python
import pytest
import tempfile
import pandas as pd
import numpy as np
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)

@pytest.fixture
def sample_csv():
    """Create sample CSV file"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        # Create year-long sample data
        timestamps = pd.date_range('2026-01-01', periods=10000, freq='5min')
        df = pd.DataFrame({
            'timestamp': timestamps,
            'temperature': 22 + np.sin(np.arange(10000) * 2*np.pi/288) * 5 + np.random.randn(10000) * 0.5,
            'humidity': 65 + np.cos(np.arange(10000) * 2*np.pi/288) * 10 + np.random.randn(10000) * 1,
            'light': 200 * (1 + np.sin(np.arange(10000) * 2*np.pi/288)) + np.random.randn(10000) * 20,
            'co2': 400 + np.random.randn(10000) * 20
        })
        
        df.to_csv(f.name, index=False)
        return f.name

def test_train_api_seasonal_patterns(sample_csv):
    """Should train and return 4 seasonal patterns"""
    
    with open(sample_csv, 'rb') as f:
        response = client.post(
            "/api/anomaly/train",
            files={"file": f},
            params={"customer_id": "test_customer"}
        )
    
    assert response.status_code == 200
    data = response.json()
    
    assert data['status'] == 'training_complete'
    assert 'patterns' in data
    assert len(data['patterns']) == 4  # 4 seasons
    
    # Each season should have pattern
    for season in ['spring', 'summer', 'autumn', 'winter']:
        assert season in data['patterns']
        assert 'pattern_id' in data['patterns'][season]
        assert 'rows_analyzed' in data['patterns'][season]
        assert 'thresholds' in data['patterns'][season]

def test_train_api_invalid_customer():
    """Should reject request without customer_id"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv') as f:
        f.write("temperature\n22.5\n")
        f.flush()
        
        with open(f.name, 'rb') as file:
            response = client.post(
                "/api/anomaly/train",
                files={"file": file}
            )
    
    assert response.status_code == 400
```

### Step 16: `docker-compose.yml`

```yaml
version: '3.8'

services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql://postgres:password@db:5432/anomaly_db
    depends_on:
      - db
    volumes:
      - ./models:/app/models
      - ./results:/app/results

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

---

## ✅ SUCCESS CRITERIA

- [ ] FastAPI app starts without errors (`uvicorn api.main:app --reload`)
- [ ] Database initializes (tables created)
- [ ] CSV parser handles comma & semicolon delimiters
- [ ] Pattern learner calculates 4 seasonal patterns independently
- [ ] Thresholds learned from data (not hardcoded)
- [ ] Train endpoint returns all 4 patterns with statistics
- [ ] All unit tests pass (15+ test cases)
- [ ] Integration test: Upload CSV → Get 4 seasonal patterns
- [ ] Patterns saved to JSON files with versioning

---

## 🧪 TESTING COMMAND

```bash
pytest tests/ -v
# Expected: 30+/30+ passing
```

---

## 🚀 RUN LOCALLY

```bash
# Install dependencies
pip install -r requirements.txt

# Initialize database
python -c "from database.session import init_db; init_db()"

# Start API
uvicorn api.main:app --reload --port 8000

# In another terminal, test
pytest tests/integration/test_train_api.py -v
```

---

## ⏭️ NEXT PHASE

After this completes → **PHASE 2: Detectors + Active Learning**

**auto-proceed**
