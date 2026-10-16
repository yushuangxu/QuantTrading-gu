#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

PID_FILE="runtime/quant_alert.pid"
LOG_META_FILE="runtime/current_logs.env"
LOG_FILE="logs/quant_alert.log"
ERR_LOG_FILE="logs/quant_alert.err.log"

if [[ -f "$LOG_META_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$LOG_META_FILE"
fi

if [[ ! -f "$PID_FILE" ]]; then
  echo "quant_alert is not running. (PID file not found)"
  if [[ -f "$LOG_FILE" ]]; then
    echo
    echo "===== Last 20 log lines ====="
    tail -n 20 "$LOG_FILE"
  fi
  exit 0
fi

PID="$(cat "$PID_FILE" || true)"
if [[ -z "${PID}" ]]; then
  echo "quant_alert status unknown. PID file is empty."
  exit 1
fi

if kill -0 "$PID" 2>/dev/null; then
  echo "quant_alert is running. PID=$PID"
else
  echo "quant_alert is not running. stale PID=$PID"
fi

if [[ -f "$LOG_FILE" ]]; then
  echo
  echo "===== Last 20 log lines ====="
  tail -n 20 "$LOG_FILE"
else
  echo "log file not found: $LOG_FILE"
fi

if [[ -f "$ERR_LOG_FILE" ]]; then
  echo
  echo "===== Last 20 error log lines ====="
  tail -n 20 "$ERR_LOG_FILE"
fi
