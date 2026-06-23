# 📋 TASK 4: Local Development Environment (Docker)
## Thursday Morning - Stage 1 (Gemma Code Generation)

### **Task Type:** DevOps / Infrastructure
### **Agents:** DevOps Lead (primary)
### **Estimated Time:** 10 minutes
### **Success Criteria:** Docker environment runs, all services healthy

### **Depends On:** Task 1-3 completion (code exists)

---

## **COMPLETE TASK PROMPT FOR STAGE 1**

```
Generate Docker setup for local development of LSTM-VAE model.
This allows team members to work with consistent environment.

## 1. Docker Compose Configuration

Create docker-compose.yml:

```yaml
version: '3.9'

services:
  # PostgreSQL database for storing sensor readings
  postgres:
    image: postgres:15-alpine
    container_name: agronomist-postgres
    environment:
      POSTGRES_DB: greenhouse_monitoring
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres_dev_password
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - agronomist-network

  # Adminer for database management (optional, development only)
  adminer:
    image: adminer:latest
    container_name: agronomist-adminer
    ports:
      - "8080:8080"
    depends_on:
      - postgres
    networks:
      - agronomist-network

  # Jupyter for interactive development
  jupyter:
    build:
      context: .
      dockerfile: Dockerfile.jupyter
    container_name: agronomist-jupyter
    ports:
      - "8888:8888"
    volumes:
      - .:/workspace
      - jupyter_data:/root/.jupyter
    environment:
      JUPYTER_ENABLE_LAB: "yes"
    command: jupyter lab --ip=0.0.0.0 --allow-root --no-browser
    depends_on:
      - postgres
    networks:
      - agronomist-network

  # TensorBoard for training visualization
  tensorboard:
    image: tensorflow/tensorflow:latest-jupyter
    container_name: agronomist-tensorboard
    ports:
      - "6006:6006"
    volumes:
      - ./logs/tensorboard:/logs
    command: tensorboard --logdir=/logs --host=0.0.0.0
    networks:
      - agronomist-network

volumes:
  postgres_data:
  jupyter_data:

networks:
  agronomist-network:
    driver: bridge
```

---

## 2. Dockerfile for Development

Create Dockerfile:

```dockerfile
# Multi-stage build for efficiency

# Stage 1: Builder
FROM python:3.11-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Create wheels
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /build/wheels -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels from builder
COPY --from=builder /build/wheels /wheels
COPY --from=builder /build/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache /wheels/*

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p logs/tensorboard \
    && mkdir -p models/checkpoints \
    && mkdir -p data/synthetic

# Health check
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import torch; print('OK')" || exit 1

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV LOG_LEVEL=INFO
ENV DATABASE_URL=postgresql://postgres:postgres_dev_password@postgres:5432/greenhouse_monitoring

# Expose port for API (will be used in Phase 2)
EXPOSE 8000

# Default command
CMD ["python", "-c", "import src; print('Ready for training')"]
```

---

## 3. Jupyter Dockerfile

Create Dockerfile.jupyter:

```dockerfile
FROM python:3.11-slim

WORKDIR /workspace

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    postgresql-client \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir \
    jupyterlab \
    ipykernel \
    ipywidgets \
    matplotlib \
    seaborn \
    plotly

# Create necessary directories
RUN mkdir -p /workspace/logs/tensorboard

# Health check
HEALTHCHECK --interval=10s --timeout=5s CMD curl -f http://localhost:8888/lab || exit 1

# Environment
ENV PYTHONUNBUFFERED=1
ENV DATABASE_URL=postgresql://postgres:postgres_dev_password@postgres:5432/greenhouse_monitoring

EXPOSE 8888

CMD ["jupyter", "lab", "--ip=0.0.0.0", "--allow-root", "--no-browser"]
```

---

## 4. Environment Configuration

Create .env.development:

```bash
# Database
DATABASE_URL=postgresql://postgres:postgres_dev_password@postgres:5432/greenhouse_monitoring
POSTGRES_DB=greenhouse_monitoring
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres_dev_password

# Application
DEBUG=True
LOG_LEVEL=DEBUG
PYTHONUNBUFFERED=1

# Paths (relative to container)
CHECKPOINT_DIR=/app/models/checkpoints
DATA_DIR=/app/data/synthetic
LOG_DIR=/app/logs/tensorboard

# Training
DEVICE=cpu  # Use 'cuda' if GPU available
RANDOM_SEED=42
```

Create .env.example (template):

```bash
# Database
DATABASE_URL=postgresql://user:password@hostname:5432/database_name
POSTGRES_DB=greenhouse_monitoring
POSTGRES_USER=postgres
POSTGRES_PASSWORD=change_me

# Application
DEBUG=False
LOG_LEVEL=INFO
PYTHONUNBUFFERED=1

# Paths
CHECKPOINT_DIR=/app/models/checkpoints
DATA_DIR=/app/data/synthetic
LOG_DIR=/app/logs/tensorboard

# Training
DEVICE=cpu
RANDOM_SEED=42
```

---

## 5. Docker Ignore

Create .dockerignore:

```
__pycache__
*.pyc
*.pyo
*.pyd
.Python
env/
venv/
.venv/
.git
.gitignore
.dockerignore
.idea
.vscode
*.swp
*.swo
*~
.DS_Store
.coverage
htmlcov/
dist/
build/
*.egg-info/
.pytest_cache/
logs/tensorboard/*
models/checkpoints/*
data/synthetic/*
notebooks/.ipynb_checkpoints/
*.log
```

---

## 6. Requirements File

Create requirements.txt (Python dependencies):

```
# Core ML
torch==2.0.0
numpy==1.24.3

# Data processing
pandas==2.0.0
scikit-learn==1.2.2

# Database (for Phase 2)
sqlalchemy==2.0.15
psycopg2-binary==2.9.6
alembic==1.10.4

# Logging & Monitoring
tensorboard==2.12.0

# Testing
pytest==7.3.1
pytest-cov==4.1.0

# Code quality
black==23.3.0
flake8==6.0.0
isort==5.12.0

# Development
ipython==8.12.0
ipdb==0.13.13
```

---

## 7. Local Development Setup Guide

Create docs/DOCKER_SETUP.md:

```markdown
# Docker Local Development Setup

## Quick Start

### 1. Prerequisites
- Docker: https://docs.docker.com/get-docker/
- Docker Compose: https://docs.docker.com/compose/install/

### 2. Start Services

\`\`\`bash
# Clone repo
git clone <repo-url>
cd ai-agronomist

# Copy environment
cp .env.example .env.development

# Start services
docker-compose up -d

# Wait for services to be healthy
docker-compose ps

# Expected output:
# NAME                 STATUS
# agronomist-postgres  healthy
# agronomist-jupyter   up
# agronomist-tensorboard  up
# agronomist-adminer   up
\`\`\`

### 3. Verify Services

#### Database (PostgreSQL)
\`\`\`bash
docker-compose exec postgres psql -U postgres -d greenhouse_monitoring -c "SELECT 1;"
# Expected: (1 row)
\`\`\`

#### Adminer (Database UI)
- Access: http://localhost:8080
- Server: postgres
- Username: postgres
- Password: postgres_dev_password

#### Jupyter Lab (Development)
- Access: http://localhost:8888
- All notebooks automatically connected to database
- Libraries: torch, pandas, sklearn, tensorboard pre-installed

#### TensorBoard (Training Visualization)
- Access: http://localhost:6006
- Displays training metrics in real-time

### 4. Run Training

\`\`\`bash
# Option 1: Inside Jupyter
# 1. Open http://localhost:8888
# 2. Create new Python notebook
# 3. Import and run training code

# Option 2: Command line
docker-compose exec jupyter python scripts/train_lstm_vae.py --epochs 100

# Option 3: Local environment
source venv/bin/activate
python scripts/train_lstm_vae.py --epochs 100
\`\`\`

### 5. View Results

#### TensorBoard
\`\`\`bash
# Automatically running at http://localhost:6006
# Shows:
# - Training/validation loss
# - Accuracy metrics
# - Model weight histograms
# - Latent space visualization
\`\`\`

#### Database
\`\`\`bash
docker-compose exec postgres psql -U postgres -d greenhouse_monitoring

# View tables
\d

# Query data
SELECT COUNT(*) FROM sensor_readings;
\`\`\`

### 6. Cleaning Up

\`\`\`bash
# Stop services (keep data)
docker-compose down

# Stop services and remove data
docker-compose down -v

# Remove images
docker-compose down -v --rmi all
\`\`\`

### 7. Troubleshooting

#### "Port already in use"
\`\`\`bash
# Find process using port
lsof -i :5432  # PostgreSQL
lsof -i :8080  # Adminer
lsof -i :8888  # Jupyter
lsof -i :6006  # TensorBoard

# Stop conflict process or change port in docker-compose.yml
\`\`\`

#### "Container won't start"
\`\`\`bash
# Check logs
docker-compose logs <service_name>

# Rebuild images
docker-compose build --no-cache
docker-compose up -d
\`\`\`

#### "Database connection refused"
\`\`\`bash
# Wait for postgres to fully start
docker-compose exec postgres pg_isready

# Restart postgres
docker-compose restart postgres
docker-compose exec postgres psql -U postgres -d greenhouse_monitoring -c "SELECT 1;"
\`\`\`

### 8. Common Commands

\`\`\`bash
# View logs
docker-compose logs -f <service_name>

# Execute command in container
docker-compose exec <service> <command>

# Run bash inside container
docker-compose exec jupyter bash

# Rebuild specific service
docker-compose build <service>

# Pull latest images
docker-compose pull

# Health status
docker-compose ps
\`\`\`

### 9. Development Workflow

1. Modify code locally (in your IDE)
2. Code changes reflected in Jupyter via volume mount
3. Train model in Jupyter or via CLI
4. View results in TensorBoard (http://localhost:6006)
5. Checkpoints saved to local `models/checkpoints/`

### 10. Notes

- **Database**: PostgreSQL running in container, data persists in named volume
- **Volumes**: All code and data mounted, changes reflected immediately
- **Jupyter**: Full development environment with all dependencies
- **TensorBoard**: Training metrics logged automatically
- **Network**: Services communicate via `agronomist-network`

\`\`\`

---

## 8. Health Check Script

Create scripts/health-check.sh:

```bash
#!/bin/bash

echo "Checking Agronomist services health..."

# Check PostgreSQL
echo -n "PostgreSQL: "
if docker-compose exec -T postgres pg_isready -U postgres &>/dev/null; then
    echo "✓ Healthy"
else
    echo "✗ Not responding"
fi

# Check Jupyter
echo -n "Jupyter: "
if curl -s http://localhost:8888/lab &>/dev/null; then
    echo "✓ Healthy"
else
    echo "✗ Not responding"
fi

# Check TensorBoard
echo -n "TensorBoard: "
if curl -s http://localhost:6006 &>/dev/null; then
    echo "✓ Healthy"
else
    echo "✗ Not responding"
fi

# Check Adminer
echo -n "Adminer: "
if curl -s http://localhost:8080 &>/dev/null; then
    echo "✓ Healthy"
else
    echo "✗ Not responding"
fi

echo ""
echo "Full status:"
docker-compose ps
```

---

## 9. Docker Deliverables

### Configuration Files (to be created)
- [ ] `docker-compose.yml` (PostgreSQL, Jupyter, TensorBoard, Adminer)
- [ ] `Dockerfile` (Main application)
- [ ] `Dockerfile.jupyter` (Jupyter Lab development)
- [ ] `.env.development` (Development environment variables)
- [ ] `.env.example` (Template for environment)
- [ ] `.dockerignore` (Optimize image size)
- [ ] `requirements.txt` (Python dependencies)
- [ ] `scripts/health-check.sh` (Service health verification)
- [ ] `docs/DOCKER_SETUP.md` (Complete setup guide)

### Services Included
- ✅ PostgreSQL 15 (database)
- ✅ Jupyter Lab (interactive development)
- ✅ TensorBoard (training visualization)
- ✅ Adminer (database UI)
- ✅ Network isolation & health checks

---

## SUBMISSION INSTRUCTIONS

### Stage 1: Code Generation (Gemma)
```bash
# At orchestrator "You: " prompt:
@devops-lead

[Paste this entire task (sections 1-9 above)]
```

### Stage 2: File Creation (Sonnet)
```bash
@integrator

[Paste Stage 1 code output]

Please:
1. Create all Docker configuration files
2. Build and test: docker-compose build
3. Start services: docker-compose up -d
4. Verify all services healthy: docker-compose ps
5. Test database connection
6. Report status
```

---

## EXPECTED OUTPUT

After completion:
- Docker images built: ✅
- All services running: ✅
- PostgreSQL healthy: ✅
- Jupyter accessible: ✅
- TensorBoard accessible: ✅
- Database persists across restarts: ✅

```

---

