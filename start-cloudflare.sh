#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

PORT="${1:-8000}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-cloudflared}"
CF_LOG="logs/cloudflared.log"
CF_PID_FILE="logs/cloudflared.pid"
CF_URL_FILE="cloudflare-url.txt"
CF_TOKEN_FILE="logs/cloudflared-token"
CF_PUBLIC_URL="${CLOUDFLARE_PUBLIC_URL:-}"
CF_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"
CF_PROTOCOL="${CLOUDFLARE_PROTOCOL:-http2}"
CF_EDGE_IP_VERSION="${CLOUDFLARE_EDGE_IP_VERSION:-4}"
NAMED_TUNNEL=0
LOCAL_URL="http://127.0.0.1:${PORT}"

fail() {
  echo "error: $*" >&2
  exit 1
}

if ! command -v "$CLOUDFLARED_BIN" >/dev/null 2>&1; then
  fail "cloudflared command not found. Install cloudflared first."
fi

START_PUBLIC=0 ./start-local.sh >/dev/null

if [ -f "$CF_PID_FILE" ]; then
  OLD_PID="$(cat "$CF_PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
fi

rm -f "$CF_LOG" "$CF_PID_FILE" "$CF_URL_FILE"

if [ -n "$CF_TUNNEL_TOKEN" ]; then
  umask 077
  printf '%s\n' "$CF_TUNNEL_TOKEN" > "$CF_TOKEN_FILE"
  unset CLOUDFLARE_TUNNEL_TOKEN
fi

if [ -s "$CF_TOKEN_FILE" ]; then
  NAMED_TUNNEL=1
  setsid "$CLOUDFLARED_BIN" tunnel --no-autoupdate --logfile "$CF_LOG" --protocol "$CF_PROTOCOL" --edge-ip-version "$CF_EDGE_IP_VERSION" run --token-file "$CF_TOKEN_FILE" \
    >"$CF_LOG.stdout" 2>&1 < /dev/null &
else
  setsid "$CLOUDFLARED_BIN" tunnel --url "$LOCAL_URL" \
    --no-autoupdate \
    --protocol "$CF_PROTOCOL" \
    --edge-ip-version "$CF_EDGE_IP_VERSION" \
    --logfile "$CF_LOG" \
    >"$CF_LOG.stdout" 2>&1 < /dev/null &
fi

echo $! > "$CF_PID_FILE"

PUBLIC_URL="$CF_PUBLIC_URL"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if [ -n "$PUBLIC_URL" ]; then
    break
  fi
  PUBLIC_URL="$(awk 'match($0, /https:\/\/[-a-zA-Z0-9.]+\.trycloudflare\.com/) { print substr($0, RSTART, RLENGTH); exit }' "$CF_LOG" "$CF_LOG.stdout" 2>/dev/null || true)"
  sleep 1
done

if [ -z "$PUBLIC_URL" ]; then
  if [ "$NAMED_TUNNEL" = "1" ]; then
    echo "started: named Cloudflare Tunnel"
    echo "pid saved to: ${CF_PID_FILE}"
    echo "set CLOUDFLARE_PUBLIC_URL=https://your-domain.example to enable health checks"
    exit 0
  fi
  echo "cloudflared started, but no public URL was detected"
  echo "for named tunnels, set CLOUDFLARE_PUBLIC_URL=https://your-domain.example"
  echo "see $CF_LOG and $CF_LOG.stdout"
  exit 1
fi

printf '%s\n' "$PUBLIC_URL" > "$CF_URL_FILE"

for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl -fsS --max-time 5 "${PUBLIC_URL}/api/health" >/dev/null 2>&1; then
    echo "started: ${PUBLIC_URL}"
    exit 0
  fi
  sleep 1
done

echo "started: ${PUBLIC_URL}"
echo "warning: public health check did not respond yet; see $CF_LOG"
