#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

PORT="${1:-8000}"
NGROK_BIN="${NGROK_BIN:-ngrok}"
NGROK_API="http://127.0.0.1:4040/api/tunnels"
NGROK_LOG="logs/ngrok.log"
NGROK_PID_FILE="logs/ngrok.pid"
NGROK_URL_FILE="ngrok-url.txt"
NGROK_URL="${NGROK_URL:-}"

./start-local.sh >/dev/null

if pgrep -f "ngrok http" >/dev/null 2>&1; then
  pkill -f "ngrok http" || true
  sleep 1
fi

rm -f "$NGROK_LOG" "$NGROK_PID_FILE"

if [ -n "$NGROK_URL" ]; then
  setsid "$NGROK_BIN" http --log=stdout --url="$NGROK_URL" "http://127.0.0.1:${PORT}" \
    >"$NGROK_LOG" 2>&1 < /dev/null &
else
  setsid "$NGROK_BIN" http --log=stdout "http://127.0.0.1:${PORT}" \
    >"$NGROK_LOG" 2>&1 < /dev/null &
fi

echo $! > "$NGROK_PID_FILE"

PUBLIC_URL=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS "$NGROK_API" >/tmp/claude-web-ngrok-api.json 2>/dev/null; then
    PUBLIC_URL="$(python - <<'PY'
import json
from pathlib import Path
path = Path('/tmp/claude-web-ngrok-api.json')
data = json.loads(path.read_text(encoding='utf-8'))
tunnels = data.get('tunnels') or []
for item in tunnels:
    url = item.get('public_url') or ''
    if url.startswith('https://'):
        print(url)
        break
PY
)"
    if [ -n "$PUBLIC_URL" ]; then
      break
    fi
  fi
  sleep 1
done

if [ -z "$PUBLIC_URL" ]; then
  echo "ngrok failed to produce a public URL"
  echo "see $NGROK_LOG"
  exit 1
fi

printf '%s\n' "$PUBLIC_URL" > "$NGROK_URL_FILE"

for _ in 1 2 3 4 5 6 7 8; do
  if curl -fsS "${PUBLIC_URL}/api/health" >/dev/null 2>&1; then
    echo "started: ${PUBLIC_URL}"
    exit 0
  fi
  sleep 1
done

echo "started: ${PUBLIC_URL}"
echo "warning: public health check did not respond yet; see $NGROK_LOG"
