#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

PID_DIR="runtime"
LOG_DIR="logs"
PID_FILE="$PID_DIR/quant_alert.pid"
LOG_META_FILE="$PID_DIR/current_logs.env"
TODAY="$(date +%F)"
LOG_FILE="$LOG_DIR/${TODAY}_quant_alert.log"
ERR_LOG_FILE="$LOG_DIR/${TODAY}_quant_alert.err.log"

mkdir -p "$PID_DIR" "$LOG_DIR"

if [[ -f "$PID_FILE" ]]; then
  EXIST_PID="$(cat "$PID_FILE" || true)"
  if [[ -n "${EXIST_PID}" ]] && kill -0 "$EXIST_PID" 2>/dev/null; then
    echo "quant_alert is already running with PID $EXIST_PID"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# Use venv Python if available, otherwise fall back to system python3
if [[ -x "./venv/bin/python" ]]; then
  PY="./venv/bin/python"
else
  PY="python3"
fi

nohup "$PY" main.py >>"$LOG_FILE" 2>>"$ERR_LOG_FILE" &
NEW_PID=$!
echo "$NEW_PID" >"$PID_FILE"
cat >"$LOG_META_FILE" <<EOF
LOG_FILE=$LOG_FILE
ERR_LOG_FILE=$ERR_LOG_FILE
EOF

echo "quant_alert started. PID=$NEW_PID"
echo "log file: $LOG_FILE"
echo "err log file: $ERR_LOG_FILE"
