#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

BOOTSTRAP_AUTO_SYSTEM="${BOOTSTRAP_AUTO_SYSTEM:-1}"

# shellcheck disable=SC1091
. "${PROJECT_DIR}/scripts/bootstrap-runtime.sh"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
START_PUBLIC="${START_PUBLIC:-1}"

ensure_runtime_ready

start_public_if_enabled() {
  if [ "$START_PUBLIC" != "1" ] || [ ! -x ./start-public.sh ]; then
    return 0
  fi

  if START_PUBLIC=0 START_LOCAL=0 ./start-public.sh "$PORT"; then
    return 0
  fi

  echo "warning: public tunnel did not start; local service is still running"
  echo "warning: see logs/ngrok.log"
  return 0
}

if pgrep -f "uvicorn app:app .*--port ${PORT}" >/dev/null 2>&1; then
  pkill -f "uvicorn app:app .*--port ${PORT}" || true
  sleep 1
fi

setsid "${VENV_PYTHON}" -m uvicorn app:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers 1 \
  --no-access-log \
  > logs/uvicorn-local.log 2>&1 < /dev/null &

echo $! > logs/uvicorn-local.pid
for _ in 1 2 3 4 5; do
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    start_public_if_enabled
    echo "started: http://${HOST}:${PORT}"
    exit 0
  fi
  sleep 1
done

echo "started: http://${HOST}:${PORT}"
echo "warning: health check did not respond yet; see logs/uvicorn-local.log"
start_public_if_enabled
