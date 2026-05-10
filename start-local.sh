#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if pgrep -f "uvicorn app:app .*--port ${PORT}" >/dev/null 2>&1; then
  pkill -f "uvicorn app:app .*--port ${PORT}" || true
  sleep 1
fi

setsid .venv/bin/python -m uvicorn app:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers 1 \
  --no-access-log \
  > logs/uvicorn-local.log 2>&1 < /dev/null &

echo $! > logs/uvicorn-local.pid
for _ in 1 2 3 4 5; do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "started: http://${HOST}:${PORT}"
    exit 0
  fi
  sleep 1
done

echo "started: http://${HOST}:${PORT}"
echo "warning: health check did not respond yet; see logs/uvicorn-local.log"
