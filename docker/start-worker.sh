#!/usr/bin/env bash
set -euo pipefail

cd /app

# shellcheck source=docker/thread-env.sh
source docker/thread-env.sh

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

# The API service owns alembic migrations; the worker only needs the tables to
# exist. Retry until the jobs table is reachable instead of crash-looping.
echo "Waiting for database schema (jobs table)..."
python - <<'PY'
import asyncio, sys
from sqlalchemy import text

async def wait():
    from src.db.session import AsyncSessionLocal
    for attempt in range(30):
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1 FROM jobs LIMIT 1"))
            print("Database schema ready.")
            return
        except Exception as exc:
            print(f"Schema not ready (attempt {attempt + 1}/30): {exc}")
            await asyncio.sleep(5)
    sys.exit("Database schema never became ready — is the API deployed?")

asyncio.run(wait())
PY

echo "Starting queue worker..."
exec python -m src.workers.main
