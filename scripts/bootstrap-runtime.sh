#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-${PROJECT_DIR}/requirements.txt}"
BOOTSTRAP_STAMP="${BOOTSTRAP_STAMP:-${VENV_DIR}/.bootstrap-stamp}"
MIN_PYTHON="${MIN_PYTHON:-3.10}"
BOOTSTRAP_AUTO_SYSTEM="${BOOTSTRAP_AUTO_SYSTEM:-1}"
BOOTSTRAP_INSTALL_NGROK="${BOOTSTRAP_INSTALL_NGROK:-0}"
BOOTSTRAP_QUIET="${BOOTSTRAP_QUIET:-0}"
BIN_DIR="${HOME}/.local/bin"
NGROK_BIN="${NGROK_BIN:-${BIN_DIR}/ngrok}"

bootstrap_log() {
  if [ "${BOOTSTRAP_QUIET}" != "1" ]; then
    printf '%s\n' "$*"
  fi
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

package_hint() {
  local manager="${1:-}"
  case "$manager" in
    apt-get) echo "sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip curl tar" ;;
    dnf) echo "sudo dnf install -y python3 python3-pip python3-virtualenv curl tar" ;;
    yum) echo "sudo yum install -y python3 python3-pip curl tar" ;;
    pacman) echo "sudo pacman -Sy --noconfirm python python-pip curl tar" ;;
    apk) echo "sudo apk add python3 py3-pip py3-virtualenv curl tar" ;;
    zypper) echo "sudo zypper install -y python3 python3-pip python3-virtualenv curl tar" ;;
    brew) echo "brew install python curl" ;;
    pkg) echo "pkg install -y python curl tar" ;;
    *) echo "请先安装 Python 3.10+、venv、pip、curl 和 tar" ;;
  esac
}

detect_package_manager() {
  local manager
  for manager in apt-get dnf yum pacman apk zypper brew pkg; do
    if have_cmd "$manager"; then
      printf '%s\n' "$manager"
      return 0
    fi
  done
  return 1
}

can_run_privileged() {
  if [ "$(id -u)" -eq 0 ]; then
    return 0
  fi
  if have_cmd sudo && sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

run_privileged() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

install_system_prereqs() {
  [ "${BOOTSTRAP_AUTO_SYSTEM}" = "1" ] || return 1

  local manager
  manager="$(detect_package_manager || true)"
  [ -n "$manager" ] || return 1
  can_run_privileged || return 1

  bootstrap_log "== 安装系统依赖 (${manager}) =="
  case "$manager" in
    apt-get)
      run_privileged apt-get update
      run_privileged apt-get install -y python3 python3-venv python3-pip curl tar
      ;;
    dnf)
      run_privileged dnf install -y python3 python3-pip python3-virtualenv curl tar
      ;;
    yum)
      run_privileged yum install -y python3 python3-pip curl tar
      ;;
    pacman)
      run_privileged pacman -Sy --noconfirm python python-pip curl tar
      ;;
    apk)
      run_privileged apk add python3 py3-pip py3-virtualenv curl tar
      ;;
    zypper)
      run_privileged zypper --non-interactive install python3 python3-pip python3-virtualenv curl tar
      ;;
    brew)
      brew install python curl
      ;;
    pkg)
      pkg install -y python curl tar
      ;;
    *)
      return 1
      ;;
  esac
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if have_cmd "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

python_version_ok() {
  local py_bin="${1:?}"
  "$py_bin" - <<PY >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= tuple(map(int, "${MIN_PYTHON}".split("."))) else 1)
PY
}

ensure_python_ready() {
  local py_bin
  py_bin="$(find_python || true)"

  if [ -z "$py_bin" ]; then
    install_system_prereqs || true
    py_bin="$(find_python || true)"
  fi

  if [ -z "$py_bin" ]; then
    local manager
    manager="$(detect_package_manager || true)"
    bootstrap_log "错误：未找到可用的 Python。"
    bootstrap_log "可尝试运行：$(package_hint "$manager")"
    return 1
  fi

  if ! python_version_ok "$py_bin"; then
    bootstrap_log "错误：需要 Python ${MIN_PYTHON}+，当前是 $("$py_bin" --version 2>&1)"
    return 1
  fi

  if ! "$py_bin" -m venv --help >/dev/null 2>&1; then
    install_system_prereqs || true
  fi

  if ! "$py_bin" -m venv --help >/dev/null 2>&1; then
    local manager
    manager="$(detect_package_manager || true)"
    bootstrap_log "错误：当前 Python 缺少 venv 模块。"
    bootstrap_log "可尝试运行：$(package_hint "$manager")"
    return 1
  fi

  PYTHON_BIN="$py_bin"
  export PYTHON_BIN
}

venv_python_works() {
  [ -x "${VENV_DIR}/bin/python" ] || return 1
  "${VENV_DIR}/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

venv_pip_works() {
  [ -x "${VENV_DIR}/bin/python" ] || return 1
  "${VENV_DIR}/bin/python" -m pip --version >/dev/null 2>&1
}

ensure_virtualenv() {
  ensure_python_ready

  if ! venv_python_works || ! venv_pip_works; then
    bootstrap_log "== 准备 Python 虚拟环境 =="
    rm -rf "${VENV_DIR}"
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  fi

  VENV_PYTHON="${VENV_DIR}/bin/python"
  export VENV_PYTHON
}

requirements_fingerprint() {
  if [ ! -f "${REQUIREMENTS_FILE}" ]; then
    return 1
  fi
  "${PYTHON_BIN}" - <<PY
from pathlib import Path
import hashlib
data = Path("${REQUIREMENTS_FILE}").read_bytes()
print(hashlib.sha256(data).hexdigest())
PY
}

needs_python_install() {
  [ -f "${REQUIREMENTS_FILE}" ] || return 1
  [ ! -f "${BOOTSTRAP_STAMP}" ] && return 0
  local current wanted
  current="$(cat "${BOOTSTRAP_STAMP}" 2>/dev/null || true)"
  wanted="$(requirements_fingerprint || true)"
  [ -z "$wanted" ] && return 1
  [ "$current" != "$wanted" ]
}

install_python_deps() {
  ensure_virtualenv
  [ -f "${REQUIREMENTS_FILE}" ] || return 0

  if needs_python_install; then
    bootstrap_log "== 安装 Python 依赖 =="
    "${VENV_PYTHON}" -m pip install --upgrade pip setuptools wheel
    "${VENV_PYTHON}" -m pip install -r "${REQUIREMENTS_FILE}"
    requirements_fingerprint > "${BOOTSTRAP_STAMP}"
  fi
}

ensure_runtime_dirs() {
  mkdir -p "${PROJECT_DIR}/uploads" "${PROJECT_DIR}/logs"
}

download_ngrok_if_needed() {
  [ "${BOOTSTRAP_INSTALL_NGROK}" = "1" ] || return 0

  if have_cmd ngrok; then
    bootstrap_log "检测到 ngrok：$(command -v ngrok)"
    return 0
  fi

  if [ -x "${NGROK_BIN}" ]; then
    bootstrap_log "检测到 ngrok：${NGROK_BIN}"
    return 0
  fi

  if ! have_cmd curl || ! have_cmd tar; then
    bootstrap_log "警告：缺少 curl 或 tar，跳过 ngrok 自动安装。"
    return 0
  fi

  local arch url tmp_dir
  arch="$(uname -m)"
  case "$arch" in
    aarch64|arm64) url="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz" ;;
    x86_64|amd64) url="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz" ;;
    armv7l|armhf) url="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm.tgz" ;;
    *)
      bootstrap_log "警告：当前架构 ${arch} 暂不自动安装 ngrok。"
      return 0
      ;;
  esac

  tmp_dir="$(mktemp -d)"
  mkdir -p "${BIN_DIR}"
  bootstrap_log "== 安装 ngrok =="
  if curl -fsSL "$url" | tar -xz -C "$tmp_dir"; then
    mv "${tmp_dir}/ngrok" "${NGROK_BIN}"
    chmod +x "${NGROK_BIN}"
    bootstrap_log "ngrok 已安装到 ${NGROK_BIN}"
  else
    bootstrap_log "警告：ngrok 下载失败，公网隧道可后续手动安装。"
  fi
  rm -rf "$tmp_dir"
}

configure_ngrok_token_if_present() {
  [ -n "${NGROK_AUTHTOKEN:-}" ] || return 0
  local ngrok_cmd=""
  if have_cmd ngrok; then
    ngrok_cmd="$(command -v ngrok)"
  elif [ -x "${NGROK_BIN}" ]; then
    ngrok_cmd="${NGROK_BIN}"
  fi
  [ -n "$ngrok_cmd" ] || return 0
  "$ngrok_cmd" config add-authtoken "${NGROK_AUTHTOKEN}" >/dev/null 2>&1 || true
}

ensure_runtime_ready() {
  install_python_deps
  ensure_runtime_dirs
  download_ngrok_if_needed
  configure_ngrok_token_if_present
}
