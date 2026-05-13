#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PUBLIC_URL="${PUBLIC_URL:-https://kindling-shaft-creamer.ngrok-free.dev}"
LOCAL_URL="${LOCAL_URL:-http://127.0.0.1:8000}"

echo "== Claude Web deploy =="
echo "project: $(pwd)"

chmod +x install.sh start-local.sh start-public.sh start-ngrok.sh start-cloudflare.sh start-monitors.sh scripts/smoke_test.py 2>/dev/null || true

echo "== install dependencies =="
./install.sh

echo "== compile check =="
.venv/bin/python -m py_compile app.py db.py config.py schemas.py chat_utils.py services.py

echo "== start service =="
./start-local.sh

echo "== enable monitors =="
./start-monitors.sh || true

echo "== local smoke test =="
.venv/bin/python scripts/smoke_test.py "$LOCAL_URL"

echo "== public health check =="
if curl -fsS -H "ngrok-skip-browser-warning: 1" "${PUBLIC_URL}/api/health" >/dev/null 2>&1; then
  echo "public: ok ${PUBLIC_URL}"
else
  echo "warning: public health check failed; local service is still available at ${LOCAL_URL}"
fi

echo "== done =="
echo "local:  ${LOCAL_URL}"
echo "public: ${PUBLIC_URL}"
