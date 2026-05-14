#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BOOTSTRAP_INSTALL_NGROK="${BOOTSTRAP_INSTALL_NGROK:-1}"
BOOTSTRAP_AUTO_SYSTEM="${BOOTSTRAP_AUTO_SYSTEM:-1}"

# shellcheck disable=SC1091
. "${PROJECT_DIR}/scripts/bootstrap-runtime.sh"

echo "======================================"
echo " Claude Web installer"
echo "======================================"
echo
echo "项目目录：${PROJECT_DIR}"

ensure_runtime_ready

echo "== Python =="
echo "$("${VENV_PYTHON}" --version)"

echo "== 运行检查 =="
"${VENV_PYTHON}" -m py_compile app.py db.py config.py schemas.py chat_utils.py services.py

chmod +x install.sh deploy.sh start-local.sh start-public.sh start-ngrok.sh start-cloudflare.sh start-monitors.sh 2>/dev/null || true

echo
echo "安装完成。"
echo "启动本地服务：./start-local.sh"
echo "一键部署验证：./deploy.sh"
