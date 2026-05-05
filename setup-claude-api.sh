#!/usr/bin/env bash
set -e

echo "======================================"
echo " Claude Code 第三方 API 环境变量配置"
echo "======================================"
echo

read -rp "请输入第三方 API Base URL，例如 https://api.example.com 或 https://api.example.com/v1 ： " BASE_URL
read -rsp "请输入 API Key： " API_KEY
echo
read -rp "请输入默认模型名，例如 claude-3-5-sonnet-20241022，留空则不设置： " MODEL_NAME
read -rp "请输入小模型名，例如 claude-3-5-haiku-20241022，留空则不设置： " SMALL_MODEL_NAME

if [ -z "$BASE_URL" ]; then
  echo "错误：Base URL 不能为空"
  exit 1
fi

if [ -z "$API_KEY" ]; then
  echo "错误：API Key 不能为空"
  exit 1
fi

SHELL_RC="$HOME/.bashrc"

if [ -n "$ZSH_VERSION" ]; then
  SHELL_RC="$HOME/.zshrc"
elif [ -n "$BASH_VERSION" ]; then
  SHELL_RC="$HOME/.bashrc"
fi

touch "$SHELL_RC"

BACKUP_FILE="${SHELL_RC}.backup.$(date +%Y%m%d_%H%M%S)"
cp "$SHELL_RC" "$BACKUP_FILE"

# 删除旧配置块
sed -i '/# >>> CLAUDE_CODE_THIRD_API >>>/,/# <<< CLAUDE_CODE_THIRD_API <<</d' "$SHELL_RC"

{
  echo
  echo "# >>> CLAUDE_CODE_THIRD_API >>>"
  echo "# Claude Code 第三方 API 配置"
  echo "export ANTHROPIC_BASE_URL=\"$BASE_URL\""
  echo "export ANTHROPIC_API_KEY=\"$API_KEY\""
  echo "export ANTHROPIC_AUTH_TOKEN=\"$API_KEY\""

  if [ -n "$MODEL_NAME" ]; then
    echo "export ANTHROPIC_MODEL=\"$MODEL_NAME\""
  fi

  if [ -n "$SMALL_MODEL_NAME" ]; then
    echo "export ANTHROPIC_SMALL_FAST_MODEL=\"$SMALL_MODEL_NAME\""
  fi

  echo "# <<< CLAUDE_CODE_THIRD_API <<<"
} >> "$SHELL_RC"

# 同步写入 ~/.profile，方便部分 Ubuntu/proot 环境登录 shell 生效
PROFILE_FILE="$HOME/.profile"
touch "$PROFILE_FILE"
cp "$PROFILE_FILE" "${PROFILE_FILE}.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
sed -i '/# >>> CLAUDE_CODE_THIRD_API >>>/,/# <<< CLAUDE_CODE_THIRD_API <<</d' "$PROFILE_FILE"

{
  echo
  echo "# >>> CLAUDE_CODE_THIRD_API >>>"
  echo "# Claude Code 第三方 API 配置"
  echo "export ANTHROPIC_BASE_URL=\"$BASE_URL\""
  echo "export ANTHROPIC_API_KEY=\"$API_KEY\""
  echo "export ANTHROPIC_AUTH_TOKEN=\"$API_KEY\""

  if [ -n "$MODEL_NAME" ]; then
    echo "export ANTHROPIC_MODEL=\"$MODEL_NAME\""
  fi

  if [ -n "$SMALL_MODEL_NAME" ]; then
    echo "export ANTHROPIC_SMALL_FAST_MODEL=\"$SMALL_MODEL_NAME\""
  fi

  echo "# <<< CLAUDE_CODE_THIRD_API <<<"
} >> "$PROFILE_FILE"

echo
echo "配置完成。"
echo "已备份原配置到：$BACKUP_FILE"
echo
echo "请执行下面命令让配置立即生效："
echo
echo "  source \"$SHELL_RC\""
echo
echo "然后测试："
echo
echo "  claude --version"
echo "  claude"
echo
