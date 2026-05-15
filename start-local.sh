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
PID_FILE="logs/uvicorn-local.pid"

ensure_runtime_ready

pid_matches() {
  local pid="$1"
  local expected="$2"
  [ -n "$pid" ] || return 1
  [ -r "/proc/$pid/cmdline" ] || return 1
  tr '\000' ' ' < "/proc/$pid/cmdline" | grep -F -- "$expected" >/dev/null 2>&1
}

find_matching_pids() {
  local expected="$1"
  local cmdline
  local comm
  for proc_dir in /proc/[0-9]*; do
    [ -r "$proc_dir/cmdline" ] || continue
    comm="$(cat "$proc_dir/comm" 2>/dev/null || true)"
    case "$comm" in
      bash|sh|dash|zsh|fish) continue ;;
    esac
    cmdline="$(tr '\000' ' ' < "$proc_dir/cmdline" 2>/dev/null || true)"
    case "$cmdline" in
      *"$expected"* )
        printf '%s\n' "${proc_dir##*/}"
        ;;
    esac
  done
}

stop_existing_backend() {
  local pattern="uvicorn app:app"
  local port_pattern="--port ${PORT}"
  local pid

  if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if pid_matches "$pid" "$pattern"; then
      kill "$pid" >/dev/null 2>&1 || true
      sleep 1
    fi
    rm -f "$PID_FILE"
  fi

  for pid in $(find_matching_pids "$pattern"); do
    if pid_matches "$pid" "$port_pattern"; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
  sleep 1
}

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

stop_existing_backend

setsid "${VENV_PYTHON}" -m uvicorn app:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers 1 \
  --no-access-log \
  > logs/uvicorn-local.log 2>&1 < /dev/null &

uvicorn_pid=$!
echo "$uvicorn_pid" > "$PID_FILE"
for _ in 1 2 3 4 5; do
  if ! kill -0 "$uvicorn_pid" >/dev/null 2>&1; then
    echo "error: backend failed to stay running; see logs/uvicorn-local.log"
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    for pid in $(find_matching_pids "uvicorn app:app"); do
      if pid_matches "$pid" "--port ${PORT}"; then
        echo "$pid" > "$PID_FILE"
        break
      fi
    done
    start_public_if_enabled
    echo "started: http://${HOST}:${PORT}"
    exit 0
  fi
  sleep 1
done

if ! kill -0 "$uvicorn_pid" >/dev/null 2>&1; then
  echo "error: backend failed to stay running; see logs/uvicorn-local.log"
  rm -f "$PID_FILE"
  exit 1
fi

echo "started: http://${HOST}:${PORT}"
echo "warning: health check did not respond yet; see logs/uvicorn-local.log"
start_public_if_enabled
