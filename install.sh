#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10

echo "======================================"
echo " Claude Web Linux installer"
echo "======================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未找到 python3。"
  echo "Debian/Ubuntu 可尝试：sudo apt install python3 python3-venv python3-pip"
  exit 1
fi

PYTHON_VERSION_OK="$(python3 - <<'PY'
import sys
print("1" if sys.version_info >= (3, 10) else "0")
PY
)"

if [ "$PYTHON_VERSION_OK" != "1" ]; then
  echo "错误：需要 Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+。当前版本：$(python3 --version)"
  exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "错误：当前 python3 不支持 venv。"
  echo "Debian/Ubuntu 可尝试：sudo apt install python3-venv python3-pip"
  exit 1
fi

if [ ! -f requirements.txt ]; then
  echo "错误：未找到 requirements.txt。请确认在项目根目录运行。"
  exit 1
fi

echo "项目目录：$PROJECT_DIR"
echo "Python：$(python3 --version)"
echo

echo "安全提醒：以下文件/目录属于本机运行数据，不应提交到 GitHub："
echo "  chat.db uploads/ logs/ .venv/ .env admin-token.txt filebrowser.db ngrok-url.txt"
echo "安装脚本不会删除这些文件，也不会写入 API Key。"
echo

if [ ! -d .venv ]; then
  echo "创建虚拟环境 .venv ..."
  python3 -m venv .venv
else
  echo "复用已有虚拟环境 .venv"
fi

echo "升级 pip ..."
.venv/bin/python -m pip install --upgrade pip

echo "安装 Python 依赖 ..."
.venv/bin/python -m pip install -r requirements.txt

echo "创建运行目录 ..."
mkdir -p uploads logs

echo "检查 Python 语法 ..."
.venv/bin/python -m py_compile app.py db.py config.py schemas.py chat_utils.py services.py

echo
echo "安装完成。"
echo "启动服务："
echo "  ./start-local.sh"
echo
echo "访问地址："
echo "  http://127.0.0.1:8000"
echo
echo "部署后请在 Web 管理界面配置 API 接入商，不要把 API Key 写入公开仓库。"
