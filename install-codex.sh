#!/usr/bin/env bash
set -e

echo "======================================"
echo " OpenAI Codex CLI 安装脚本"
echo "======================================"

if ! command -v curl >/dev/null 2>&1; then
  apt update
  apt install -y curl
fi

if ! command -v git >/dev/null 2>&1; then
  apt update
  apt install -y git
fi

if ! command -v node >/dev/null 2>&1; then
  echo "未检测到 Node.js，正在安装 Node.js LTS..."

  if [ ! -d "$HOME/.nvm" ]; then
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
  fi

  export NVM_DIR="$HOME/.nvm"
  [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

  nvm install --lts
  nvm use --lts
  nvm alias default node
else
  echo "已检测到 Node.js：$(node -v)"
fi

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

echo
echo "正在安装 Codex CLI..."
npm install -g @openai/codex

echo
echo "======================================"
echo " 安装完成"
echo "======================================"
echo
echo "请执行："
echo
echo "  codex --version"
echo
echo "如果能看到版本号，就安装成功。"
echo
