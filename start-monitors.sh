#!/usr/bin/env bash
set -euo pipefail

TERMUX_JOB_SCHEDULER="${TERMUX_JOB_SCHEDULER:-termux-job-scheduler}"
TERMUX_HOME="${TERMUX_HOME:-/data/data/com.termux/files/home}"

KEEPALIVE_SCRIPT="${TERMUX_HOME}/keep-termux-alive.sh"
LOCAL_CHECK_SCRIPT="${TERMUX_HOME}/check-claude-web.sh"
PUBLIC_CHECK_SCRIPT="${TERMUX_HOME}/check-claude-web-public.sh"

schedule_job() {
  local job_id="$1"
  local script_path="$2"

  if [ ! -x "$script_path" ]; then
    echo "warning: monitor script missing: $script_path"
    return 0
  fi

  "$TERMUX_JOB_SCHEDULER" \
    --job-id "$job_id" \
    --period-ms 900000 \
    --network any \
    --persisted true \
    --script "$script_path" >/dev/null
}

if ! command -v "$TERMUX_JOB_SCHEDULER" >/dev/null 2>&1; then
  echo "warning: termux-job-scheduler not found; skip monitor registration"
  exit 0
fi

schedule_job 9001 "$KEEPALIVE_SCRIPT"
schedule_job 9002 "$LOCAL_CHECK_SCRIPT"
schedule_job 9003 "$PUBLIC_CHECK_SCRIPT"

echo "monitors: enabled"
