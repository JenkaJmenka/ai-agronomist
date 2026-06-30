# PHASE 6: PRODUCTION HARDENING & DEPLOYMENT
## Timeline: 1 week | Priority: Security + monitoring + documentation

---

## 🎯 OBJECTIVE

Harden for production:
- Security (auth, rate limiting, input validation)
- Monitoring & logging
- Error handling
- Docker containerization
- Deployment automation
- Complete documentation

**Output:** Production-ready system ready to onboard first pilot customers

---

## 📋 DELIVERABLES

- [ ] `api/middleware/auth.py` - JWT authentication
- [ ] `api/middleware/rate_limiter.py` - Rate limiting
- [ ] `api/middleware/error_handler.py` - Global error handling
- [ ] `utils/logger.py` - Structured logging
- [ ] `utils/validators.py` - Input validation
- [ ] `docker/Dockerfile` - Container image
- [ ] `docker/docker-compose.yml` - Full stack
- [ ] `docker/.env.example` - Environment template
- [ ] `docs/API.md` - OpenAPI documentation
- [ ] `docs/DEPLOYMENT.md` - Deployment guide
- [ ] `docs/MONITORING.md` - Monitoring setup
- [ ] `scripts/deploy.sh` - Deployment script
- [ ] `config/logging.yaml` - Logging configuration
- [ ] `.gitignore` - Version control
- [ ] `tests/security/test_auth.py`
- [ ] `tests/security/test_input_validation.py`

---

## 🔧 IMPLEMENTATION

### Step 1: `api/middleware/auth.py`

```python
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthCredentials
import jwt
from datetime import datetime, timedelta
from typing import Optional

security = HTTPBearer()

SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

def create_access_token(customer_id: str, expires_delta: Optional[timedelta] = None):
    """Create JWT token"""
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = {
        "sub": customer_id,
        "exp": expire,
        "iat": datetime.utcnow()
    }
    
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def verify_token(credentials: HTTPAuthCredentials = Security(security)):
    """Verify JWT token"""
    
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        customer_id: str = payload.get("sub")
        
        if customer_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        
        return customer_id
    
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

# Usage in routes:
# async def protected_endpoint(customer_id: str = Depends(verify_token)):
```

### Step 2: `api/middleware/rate_limiter.py`

```python
from fastapi import Request, HTTPException, status
from datetime import datetime, timedelta
from collections import defaultdict
import asyncio

class RateLimiter:
    """Simple in-memory rate limiter"""
    
    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        self.requests = defaultdict(list)  # {client_ip: [timestamps]}
        self.lock = asyncio.Lock()
    
    async def check_rate_limit(self, request: Request) -> bool:
        """Check if client exceeded rate limit"""
        
        client_ip = request.client.host
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=1)
        
        async with self.lock:
            # Clean old requests
            self.requests[client_ip] = [
                t for t in self.requests[client_ip]
                if t > cutoff
            ]
            
            # Check limit
            if len(self.requests[client_ip]) >= self.requests_per_minute:
                return False
            
            # Record request
            self.requests[client_ip].append(now)
            return True

limiter = RateLimiter(requests_per_minute=60)

async def rate_limit_check(request: Request):
    """Middleware to check rate limits"""
    
    if not await limiter.check_rate_limit(request):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded"
        )
```

### Step 3: `api/middleware/error_handler.py`

```python
from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

async def global_exception_handler(request: Request, exc: Exception):
    """Handle all exceptions globally"""
    
    logger.error(
        f"Unhandled exception: {str(exc)}",
        extra={
            'path': request.url.path,
            'method': request.method,
            'client_ip': request.client.host if request.client else 'unknown',
            'timestamp': datetime.utcnow().isoformat()
        }
    )
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            'status': 'error',
            'message': 'Internal server error',
            'timestamp': datetime.utcnow().isoformat()
        }
    )

async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors"""
    
    logger.warning(
        f"Validation error: {exc}",
        extra={'path': request.url.path}
    )
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            'status': 'validation_error',
            'errors': exc.errors()
        }
    )
```

### Step 4: `utils/logger.py`

```python
import logging
import logging.config
import yaml
from pathlib import Path

def setup_logging():
    """Setup structured logging"""
    
    config_path = Path(__file__).parent.parent.parent / 'config' / 'logging.yaml'
    
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
        logging.config.dictConfig(config)
    else:
        # Fallback
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
    
    return logging.getLogger(__name__)

logger = setup_logging()
```

### Step 5: `config/logging.yaml`

```yaml
version: 1
disable_existing_loggers: false

formatters:
  standard:
    format: '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
  json:
    format: '%(asctime)s %(name)s %(levelname)s %(message)s'

handlers:
  console:
    class: logging.StreamHandler
    level: INFO
    formatter: standard
    stream: ext://sys.stdout
  
  file:
    class: logging.handlers.RotatingFileHandler
    level: DEBUG
    formatter: json
    filename: logs/ai_agronomist.log
    maxBytes: 10485760  # 10MB
    backupCount: 10

root:
  level: INFO
  handlers: [console, file]

loggers:
  api:
    level: DEBUG
    handlers: [console, file]
  
  database:
    level: DEBUG
    handlers: [file]
```

### Step 6: `utils/validators.py`

```python
from typing import Optional
import re

class InputValidator:
    """Validate user inputs"""
    
    @staticmethod
    def validate_customer_id(customer_id: str) -> bool:
        """Validate customer ID format"""
        
        if not customer_id:
            return False
        
        # Allow alphanumeric + underscore
        if not re.match(r'^[a-zA-Z0-9_]{3,50}$', customer_id):
            return False
        
        return True
    
    @staticmethod
    def validate_email(email: str) -> bool:
        """Validate email format"""
        
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))
    
    @staticmethod
    def validate_file_size(file_size_bytes: int, max_mb: int = 500) -> bool:
        """Validate file size"""
        
        max_bytes = max_mb * 1024 * 1024
        return file_size_bytes <= max_bytes
    
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Sanitize filename to prevent path traversal"""
        
        # Remove any path separators
        filename = filename.replace('/', '').replace('\\', '')
        
        # Remove special characters
        filename = re.sub(r'[^a-zA-Z0-9._-]', '', filename)
        
        return filename[:255]  # Max filename length
```

### Step 7: `docker/Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create logs directory
RUN mkdir -p logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/health')"

# Run application
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Step 8: `docker/docker-compose.yml`

```yaml
version: '3.8'

services:
  app:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql://postgres:${DB_PASSWORD}@db:5432/${DB_NAME}
      SECRET_KEY: ${SECRET_KEY}
      ENVIRONMENT: production
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - ./logs:/app/logs
      - ./models:/app/models
      - ./results:/app/results
    restart: unless-stopped
    networks:
      - ai-agronomist

  db:
    image: postgres:15
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ${DB_NAME}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped
    networks:
      - ai-agronomist

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    restart: unless-stopped
    networks:
      - ai-agronomist

volumes:
  postgres_data:

networks:
  ai-agronomist:
    driver: bridge
```

### Step 9: `docker/.env.example`

```bash
# Database
DB_PASSWORD=your_secure_password_here
DB_NAME=anomaly_detection_db

# Security
SECRET_KEY=your_jwt_secret_key_here_change_in_production

# Environment
ENVIRONMENT=production

# API
MAX_UPLOAD_SIZE_MB=500
ANOMALY_CONFIDENCE_THRESHOLD=0.3

# Logging
LOG_LEVEL=INFO
```

### Step 10: `docs/API.md`

```markdown
# AI Agronomist API Documentation

## Authentication

All endpoints (except `/health`) require JWT token:

```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "your_customer_id", "password": "your_password"}'
```

Response:
```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

Then use token in subsequent requests:
```bash
curl -X GET http://localhost:8000/api/anomaly/patterns/list \
  -H "Authorization: Bearer eyJhbGc..."
```

## Endpoints

### Training

**POST /api/anomaly/train**

Train seasonal patterns from CSV.

Request:
```bash
curl -X POST http://localhost:8000/api/anomaly/train \
  -H "Authorization: Bearer <token>" \
  -F "file=@data.csv" \
  -F "customer_id=customer_123"
```

Response:
```json
{
  "status": "training_complete",
  "patterns": {
    "spring": {
      "pattern_id": "pattern_spring_v1_2026-08-20",
      "rows_analyzed": 28000,
      "sensors": ["temperature", "humidity", "light"]
    },
    "summer": {...},
    "autumn": {...},
    "winter": {...}
  }
}
```

### Detection

**POST /api/anomaly/detect**

Detect anomalies using stored pattern.

Request:
```bash
curl -X POST http://localhost:8000/api/anomaly/detect \
  -H "Authorization: Bearer <token>" \
  -F "file=@new_data.csv" \
  -F "customer_id=customer_123" \
  -F "pattern_id=pattern_spring_v1_2026-08-20"
```

**POST /api/anomaly/analyze**

Train + Detect all-in-one.

### Pattern Management

**GET /api/anomaly/patterns/list**

List all patterns for customer.

**GET /api/anomaly/patterns/{pattern_id}**

Retrieve specific pattern.

**DELETE /api/anomaly/patterns/reset**

Hard reset all patterns (requires confirm=true).

### Feedback

**POST /api/feedback/mark-anomaly**

Record operator feedback (true/false positive).

**GET /api/feedback/statistics**

Get feedback collection statistics.

## Rate Limiting

- 60 requests per minute per IP
- Returns 429 if exceeded

## Error Responses

```json
{
  "status": "error",
  "message": "Descriptive error message",
  "timestamp": "2026-06-20T12:30:45.123456"
}
```
```

### Step 11: `docs/DEPLOYMENT.md`

```markdown
# Deployment Guide

## Prerequisites

- Docker & Docker Compose
- PostgreSQL 15+
- Python 3.11+

## Local Development

```bash
# Clone repo
git clone <repo>
cd ai-agronomist

# Create .env
cp docker/.env.example .env

# Edit .env with your settings
nano .env

# Start services
docker-compose up -d

# Initialize database
docker-compose exec app python -c "from database.session import init_db; init_db()"

# Check health
curl http://localhost:8000/health
```

## Production Deployment

### Option 1: AWS EC2

```bash
# 1. Create EC2 instance (Ubuntu 22.04, t3.medium)
# 2. SSH into instance
ssh -i key.pem ubuntu@your_instance_ip

# 3. Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# 4. Clone repo
git clone <repo>
cd ai-agronomist

# 5. Configure environment
cp docker/.env.example .env
# Edit .env with production values

# 6. Start services
docker-compose up -d

# 7. Setup monitoring (see MONITORING.md)
```

### Option 2: Kubernetes

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-agronomist
spec:
  replicas: 3
  selector:
    matchLabels:
      app: ai-agronomist
  template:
    metadata:
      labels:
        app: ai-agronomist
    spec:
      containers:
      - name: api
        image: your-registry/ai-agronomist:latest
        ports:
        - containerPort: 8000
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: db-credentials
              key: url
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
```

## Health Checks

```bash
# Check API health
curl http://localhost:8000/health

# Check database
docker-compose exec db psql -U postgres -d anomaly_db -c "SELECT 1"

# Check logs
docker-compose logs -f app
```

## Scaling

```bash
# Horizontal scaling
docker-compose up -d --scale app=3

# Load balancing
# Use nginx or cloud load balancer in front of instances
```

## Backup & Recovery

```bash
# Backup database
docker-compose exec db pg_dump -U postgres anomaly_db > backup.sql

# Restore
docker-compose exec -T db psql -U postgres anomaly_db < backup.sql

# Backup models
tar czf models_backup.tar.gz models/

# Restore
tar xzf models_backup.tar.gz
```
```

### Step 12: `docs/MONITORING.md`

```markdown
# Monitoring & Alerting

## Key Metrics

- **API Latency:** p50, p95, p99
- **Throughput:** Requests/sec
- **Error Rate:** % of 5xx responses
- **Database:** Connection count, query latency
- **Detection:** Anomalies/hour, false positive rate

## Setup Prometheus

```yaml
# prometheus.yml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'ai-agronomist'
    static_configs:
      - targets: ['localhost:8000']
```

## Setup Grafana

```bash
docker run -d -p 3000:3000 grafana/grafana
# Login: admin/admin
# Add Prometheus data source
# Import dashboard: https://grafana.com/...
```

## Alerts

High priority:
- API down (no /health response)
- Database unavailable
- Error rate > 5%
- False positive rate > 10%

Medium priority:
- Latency p95 > 2s
- Disk space < 10%

## Logs

View logs:
```bash
docker-compose logs -f app
```

Search logs:
```bash
docker-compose logs app | grep ERROR
```

## Performance Tuning

- Increase database pool size if connections exhausted
- Enable pattern caching (CACHE_TTL_MINUTES)
- Use read replicas for queries
```

### Step 13: `scripts/deploy.sh`

```bash
#!/bin/bash
set -e

echo "🚀 Deploying AI Agronomist..."

# Check prerequisites
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found"
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose not found"
    exit 1
fi

# Load environment
if [ ! -f .env ]; then
    echo "❌ .env file not found. Copy from .env.example"
    exit 1
fi

source .env

# Stop existing services
echo "Stopping existing services..."
docker-compose down || true

# Pull latest images
echo "Pulling latest images..."
docker-compose pull

# Start services
echo "Starting services..."
docker-compose up -d

# Wait for database
echo "Waiting for database..."
sleep 10

# Initialize database
echo "Initializing database..."
docker-compose exec -T app python -c "from database.session import init_db; init_db()"

# Run tests
echo "Running tests..."
docker-compose exec -T app pytest tests/ -v --tb=short

# Check health
echo "Checking health..."
for i in {1..30}; do
    if curl -f http://localhost:8000/health > /dev/null 2>&1; then
        echo "✅ Health check passed"
        break
    fi
    echo "Waiting for API... ($i/30)"
    sleep 1
done

echo "✅ Deployment complete!"
echo ""
echo "API is available at: http://localhost:8000"
echo "Docs at: http://localhost:8000/docs"
echo ""
echo "Next steps:"
echo "1. Create first customer: curl -X POST http://localhost:8000/api/auth/register"
echo "2. Upload CSV: curl -X POST http://localhost:8000/api/anomaly/train -F file=@data.csv"
echo "3. Monitor logs: docker-compose logs -f app"
```

### Step 14: `tests/security/test_auth.py`

```python
import pytest
from fastapi.testclient import TestClient
from api.main import app
from api.middleware.auth import create_access_token

client = TestClient(app)

def test_protected_endpoint_requires_token():
    """Unauth request should be rejected"""
    
    response = client.get("/api/anomaly/patterns/list")
    
    assert response.status_code == 403

def test_valid_token_accepted():
    """Valid token should allow access"""
    
    token = create_access_token("test_customer")
    
    response = client.get(
        "/api/anomaly/patterns/list",
        headers={"Authorization": f"Bearer {token}"}
    )
    
    # May 404 if customer doesn't exist, but not 401
    assert response.status_code != 401

def test_invalid_token_rejected():
    """Invalid token should be rejected"""
    
    response = client.get(
        "/api/anomaly/patterns/list",
        headers={"Authorization": "Bearer invalid_token"}
    )
    
    assert response.status_code == 401

def test_rate_limiting():
    """Rapid requests should be rate limited"""
    
    token = create_access_token("test_customer")
    
    # Make 100 requests rapidly
    responses = []
    for i in range(100):
        response = client.get(
            "/api/anomaly/patterns/list",
            headers={"Authorization": f"Bearer {token}"}
        )
        responses.append(response.status_code)
    
    # Some should be rate limited
    assert any(status == 429 for status in responses)
```

### Step 15: `tests/security/test_input_validation.py`

```python
import pytest
from utils.validators import InputValidator

def test_validate_customer_id():
    """Should validate customer ID format"""
    
    assert InputValidator.validate_customer_id("customer_123") == True
    assert InputValidator.validate_customer_id("cust@123") == False
    assert InputValidator.validate_customer_id("ab") == False  # Too short

def test_validate_email():
    """Should validate email format"""
    
    assert InputValidator.validate_email("user@example.com") == True
    assert InputValidator.validate_email("invalid_email") == False

def test_validate_file_size():
    """Should validate file size"""
    
    assert InputValidator.validate_file_size(100 * 1024 * 1024) == True  # 100MB
    assert InputValidator.validate_file_size(600 * 1024 * 1024) == False  # 600MB > 500MB limit

def test_sanitize_filename():
    """Should sanitize filename"""
    
    assert InputValidator.sanitize_filename("../etc/passwd") == "etcpasswd"
    assert InputValidator.sanitize_filename("data_2026.csv") == "data_2026.csv"
```

---

## ✅ SUCCESS CRITERIA

- [ ] JWT authentication works
- [ ] Rate limiting enforces 60 req/min
- [ ] All errors return structured JSON
- [ ] Logging captures all requests/errors
- [ ] Input validation prevents injection attacks
- [ ] Docker image builds without errors
- [ ] docker-compose up starts all services
- [ ] Health check passes
- [ ] Database initializes on startup
- [ ] All tests pass (security + integration)
- [ ] API docs available at /docs
- [ ] Deployment script completes successfully

---

## 🧪 TESTING COMMAND

```bash
pytest tests/security/ -v
# Expected: 20+/20+ passing

# Full test suite
pytest tests/ -v --cov=api --cov=engines --cov=detectors
# Expected: 200+/200+ passing with 80%+ coverage
```

---

## 🚀 FINAL CHECKLIST

- [ ] All 6 phases completed
- [ ] 200+ tests passing with 80%+ coverage
- [ ] Docker deployment tested locally
- [ ] API documentation complete
- [ ] Monitoring setup documented
- [ ] Security audit passed
- [ ] Performance benchmarks documented
- [ ] Ready for production deployment ✅

---

## 📋 NEXT STEPS AFTER GO-LIVE

1. **First Pilot Customer (Week 1-2)**
   - Onboard 1 customer
   - Collect feedback
   - Monitor false positive rate
   - Fine-tune thresholds

2. **Iterate (Week 3-4)**
   - Implement improvements from feedback
   - Add new detectors if needed
   - Improve explanations

3. **Scale (Week 5+)**
   - Onboard 5+ customers
   - Monitor system metrics
   - Plan Phase 1.5 (Priva API integration)

---

**auto-proceed**
