# Docker Local Development Setup

## Quick Start

### 1. Prerequisites
- Docker: https://docs.docker.com/get-docker/
- Docker Compose: https://docs.docker.com/compose/install/

### 2. Start Services

```bash
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
```

### 3. Verify Services

#### Database (PostgreSQL)
```bash
docker-compose exec postgres psql -U postgres -d greenhouse_monitoring -c "SELECT 1;"
# Expected: (1 row)
```

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

```bash
# Option 1: Inside Jupyter
# 1. Open http://localhost:8888
# 2. Create new Python notebook
# 3. Import and run training code

# Option 2: Command line
docker-compose exec jupyter python scripts/train_lstm_vae.py --epochs 100

# Option 3: Local environment
source venv/bin/activate
python scripts/train_lstm_vae.py --epochs 100
```

### 5. View Results

#### TensorBoard
```bash
# Automatically running at http://localhost:6006
# Shows:
# - Training/validation loss
# - Accuracy metrics
# - Model weight histograms
# - Latent space visualization
```

#### Database
```bash
docker-compose exec postgres psql -U postgres -d greenhouse_monitoring

# View tables
\d

# Query data
SELECT COUNT(*) FROM sensor_readings;
```

### 6. Cleaning Up

```bash
# Stop services (keep data)
docker-compose down

# Stop services and remove data
docker-compose down -v

# Remove images
docker-compose down -v --rmi all
```

### 7. Troubleshooting

#### "Port already in use"
```bash
# Find process using port
lsof -i :5432  # PostgreSQL
lsof -i :8080  # Adminer
lsof -i :8888  # Jupyter
lsof -i :6006  # TensorBoard

# Stop conflict process or change port in docker-compose.yml
```

#### "Container won't start"
```bash
# Check logs
docker-compose logs <service_name>

# Rebuild images
docker-compose build --no-cache
docker-compose up -d
```

#### "Database connection refused"
```bash
# Wait for postgres to fully start
docker-compose exec postgres pg_isready

# Restart postgres
docker-compose restart postgres
docker-compose exec postgres psql -U postgres -d greenhouse_monitoring -c "SELECT 1;"
```

### 8. Common Commands

```bash
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
```

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

### 11. PostgreSQL incident memory (production)

After loading a meaningful volume of incidents (migration `002` creates ivfflat on an empty table), rebuild the ANN index so cluster centroids reflect real embeddings:

```bash
# Local docker stack
docker compose exec postgres psql -U postgres -d greenhouse_monitoring \
  -c "REINDEX INDEX ix_incidents_embedding_ivfflat;"

# Or drop and recreate (same effect for ivfflat)
docker compose exec postgres psql -U postgres -d greenhouse_monitoring \
  -c "DROP INDEX IF EXISTS ix_incidents_embedding_ivfflat; \
      CREATE INDEX ix_incidents_embedding_ivfflat ON incidents \
      USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);"
```

Run pgvector integration tests locally:

```bash
docker compose up -d postgres
venv/bin/python -m pytest tests/integration/test_pgvector_incident_memory.py \
  -m postgres -q --override-ini="addopts="
```

CI runs the same suite via `.github/workflows/postgres-tests.yml`.
