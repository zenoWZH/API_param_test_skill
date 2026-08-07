#!/usr/bin/env bash
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${LLM_API_TEST_DATA_DIR:-$HOME/.config/llm-api-test}"
PID_FILE="$DATA_DIR/console.pid"
LOG_FILE="$DATA_DIR/console.log"
HOST="${WEB_CONSOLE_HOST:-0.0.0.0}"
PORT="${WEB_CONSOLE_PORT:-8090}"
PY="$SKILL_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

while IFS= read -r line; do export "$line"; done < <("$PY" "$SKILL_ROOT/scripts/skill_env.py" printenv)
export WEB_CONSOLE_HOST="$HOST"
export WEB_CONSOLE_PORT="$PORT"

pid_is_console() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  grep -q "web_console.py" "/proc/$pid/cmdline" 2>/dev/null
}

alive() {
  [[ -f "$PID_FILE" ]] || return 1
  pid_is_console "$(cat "$PID_FILE")"
}

case "${1:-status}" in
  start)
    if alive; then
      echo "console already running (pid $(cat "$PID_FILE"))"
      "$SELF" url
      exit 0
    fi
    mkdir -p "$DATA_DIR"
    if (echo >/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
      echo "error: port $PORT is already in use; stop that process or set WEB_CONSOLE_PORT" >&2
      exit 1
    fi
    cd "$SKILL_ROOT/app"
    nohup "$PY" scripts/web_console.py >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if alive; then
      echo "console started (pid $(cat "$PID_FILE"))"
      "$SELF" url
    else
      echo "console failed to start; see $LOG_FILE" >&2
      tail -20 "$LOG_FILE" >&2
      exit 1
    fi
    ;;
  stop)
    if alive; then
      kill "$(cat "$PID_FILE")"
      echo "console stopped"
    else
      echo "console not running"
    fi
    rm -f "$PID_FILE" 2>/dev/null || true
    ;;
  status)
    if alive; then
      echo "running (pid $(cat "$PID_FILE"))"
      "$SELF" url
      exit 0
    fi
    echo "not running"
    exit 1
    ;;
  url)
    echo "http://${HOST}:${PORT}/  (if host is 0.0.0.0, use http://127.0.0.1:${PORT}/ locally)"
    ;;
  logs)
    tail -50 "$LOG_FILE"
    ;;
  *)
    echo "usage: $0 {start|stop|status|url|logs}" >&2
    exit 2
    ;;
esac
