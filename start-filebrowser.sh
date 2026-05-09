#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/ai/claude-web"
DB_FILE="$PROJECT_DIR/filebrowser.db"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/filebrowser.log"
PID_FILE="$LOG_DIR/filebrowser.pid"

mkdir -p "$LOG_DIR"

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "File Browser already running: http://127.0.0.1:8080"
    echo "LAN URL may be: http://192.168.100.100:8080"
    echo "pid: $old_pid"
    exit 0
  fi
fi

setsid filebrowser \
  --database "$DB_FILE" \
  --root "$PROJECT_DIR" \
  --address 0.0.0.0 \
  --port 8080 \
  --disableExec \
  --log "$LOG_FILE" \
  </dev/null >"$LOG_FILE" 2>&1 &

pid="$!"
echo "$pid" > "$PID_FILE"

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/ >/dev/null 2>&1; then
    echo "File Browser started: http://127.0.0.1:8080"
    echo "LAN URL may be: http://192.168.100.100:8080"
    echo "pid: $pid"
    echo "log: $LOG_FILE"
    exit 0
  fi
  sleep 1
done

echo "File Browser did not become ready in time. Check log: $LOG_FILE" >&2
exit 1
