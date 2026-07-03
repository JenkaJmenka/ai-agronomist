#!/usr/bin/env bash
set -euo pipefail

cd /app

export DATABASE_URL="$(python - <<'PY'
import os
url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./greenhouse.db")
if url.startswith("postgres://"):
    url = "postgresql+asyncpg://" + url[len("postgres://") :]
elif url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
    url = "postgresql+asyncpg://" + url[len("postgresql://") :]
print(url)
PY
)"

echo "Running database migrations..."
alembic upgrade head

PORT="${PORT:-8000}"
echo "Starting API on dual-stack fd bind, port ${PORT}"
exec python docker/serve.py
