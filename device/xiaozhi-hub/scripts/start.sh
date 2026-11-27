#!/usr/bin/env bash
# ThinkSmart View native xiaozhi-hub startup entrypoint (invoked by xiaozhi-hub.service)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/main/xiaozhi-server"
PY="$SERVER/.venv/bin/python"

[[ -x "$PY" ]] || { echo "Missing hub virtualenv: $PY" >&2; exit 1; }

if [[ -n "${AUTOSTART_ERR_LOG:-}" && -f "$AUTOSTART_ERR_LOG" ]] && (( $(wc -c <"$AUTOSTART_ERR_LOG") > 1048576 )); then
  : >"$AUTOSTART_ERR_LOG"
fi

cd "$SERVER"
exec "$PY" -B app.py
