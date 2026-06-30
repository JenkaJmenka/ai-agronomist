# PHASE 5: TRANSFER LEARNING & OPTIMIZATION
## Timeline: 1 week | Priority: Multi-customer learning + performance

---

## 🎯 OBJECTIVE

Enable learning across customers + performance optimization:

**Transfer Learning:**
```
Customer A (6 months data)
    ↓
Extract shared knowledge (patterns, threshold distributions)
    ↓
Customer B (new, day 1)
    ↓
Bootstrap from shared knowledge
    ↓
Converges 4x faster (1 week → 2 days)
```

**Performance Optimization:**
- Pattern caching (avoid reload per request)
- Vectorized detection (process 1000s rows/sec)
- Connection pooling (DB + Redis)
- Lazy loading (patterns only when needed)

---

## 📋 DELIVERABLES

- [ ] `models/transfer_learning.py` - Cross-customer knowledge extraction
- [ ] `engines/model_aggregation.py` - Combine customer patterns
- [ ] `engines/fine_tuning.py` - Adapt shared model to new customer
- [ ] `utils/cache.py` - Pattern caching with TTL
- [ ] `api/middleware/cache_middleware.py` - Request caching
- [ ] Updated: `database/session.py` - Connection pooling
- [ ] `tests/unit/test_transfer_learning.py`
- [ ] `tests/performance/test_throughput.py`
- [ ] Performance benchmarking report

---

## 🔧 IMPLEMENTATION

### Step 1: `models/transfer_learning.py`

```python
import numpy as np
from typing import Dict, List
from collections import defaultdict
import json

class TransferLearning:
    """Extract and share knowledge across customers"""
    
    @staticmethod
    def extract_shared_knowledge(customer_patterns: Dict[str, Dict]) -> Dict:
        """
        Extract generalizable patterns across customers
        
        Args:
            customer_patterns: {customer_id: {season: pattern}}
        
        Returns:
            shared_knowledge: Aggregated statistics
        """
        
        shared = {
            'threshold_distributions': {
                'spike': {},
                'stuck': {},
                'drift': {},
                'precision': {}
            },
            'correlation_patterns': {},
            'seasonal_adjustments': {},
            'sensor_importance': {}
        }
        
        # Collect all threshold values
        all_spike_thresholds = defaultdict(list)
        all_stuck_thresholds = defaultdict(list)
        all_drift_thresholds = defaultdict(list)
        all_precision_thresholds = defaultdict(list)
        
        for customer_id, customer_data in customer_patterns.items():
            
            for season, pattern in customer_data.items():
                
                # Extract thresholds
                thresholds = pattern.get('thresholds', {})
                
                for sensor, threshold in thresholds.get('spike', {}).items():
                    all_spike_thresholds[sensor].append(threshold)
                
                for sensor, threshold in thresholds.get('stuck', {}).items():
                    all_stuck_thresholds[sensor].append(threshold)
                
                for sensor, threshold in thresholds.get('drift', {}).items():
                    all_drift_thresholds[sensor].append(threshold)
                
                for sensor, threshold in thresholds.get('precision', {}).items():
                    all_precision_thresholds[sensor].append(threshold)
        
        # Calculate aggregate statistics per sensor
        for sensor, values in all_spike_thresholds.items():
            shared['threshold_distributions']['spike'][sensor] = {
                'mean': float(np.mean(values)),
                'std': float(np.std(values)),
                'percentile_25': float(np.percentile(values, 25)),
                'percentile_75': float(np.percentile(values, 75))
            }
        
        # Repeat for other thresholds...
        for sensor, values in all_stuck_thresholds.items():
            shared['threshold_distributions']['stuck'][sensor] = {
                'mean': float(np.mean(values)),
                'std': float(np.std(values))
            }
        
        # Seasonal adjustment factors
        shared['seasonal_adjustments'] = {
            'spring': 1.0,    # Baseline
            'summer': 1.1,    # 10% higher variability
            'autumn': 0.95,   # 5% lower
            'winter': 1.05    # 5% higher
        }
        
        return shared
    
    @staticmethod
    def calculate_sensor_importance(customer_patterns: Dict) -> Dict[str, float]:
        """
        Rank sensors by frequency of anomalies across customers
        (Sensors that frequently flag anomalies are more important)
        """
        
        sensor_anomaly_count = defaultdict(int)
        total_anomalies = 0
        
        for customer_id, customer_data in customer_patterns.items():
            # Count anomalies per sensor (simulated)
            # In production: use actual anomaly history
            
            for season, pattern in customer_data.items():
                sensors = pattern.get('sensors', [])
                for sensor in sensors:
                    sensor_anomaly_count[sensor] += 1
        
        # Normalize
        total = sum(sensor_anomaly_count.values()) or 1
        importance = {
            sensor: count / total
            for sensor, count in sensor_anomaly_count.items()
        }
        
        return importance
```

### Step 2: `engines/model_aggregation.py`

```python
from typing import Dict, List
import numpy as np

class ModelAggregator:
    """Combine knowledge from multiple customers"""
    
    @staticmethod
    def aggregate_thresholds(customer_patterns: Dict[str, Dict],
                           shared_knowledge: Dict) -> Dict:
        """
        Create universal thresholds based on customer distribution
        """
        
        universal_thresholds = {
            'spike': {},
            'stuck': {},
            'drift': {},
            'precision': {}
        }
        
        # Use percentile 75 as universal (conservative)
        for detector_type in universal_thresholds.keys():
            
            for sensor, stats in shared_knowledge['threshold_distributions'].get(
                detector_type, {}
            ).items():
                
                # Use 75th percentile (slightly aggressive)
                if 'percentile_75' in stats:
                    universal_thresholds[detector_type][sensor] = stats['percentile_75']
                else:
                    universal_thresholds[detector_type][sensor] = stats.get('mean', 0)
        
        return universal_thresholds
    
    @staticmethod
    def create_aggregate_pattern(customer_patterns: Dict[str, Dict],
                                shared_knowledge: Dict) -> Dict:
        """
        Create aggregate baseline pattern for new customers
        """
        
        # Pick one customer's pattern as template
        template_pattern = list(customer_patterns.values())[0].get('summer', {})
        
        aggregate = {
            'sensors': template_pattern.get('sensors', []),
            'thresholds': ModelAggregator.aggregate_thresholds(
                customer_patterns, shared_knowledge
            ),
            'statistics': {},  # Will be refined per customer
            'diurnal': {},
            'correlations': {},
            'seasonal_adjustments': shared_knowledge['seasonal_adjustments'],
            'sensor_importance': shared_knowledge.get('sensor_importance', {})
        }
        
        return aggregate
```

### Step 3: `engines/fine_tuning.py`

```python
from typing import Dict
import numpy as np

class FineTuner:
    """Adapt universal model to specific customer"""
    
    @staticmethod
    def fine_tune(
        new_customer_pattern: Dict,
        aggregate_pattern: Dict,
        adaptation_strength: float = 0.3
    ) -> Dict:
        """
        Blend aggregate knowledge with new customer data
        
        Args:
            new_customer_pattern: Learned from 2 weeks new customer data
            aggregate_pattern: Universal pattern from all customers
            adaptation_strength: 0.3 = 70% aggregate, 30% new data
        
        Returns:
            fine_tuned_pattern
        """
        
        fine_tuned = aggregate_pattern.copy()
        
        # Blend thresholds
        for detector_type in fine_tuned['thresholds'].keys():
            
            for sensor in fine_tuned['thresholds'][detector_type].keys():
                
                aggregate_threshold = fine_tuned['thresholds'][detector_type][sensor]
                new_threshold = new_customer_pattern.get('thresholds', {}).get(
                    detector_type, {}
                ).get(sensor, aggregate_threshold)
                
                # Weighted blend
                fine_tuned['thresholds'][detector_type][sensor] = (
                    aggregate_threshold * (1 - adaptation_strength) +
                    new_threshold * adaptation_strength
                )
        
        # Blend statistics
        for sensor in fine_tuned.get('sensors', []):
            
            aggregate_stats = fine_tuned.get('statistics', {}).get(sensor, {})
            new_stats = new_customer_pattern.get('statistics', {}).get(sensor, {})
            
            if not aggregate_stats:
                fine_tuned['statistics'][sensor] = new_stats
            else:
                # Blend mean, std, etc.
                blended = {}
                for stat_key in ['mean', 'std', 'min', 'max']:
                    if stat_key in aggregate_stats and stat_key in new_stats:
                        blended[stat_key] = (
                            aggregate_stats[stat_key] * (1 - adaptation_strength) +
                            new_stats[stat_key] * adaptation_strength
                        )
                    elif stat_key in new_stats:
                        blended[stat_key] = new_stats[stat_key]
                
                fine_tuned['statistics'][sensor] = blended
        
        return fine_tuned
    
    @staticmethod
    def onboarding_timeline(days_available: int) -> Dict:
        """
        Predict onboarding time with transfer learning
        
        Without transfer: 30-45 days
        With transfer: 7-14 days
        """
        
        if days_available < 7:
            adaptation_strength = 0.1
            status = 'Using aggregate knowledge only'
        elif days_available < 14:
            adaptation_strength = 0.3
            status = 'Blending aggregate + new data'
        elif days_available < 30:
            adaptation_strength = 0.6
            status = 'Mostly new customer data'
        else:
            adaptation_strength = 1.0
            status = 'Full new customer model'
        
        return {
            'days_available': days_available,
            'adaptation_strength': adaptation_strength,
            'status': status,
            'expected_convergence_days': max(7, 30 - (days_available // 2))
        }
```

### Step 4: `utils/cache.py`

```python
from typing import Dict, Optional
from datetime import datetime, timedelta
import json
import hashlib

class PatternCache:
    """In-memory cache for patterns with TTL"""
    
    def __init__(self, ttl_minutes: int = 60):
        self.cache: Dict[str, Dict] = {}
        self.ttl = timedelta(minutes=ttl_minutes)
        self.timestamps: Dict[str, datetime] = {}
    
    def get(self, key: str) -> Optional[Dict]:
        """Get pattern from cache if not expired"""
        
        if key not in self.cache:
            return None
        
        # Check TTL
        created = self.timestamps.get(key)
        if created and datetime.utcnow() - created > self.ttl:
            del self.cache[key]
            del self.timestamps[key]
            return None
        
        return self.cache[key]
    
    def set(self, key: str, value: Dict) -> None:
        """Store pattern in cache"""
        
        self.cache[key] = value
        self.timestamps[key] = datetime.utcnow()
    
    def clear_expired(self) -> int:
        """Remove expired entries"""
        
        now = datetime.utcnow()
        expired_keys = [
            key for key, created in self.timestamps.items()
            if now - created > self.ttl
        ]
        
        for key in expired_keys:
            del self.cache[key]
            del self.timestamps[key]
        
        return len(expired_keys)
    
    def stats(self) -> Dict:
        """Cache statistics"""
        
        return {
            'total_items': len(self.cache),
            'ttl_minutes': self.ttl.total_seconds() / 60
        }

# Global cache instance
pattern_cache = PatternCache(ttl_minutes=60)
```

### Step 5: `api/middleware/cache_middleware.py`

```python
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from utils.cache import pattern_cache
import hashlib
import json

class CacheMiddleware(BaseHTTPMiddleware):
    """Cache GET requests for pattern endpoints"""
    
    async def dispatch(self, request: Request, call_next):
        
        # Only cache GET requests to patterns
        if request.method == "GET" and "/patterns/" in request.url.path:
            
            # Create cache key
            cache_key = f"{request.url.path}?{request.url.query}"
            
            # Try cache
            cached = pattern_cache.get(cache_key)
            if cached:
                from fastapi.responses import JSONResponse
                return JSONResponse(cached)
        
        # Pass through
        response = await call_next(request)
        return response
```

### Step 6: `tests/unit/test_transfer_learning.py`

```python
import pytest
from models.transfer_learning import TransferLearning
from engines.fine_tuning import FineTuner

@pytest.fixture
def multi_customer_patterns():
    """Sample patterns from multiple customers"""
    
    return {
        'customer_a': {
            'summer': {
                'sensors': ['temperature', 'humidity'],
                'thresholds': {
                    'spike': {'temperature': 5.0, 'humidity': 10.0}
                },
                'statistics': {
                    'temperature': {'mean': 25.0, 'std': 3.0}
                }
            }
        },
        'customer_b': {
            'summer': {
                'sensors': ['temperature', 'humidity'],
                'thresholds': {
                    'spike': {'temperature': 4.5, 'humidity': 9.5}
                },
                'statistics': {
                    'temperature': {'mean': 24.0, 'std': 2.8}
                }
            }
        }
    }

def test_extract_shared_knowledge(multi_customer_patterns):
    """Should extract generalizable patterns"""
    
    shared = TransferLearning.extract_shared_knowledge(multi_customer_patterns)
    
    assert 'threshold_distributions' in shared
    assert 'spike' in shared['threshold_distributions']
    assert 'temperature' in shared['threshold_distributions']['spike']

def test_sensor_importance(multi_customer_patterns):
    """Should rank sensors by frequency"""
    
    importance = TransferLearning.calculate_sensor_importance(multi_customer_patterns)
    
    assert 'temperature' in importance
    assert 'humidity' in importance
    assert sum(importance.values()) > 0

def test_fine_tuning():
    """Should blend aggregate + new customer patterns"""
    
    aggregate = {
        'thresholds': {
            'spike': {'temperature': 5.0},
            'stuck': {'temperature': 0.01}
        },
        'statistics': {
            'temperature': {'mean': 22.0, 'std': 2.0}
        }
    }
    
    new_customer = {
        'thresholds': {
            'spike': {'temperature': 6.0},
            'stuck': {'temperature': 0.02}
        },
        'statistics': {
            'temperature': {'mean': 24.0, 'std': 2.5}
        }
    }
    
    fine_tuned = FineTuner.fine_tune(
        new_customer, aggregate, adaptation_strength=0.3
    )
    
    # Should blend
    assert fine_tuned['thresholds']['spike']['temperature'] > 5.0
    assert fine_tuned['thresholds']['spike']['temperature'] < 6.0

def test_onboarding_timeline():
    """Should estimate convergence time"""
    
    result = FineTuner.onboarding_timeline(14)
    
    assert result['adaptation_strength'] > 0
    assert result['expected_convergence_days'] < 30
```

### Step 7: `tests/performance/test_throughput.py`

```python
import pytest
import time
import numpy as np
import pandas as pd
from engines.pass2_detect_seasonal import Pass2DetectorSeasonal

def test_detection_throughput():
    """Should process 1000+ rows/sec"""
    
    # Create large dataset
    n_rows = 10000
    df = pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=n_rows, freq='5min'),
        'temperature': 22 + np.random.randn(n_rows),
        'humidity': 65 + np.random.randn(n_rows),
        'light': 200 + np.random.randn(n_rows) * 20,
        'co2': 400 + np.random.randn(n_rows) * 10
    })
    
    # Create pattern
    pattern = {
        'sensors': ['temperature', 'humidity', 'light', 'co2'],
        'thresholds': {
            'spike': {s: 3.0 for s in ['temperature', 'humidity', 'light', 'co2']},
            'stuck': {s: 0.01 for s in ['temperature', 'humidity', 'light', 'co2']},
            'drift': {s: 3.0 for s in ['temperature', 'humidity', 'light', 'co2']},
            'precision': {s: 0.1 for s in ['temperature', 'humidity', 'light', 'co2']}
        },
        'statistics': {
            s: {'mean': 0, 'std': 1}
            for s in ['temperature', 'humidity', 'light', 'co2']
        }
    }
    
    patterns_seasonal = {
        'spring': pattern,
        'summer': pattern,
        'autumn': pattern,
        'winter': pattern
    }
    
    # Time detection
    start = time.time()
    detector = Pass2DetectorSeasonal(df, patterns_seasonal, 'timestamp')
    anomalies, stats = detector.detect()
    elapsed = time.time() - start
    
    throughput = n_rows / elapsed
    
    print(f"Throughput: {throughput:.0f} rows/sec")
    
    # Should be able to process 1000+ rows/sec
    assert throughput > 1000, f"Throughput {throughput} < 1000 rows/sec"
```

### Step 8: Updated `database/session.py` - Connection Pooling

```python
# ADD to existing session.py:

from sqlalchemy.pool import QueuePool

engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
```

---

## ✅ SUCCESS CRITERIA

- [ ] Transfer learning extracts knowledge across 3+ customers
- [ ] Fine-tuning blends aggregate + new patterns
- [ ] Pattern cache works with TTL expiration
- [ ] Detection throughput >1000 rows/sec
- [ ] Connection pooling reduces DB load
- [ ] Onboarding time <14 days with transfer (vs 30+ without)
- [ ] 30+/30+ tests passing
- [ ] Performance benchmark report generated

---

## 🧪 TESTING COMMAND

```bash
pytest tests/unit/test_transfer_learning.py tests/performance/test_throughput.py -v
# Expected: 30+/30+ passing
```

---

## ⏭️ NEXT PHASE

After this completes → **PHASE 6: Production Hardening**

**auto-proceed**
