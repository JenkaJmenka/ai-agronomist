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
