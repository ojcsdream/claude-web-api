#!/usr/bin/env bash
set -e

CODEX_DIR="$HOME/.codex"
CONFIG_FILE="$CODEX_DIR/config.toml"
ENV_FILE="$CODEX_DIR/codex.env"
BASHRC="$HOME/.bashrc"
BIN_DIR="$HOME/bin"
LAUNCHER="$BIN_DIR/cdx"

mkdir -p "$CODEX_DIR" "$BIN_DIR"

echo "======================================"
echo " Codex API 一键清除并重新导入"
echo "======================================"
echo

read -rp "请输入 API Base URL，例如 https://api.xxx.com/v1 ： " BASE_URL
read -rsp "请输入 API Key： " API_KEY
echo
read -rp "请输入模型名，例如 gpt-5.5 ： " MODEL
read -rp "请输入推理强度 low/medium/high/xhigh [默认 medium]： " REASONING

if [ -z "$BASE_URL" ]; then
  echo "错误：API Base URL 不能为空"
  exit 1
fi

if [ -z "$API_KEY" ]; then
  echo "错误：API Key 不能为空"
  exit 1
fi

if [ -z "$MODEL" ]; then
  MODEL="gpt-5.5"
fi

if [ -z "$REASONING" ]; then
  REASONING="medium"
fi

echo
echo "正在清除当前 shell 环境变量..."

unset OPENAI_API_KEY || true
unset OPENAI_BASE_URL || true
unset OPENAI_API_BASE || true
unset OPENAI_ORG_ID || true
unset OPENAI_PROJECT_ID || true
unset CODEX_MODEL || true

echo "正在清理 ~/.bashrc 旧配置..."

touch "$BASHRC"

sed -i '/# >>> CODEX_API_CONFIG >>>/,/# <<< CODEX_API_CONFIG <<</d' "$BASHRC" 2>/dev/null || true
sed -i '/# >>> CODEX_OPENAI_API >>>/,/# <<< CODEX_OPENAI_API <<</d' "$BASHRC" 2>/dev/null || true
sed -i '/^[[:space:]]*export[[:space:]]\+OPENAI_API_KEY=/d' "$BASHRC" 2>/dev/null || true
sed -i '/^[[:space:]]*export[[:space:]]\+OPENAI_BASE_URL=/d' "$BASHRC" 2>/dev/null || true
sed -i '/^[[:space:]]*export[[:space:]]\+OPENAI_API_BASE=/d' "$BASHRC" 2>/dev/null || true
sed -i '/^[[:space:]]*export[[:space:]]\+CODEX_MODEL=/d' "$BASHRC" 2>/dev/null || true

echo "正在写入独立环境变量文件：$ENV_FILE"

cat > "$ENV_FILE" <<EOF2
export OPENAI_API_KEY="$API_KEY"
export OPENAI_BASE_URL="$BASE_URL"
export OPENAI_API_BASE="$BASE_URL"
export CODEX_MODEL="$MODEL"
EOF2

chmod 600 "$ENV_FILE"

cat >> "$BASHRC" <<EOF2

# >>> CODEX_API_CONFIG >>>
# Codex API 配置
if [ -f "\$HOME/.codex/codex.env" ]; then
  source "\$HOME/.codex/codex.env"
fi
# <<< CODEX_API_CONFIG <<<
EOF2

echo "正在备份并重写 Codex 配置文件..."

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

echo "正在创建快捷启动命令：cdx"

cat > "$LAUNCHER" <<'EOF2'
#!/usr/bin/env bash
set -e

ENV_FILE="$HOME/.codex/codex.env"

unset OPENAI_API_KEY || true
unset OPENAI_BASE_URL || true
unset OPENAI_API_BASE || true
unset OPENAI_ORG_ID || true
unset OPENAI_PROJECT_ID || true
unset CODEX_MODEL || true

if [ ! -f "$ENV_FILE" ]; then
  echo "错误：找不到 $ENV_FILE"
  echo "请重新运行 reset-codex-api.sh"
  exit 1
fi

source "$ENV_FILE"

if [ -z "$OPENAI_API_KEY" ]; then
  echo "错误：OPENAI_API_KEY 为空"
  exit 1
fi

if [ -z "$OPENAI_BASE_URL" ]; then
  echo "错误：OPENAI_BASE_URL 为空"
  exit 1
fi

export OPENAI_API_KEY
export OPENAI_BASE_URL
export OPENAI_API_BASE
export CODEX_MODEL

echo "======================================"
echo " Codex 启动信息"
echo "======================================"
echo "Base URL : $OPENAI_BASE_URL"
echo "API Key  : ${OPENAI_API_KEY:0:8}********"
echo "Model    : ${CODEX_MODEL:-未设置}"
echo "======================================"
echo

CODEX_PERMISSIONS=(
  --dangerously-bypass-approvals-and-sandbox
)

if [ -n "$CODEX_MODEL" ]; then
  exec codex "${CODEX_PERMISSIONS[@]}" -m "$CODEX_MODEL" "$@"
else
  exec codex "${CODEX_PERMISSIONS[@]}" "$@"
fi
EOF2

chmod +x "$LAUNCHER"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo 'export PATH="$HOME/bin:$PATH"' >> "$BASHRC"
    export PATH="$BIN_DIR:$PATH"
    ;;
esac

export OPENAI_API_KEY="$API_KEY"
export OPENAI_BASE_URL="$BASE_URL"
export OPENAI_API_BASE="$BASE_URL"
export CODEX_MODEL="$MODEL"

echo
echo "======================================"
echo " 配置完成"
echo "======================================"
echo
echo "Base URL : $BASE_URL"
echo "API Key  : ${API_KEY:0:8}********"
echo "Model    : $MODEL"
echo "Reasoning: $REASONING"
echo
echo "现在执行："
echo
echo "  source ~/.bashrc"
echo "  cdx"
echo
