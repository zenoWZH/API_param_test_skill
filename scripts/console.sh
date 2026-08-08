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

eval "$("$PY" "$SKILL_ROOT/scripts/skill_env.py" printenv)"
export WEB_CONSOLE_HOST="$HOST"
export WEB_CONSOLE_PORT="$PORT"

pid_is_console() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  grep -q "web_console.py" "/proc/$pid/cmdline" 2>/dev/null
}

TUNNEL_PID_FILE="$DATA_DIR/tunnel.pid"
TUNNEL_LOG="$DATA_DIR/tunnel.log"
CLOUDFLARED=""

ensure_cloudflared() {
  for candidate in cloudflared "$HOME/.local/bin/cloudflared"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CLOUDFLARED="$candidate"
      return 0
    fi
  done
  local arch
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) echo "error: unsupported arch $(uname -m) for cloudflared" >&2; return 1 ;;
  esac
  mkdir -p "$HOME/.local/bin"
  echo "downloading cloudflared (linux-$arch)..."
  curl -LsSf -o "$HOME/.local/bin/cloudflared" \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
  chmod +x "$HOME/.local/bin/cloudflared"
  CLOUDFLARED="$HOME/.local/bin/cloudflared"
}

tunnel_alive() {
  [[ -f "$TUNNEL_PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$TUNNEL_PID_FILE")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

tunnel_url() {
  grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$TUNNEL_LOG" 2>/dev/null | head -1
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
      grep -A3 "Web console credentials generated" "$LOG_FILE" 2>/dev/null | tail -3 || true
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
  tunnel)
    if ! alive; then
      echo "error: console is not running; start it first" >&2
      exit 1
    fi
    if tunnel_alive; then
      echo "tunnel already running (pid $(cat "$TUNNEL_PID_FILE"))"
      tunnel_url || tail -5 "$TUNNEL_LOG"
      exit 0
    fi
    ensure_cloudflared || exit 1
    nohup "$CLOUDFLARED" tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate \
      --protocol http2 >"$TUNNEL_LOG" 2>&1 &
    echo $! > "$TUNNEL_PID_FILE"
    for _ in $(seq 1 20); do
      sleep 1
      url="$(tunnel_url)"
      if [[ -n "$url" ]]; then
        echo "public url: $url"
        echo "(login required; credentials in $DATA_DIR/console_auth.json or console.log)"
        exit 0
      fi
      tunnel_alive || break
    done
    echo "tunnel failed to establish; see $TUNNEL_LOG" >&2
    tail -10 "$TUNNEL_LOG" >&2
    exit 1
    ;;
  tunnel-stop)
    if tunnel_alive; then
      kill "$(cat "$TUNNEL_PID_FILE")"
      echo "tunnel stopped"
    else
      echo "tunnel not running"
    fi
    rm -f "$TUNNEL_PID_FILE" 2>/dev/null || true
    ;;
  tunnel-url)
    tunnel_url || { echo "no tunnel url; run: $SELF tunnel" >&2; exit 1; }
    ;;
  *)
    echo "usage: $0 {start|stop|status|url|logs|tunnel|tunnel-stop|tunnel-url}" >&2
    exit 2
    ;;
esac
