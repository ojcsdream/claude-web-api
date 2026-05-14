#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

CC_CONFIG="${CC_CONFIG:-/home/ai/.cc-connect/config.toml}"
CC_BIN="${CC_BIN:-cc-connect}"
LOG_FILE="${CC_LOG_FILE:-logs/cc-connect.log}"
PID_FILE="${CC_PID_FILE:-logs/cc-connect.pid}"

if [ ! -f "$CC_CONFIG" ]; then
  echo "cc-connect config not found: $CC_CONFIG" >&2
  exit 1
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "cc-connect already running: $(cat "$PID_FILE")"
  exit 0
fi

if pgrep -af "cc-connect --config ${CC_CONFIG}" >/dev/null 2>&1; then
  pkill -f "cc-connect --config ${CC_CONFIG}" || true
  sleep 1
fi

setsid "$CC_BIN" --config "$CC_CONFIG" --force > "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if pgrep -f "cc-connect --config ${CC_CONFIG}" >/dev/null 2>&1; then
    echo "cc-connect started: $PID_FILE"
    exit 0
  fi
  sleep 1
done

echo "cc-connect start requested; see $LOG_FILE" >&2
exit 0
