#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

# Multi-user service is intentionally isolated from the original service.
# Keep runtime config isolated; `app.py` reads `.env.multi` directly.

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8002}"
PID_FILE="logs/uvicorn-multi.pid"

export CLAUDE_WEB_DB_PATH="${CLAUDE_WEB_DB_PATH:-${PROJECT_DIR}/chat_multi.db}"

# shellcheck disable=SC1091
. "${PROJECT_DIR}/scripts/bootstrap-runtime.sh"
ensure_runtime_ready

pid_matches_port() {
  local pid="$1"
  [ -r "/proc/$pid/cmdline" ] || return 1
  tr '\000' ' ' < "/proc/$pid/cmdline" | grep -F -- "uvicorn app:app" | grep -F -- "--port ${PORT}" >/dev/null 2>&1
}

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && pid_matches_port "$old_pid"; then
    kill "$old_pid" >/dev/null 2>&1 || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

for proc_dir in /proc/[0-9]*; do
  pid="${proc_dir##*/}"
  if pid_matches_port "$pid"; then
    kill "$pid" >/dev/null 2>&1 || true
  fi
done
sleep 1

setsid "${VENV_PYTHON}" -m uvicorn app:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers 1 \
  --no-access-log \
  > logs/uvicorn-multi.log 2>&1 < /dev/null &

uvicorn_pid=$!
echo "$uvicorn_pid" > "$PID_FILE"

for _ in 1 2 3 4 5; do
  if ! kill -0 "$uvicorn_pid" >/dev/null 2>&1; then
    echo "error: multi-user backend failed; see logs/uvicorn-multi.log"
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "started multi-user service: http://${HOST}:${PORT}"
    exit 0
  fi
  sleep 1
done

echo "started multi-user service: http://${HOST}:${PORT}"
echo "warning: health check did not respond yet; see logs/uvicorn-multi.log"
