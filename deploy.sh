#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PUBLIC_URL="${PUBLIC_URL:-https://kindling-shaft-creamer.ngrok-free.dev}"
LOCAL_URL="${LOCAL_URL:-http://127.0.0.1:8000}"

# shellcheck disable=SC1091
. "${PROJECT_DIR}/scripts/bootstrap-runtime.sh"

echo "== Claude Web deploy =="
echo "project: $(pwd)"

chmod +x install.sh start-local.sh start-public.sh start-ngrok.sh start-cloudflare.sh start-monitors.sh scripts/smoke_test.py 2>/dev/null || true

echo "== install dependencies =="
BOOTSTRAP_INSTALL_NGROK="${BOOTSTRAP_INSTALL_NGROK:-1}"
ensure_runtime_ready

echo "== compile check =="
"${VENV_PYTHON}" -m py_compile app.py db.py config.py schemas.py chat_utils.py services.py

echo "== start service =="
./start-local.sh

echo "== enable monitors =="
./start-monitors.sh || true

echo "== local smoke test =="
"${VENV_PYTHON}" scripts/smoke_test.py "$LOCAL_URL"

echo "== public health check =="
if curl -fsS -H "ngrok-skip-browser-warning: 1" "${PUBLIC_URL}/api/health" >/dev/null 2>&1; then
  echo "public: ok ${PUBLIC_URL}"
else
  echo "warning: public health check failed; local service is still available at ${LOCAL_URL}"
fi

echo "== done =="
echo "local:  ${LOCAL_URL}"
echo "public: ${PUBLIC_URL}"
