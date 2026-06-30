# Production image: full FastAPI + Streamlit stack (CPU PyTorch)
FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# CPU-only PyTorch keeps the image deployable on standard hosts
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir asyncpg \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker/start-api.sh docker/start-dashboard.sh \
    && mkdir -p logs models/checkpoints data/synthetic \
    && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO

EXPOSE 8000 8501

CMD ["/app/docker/start-api.sh"]
