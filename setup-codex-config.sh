#!/usr/bin/env bash
set -e

CONFIG_FILE="${CONFIG_FILE:-$HOME/.codex/config.toml}"
BASHRC="$HOME/.bashrc"

echo "======================================"
echo " Codex 配置一键替换"
echo "======================================"
echo

read -rp "请输入配置文件路径 [默认: $CONFIG_FILE]： " INPUT_CONFIG
if [ -n "$INPUT_CONFIG" ]; then
  CONFIG_FILE="$INPUT_CONFIG"
fi

read -rp "请输入 Base URL，例如 https://ai.klinkw.com ： " BASE_URL
read -rp "请输入模型名，例如 gpt-5.4 ： " MODEL
read -rp "请输入 review_model，例如 gpt-5.4 ： " REVIEW_MODEL
read -rp "请输入 API Key： " -s API_KEY
echo

if [ -z "$BASE_URL" ] || [ -z "$MODEL" ] || [ -z "$REVIEW_MODEL" ] || [ -z "$API_KEY" ]; then
  echo "错误：Base URL、模型名、review_model、API Key 都不能为空。"
  exit 1
fi

mkdir -p "$(dirname "$CONFIG_FILE")"

if [ -f "$CONFIG_FILE" ]; then
  cp "$CONFIG_FILE" "${CONFIG_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
  echo "已备份旧配置。"
fi

cat > "$CONFIG_FILE" <<EOF2
model_provider = "OpenAI"
model = "$MODEL"
review_model = "$REVIEW_MODEL"
model_reasoning_effort = "xhigh"
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

# 清理旧的 OPENAI_API_KEY 配置块
touch "$BASHRC"
sed -i '/# >>> CODEX_OPENAI_API >>>/,/# <<< CODEX_OPENAI_API <<</d' "$BASHRC" 2>/dev/null || true

cat >> "$BASHRC" <<EOF2

# >>> CODEX_OPENAI_API >>>
export OPENAI_API_KEY="$API_KEY"
# <<< CODEX_OPENAI_API <<<
EOF2

export OPENAI_API_KEY="$API_KEY"

echo
echo "配置完成。"
echo "配置文件：$CONFIG_FILE"
echo
echo "请执行下面命令让 shell 立即生效："
echo
echo "  source ~/.bashrc"
echo
echo "然后启动："
echo
echo "  codex"
echo
