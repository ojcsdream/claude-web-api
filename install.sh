#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10
BIN_DIR="${HOME}/.local/bin"
NGROK_BIN="${BIN_DIR}/ngrok"

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

download_ngrok() {
  if command -v ngrok >/dev/null 2>&1; then
    echo "检测到 ngrok：$(command -v ngrok)"
    return 0
  fi

  if [ -x "$NGROK_BIN" ]; then
    echo "检测到 ngrok：$NGROK_BIN"
    return 0
  fi

  if ! command -v tar >/dev/null 2>&1; then
    echo "警告：未找到 tar，跳过 ngrok 自动安装。"
    return 1
  fi

  if command -v curl >/dev/null 2>&1; then
    DOWNLOADER="curl -fsSL"
  elif command -v wget >/dev/null 2>&1; then
    DOWNLOADER="wget -qO-"
  else
    echo "警告：未找到 curl 或 wget，跳过 ngrok 自动安装。"
    return 1
  fi

  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64|arm64)
      NGROK_URL="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz"
      ;;
    x86_64|amd64)
      NGROK_URL="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz"
      ;;
    armv7l|armhf)
      NGROK_URL="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm.tgz"
      ;;
    *)
      echo "警告：不支持自动安装 ngrok 的架构：$ARCH"
      return 1
      ;;
  esac

  TMP_DIR="$(mktemp -d)"
  mkdir -p "$BIN_DIR"
  echo "安装 ngrok 到 $NGROK_BIN ..."
  if ! sh -c "$DOWNLOADER '$NGROK_URL' | tar -xz -C '$TMP_DIR'"; then
    rm -rf "$TMP_DIR"
    echo "警告：ngrok 下载或解压失败，公网启动需手动安装 ngrok。"
    return 1
  fi
  mv "$TMP_DIR/ngrok" "$NGROK_BIN"
  chmod +x "$NGROK_BIN"
  rm -rf "$TMP_DIR"
  echo "ngrok 安装完成：$NGROK_BIN"
}

configure_ngrok_token() {
  if [ -z "${NGROK_AUTHTOKEN:-}" ]; then
    echo "未设置 NGROK_AUTHTOKEN，跳过 ngrok token 配置。"
    echo "固定公网域名需要在启动前配置 ngrok authtoken。"
    return 0
  fi

  if command -v ngrok >/dev/null 2>&1; then
    NGROK_CMD="$(command -v ngrok)"
  else
    NGROK_CMD="$NGROK_BIN"
  fi

  if [ -x "$NGROK_CMD" ]; then
    "$NGROK_CMD" config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null
    echo "已写入 ngrok authtoken。"
  fi
}

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
chmod +x start-local.sh start-public.sh start-ngrok.sh start-cloudflare.sh 2>/dev/null || true

echo "检查 ngrok ..."
download_ngrok || true
configure_ngrok_token

echo "检查 Python 语法 ..."
.venv/bin/python -m py_compile app.py db.py config.py schemas.py chat_utils.py services.py

echo
echo "安装完成。"
echo "启动服务："
echo "  ./start-local.sh"
echo
echo "访问地址："
echo "  http://127.0.0.1:8000"
echo "  https://kindling-shaft-creamer.ngrok-free.dev  # 需要已配置 ngrok authtoken 和固定域名"
echo
echo "部署后请在 Web 管理界面配置 API 接入商，不要把 API Key 写入公开仓库。"
