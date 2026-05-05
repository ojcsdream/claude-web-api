#!/usr/bin/env bash
set -e

ENV_FILE="$HOME/.codex/codex.env"
CONFIG_FILE="$HOME/.codex/config.toml"
BASHRC="$HOME/.bashrc"

mkdir -p "$HOME/.codex"

echo "======================================"
echo " Codex API Key / URL 修复脚本"
echo "======================================"
echo

read -rp "请输入 API Base URL [默认 https://api.codemax.store/v1]： " BASE_URL
read -rsp "请输入新的 API Key： " API_KEY
echo
read -rp "请输入模型名 [默认 gpt-5.5]： " MODEL
read -rp "请输入推理强度 low/medium/high/xhigh [默认 medium]： " REASONING

BASE_URL="${BASE_URL:-https://api.codemax.store/v1}"
MODEL="${MODEL:-gpt-5.5}"
REASONING="${REASONING:-medium}"

# 清理复制时可能带入的空格、回车、换行
BASE_URL="$(printf '%s' "$BASE_URL" | tr -d '\r\n ' )"
API_KEY="$(printf '%s' "$API_KEY" | tr -d '\r\n ' )"
MODEL="$(printf '%s' "$MODEL" | tr -d '\r\n ' )"
REASONING="$(printf '%s' "$REASONING" | tr -d '\r\n ' )"

if [ -z "$API_KEY" ]; then
  echo "错误：API Key 不能为空"
  exit 1
fi

# 去掉 URL 末尾多余 /
BASE_URL="${BASE_URL%/}"

# 如果用户填的是 https://api.codemax.store，自动补 /v1
case "$BASE_URL" in
  */v1) ;;
  *) BASE_URL="$BASE_URL/v1" ;;
esac

echo
echo "正在清除旧环境变量..."

unset OPENAI_API_KEY || true
unset OPENAI_BASE_URL || true
unset OPENAI_API_BASE || true
unset CODEX_MODEL || true

echo "正在写入 $ENV_FILE"

cat > "$ENV_FILE" <<EOF2
export OPENAI_API_KEY="$API_KEY"
export OPENAI_BASE_URL="$BASE_URL"
export OPENAI_API_BASE="$BASE_URL"
export CODEX_MODEL="$MODEL"
EOF2

chmod 600 "$ENV_FILE"

echo "正在重写 Codex 配置 $CONFIG_FILE"

if [ -f "$CONFIG_FILE" ]; then
  cp "$CONFIG_FILE" "${CONFIG_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
fi

cat > "$CONFIG_FILE" <<EOF2
model_provider = "OpenAI"
model = "$MODEL"
review_model = "$MODEL"
model_reasoning_effort = "$REASONING"
disable_response_storage = true
network_access = "enabled"
sandbox_mode = "danger-full-access"
approval_policy = "never"
windows_wsl_setup_acknowledged = true
model_context_window = 1000000
model_auto_compact_token_limit = 900000

[model_providers.OpenAI]
name = "OpenAI"
base_url = "$BASE_URL"
wire_api = "responses"
requires_openai_auth = true
EOF2

touch "$BASHRC"

sed -i '/# >>> CODEX_API_CONFIG >>>/,/# <<< CODEX_API_CONFIG <<</d' "$BASHRC" 2>/dev/null || true
sed -i '/^[[:space:]]*export[[:space:]]\+OPENAI_API_KEY=/d' "$BASHRC" 2>/dev/null || true
sed -i '/^[[:space:]]*export[[:space:]]\+OPENAI_BASE_URL=/d' "$BASHRC" 2>/dev/null || true
sed -i '/^[[:space:]]*export[[:space:]]\+OPENAI_API_BASE=/d' "$BASHRC" 2>/dev/null || true
sed -i '/^[[:space:]]*export[[:space:]]\+CODEX_MODEL=/d' "$BASHRC" 2>/dev/null || true

cat >> "$BASHRC" <<'EOF2'

# >>> CODEX_API_CONFIG >>>
if [ -f "$HOME/.codex/codex.env" ]; then
  source "$HOME/.codex/codex.env"
fi
# <<< CODEX_API_CONFIG <<<
EOF2

export OPENAI_API_KEY="$API_KEY"
export OPENAI_BASE_URL="$BASE_URL"
export OPENAI_API_BASE="$BASE_URL"
export CODEX_MODEL="$MODEL"

echo
echo "======================================"
echo " 配置完成"
echo "======================================"
echo "Base URL : $BASE_URL"
echo "API Key  : ${API_KEY:0:10}********"
echo "Model    : $MODEL"
echo "Reasoning: $REASONING"
echo
echo "请执行："
echo
echo "  source ~/.bashrc"
echo "  cdx"
echo
