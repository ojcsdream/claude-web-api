#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

NGROK_URL="${NGROK_URL:-kindling-shaft-creamer.ngrok-free.dev}" START_LOCAL="${START_LOCAL:-1}" exec ./start-ngrok.sh "${1:-8000}"
