#!/usr/bin/env bash
set -euo pipefail

cd /app

# shellcheck source=docker/thread-env.sh
source docker/thread-env.sh

export API_URL="${API_URL:-http://localhost:8000}"
# Render may pass hostname only via fromService
if [[ "${API_URL}" != http* ]]; then
  export API_URL="https://${API_URL}"
fi
PORT="${PORT:-8501}"

echo "Starting Streamlit dashboard on 0.0.0.0:${PORT} (API_URL=${API_URL})"
exec python -m streamlit run src/dashboard/app.py \
  --server.port="${PORT}" \
  --server.address=0.0.0.0 \
  --server.headless=true \
  --browser.gatherUsageStats=false
