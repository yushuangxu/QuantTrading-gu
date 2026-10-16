#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

PID_FILE="runtime/quant_alert.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "PID file not found. quant_alert may not be running."
  exit 0
fi

PID="$(cat "$PID_FILE" || true)"
if [[ -z "${PID}" ]]; then
  echo "PID file is empty, cleaning it."
  rm -f "$PID_FILE"
  exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
  echo "Process $PID not found, cleaning stale PID file."
  rm -f "$PID_FILE"
  exit 0
fi

kill "$PID" 2>/dev/null || true
sleep 1

if kill -0 "$PID" 2>/dev/null; then
  kill -9 "$PID" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "quant_alert stopped. PID=$PID"
