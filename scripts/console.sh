#!/usr/bin/env bash
set -euo pipefail
umask 077

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP="${PYTHON:-python3}"
command -v "$BOOTSTRAP" >/dev/null 2>&1 || {
  echo "error: python3 is required" >&2
  exit 1
}
eval "$("$BOOTSTRAP" "$SKILL_ROOT/scripts/skill_env.py" printenv)"
DATA_DIR="$LLM_API_TEST_DATA_DIR"
PID_FILE="$DATA_DIR/console.pid"
LOG_FILE="$DATA_DIR/console.log"
HOST="${WEB_CONSOLE_HOST:-127.0.0.1}"
PORT="${WEB_CONSOLE_PORT:-8090}"
PY="$("$BOOTSTRAP" "$SKILL_ROOT/scripts/skill_env.py" venv-python)"
[[ -x "$PY" ]] || {
  echo "error: managed runtime is not installed; run setup first" >&2
  exit 1
}
export WEB_CONSOLE_HOST="$HOST"
export WEB_CONSOLE_PORT="$PORT"
export PYTHONDONTWRITEBYTECODE=1

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
  echo "error: cloudflared is required for tunnels and was not found" >&2
  echo "install a reviewed version from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" >&2
  return 1
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

set_console_password() {
  local newpw="$1"
  [[ -n "$newpw" ]] || {
    echo "error: password must not be empty" >&2
    return 2
  }
  printf '%s' "$newpw" | (
    cd "$SKILL_ROOT/app"
    "$PY" -c 'import sys; from scripts.web_console import _write_auth_file; password = sys.stdin.read(); _write_auth_file("admin", password)'
  )
  echo "password updated (takes effect immediately for new logins)"
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
    chmod 700 "$DATA_DIR"
    touch "$LOG_FILE"
    chmod 600 "$LOG_FILE"
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
      if [[ -f "$DATA_DIR/console_password" ]]; then
        echo "credentials are stored privately; a human may reveal them locally with:"
        echo "  $SELF passwd --reveal"
      fi
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
    echo "http://${HOST}:${PORT}/"
    ;;
  logs)
    tail -50 "$LOG_FILE"
    ;;
  tunnel)
    TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"
    TUNNEL_TOKEN_FILE=""
    case "${2:-}" in
      "") ;;
      --token-file)
        TUNNEL_TOKEN_FILE="${3:?usage: $SELF tunnel [--token-file <path>]}"
        [[ -r "$TUNNEL_TOKEN_FILE" ]] || {
          echo "error: tunnel token file is not readable: $TUNNEL_TOKEN_FILE" >&2
          exit 2
        }
        "$PY" -c 'import os, stat, sys; s = os.stat(sys.argv[1], follow_symlinks=False); raise SystemExit(0 if stat.S_ISREG(s.st_mode) and not (stat.S_IMODE(s.st_mode) & 0o077) else 1)' "$TUNNEL_TOKEN_FILE" || {
          echo "error: tunnel token must be a regular non-symlink file with mode 0600" >&2
          exit 2
        }
        ;;
      --token)
        echo "error: --token is refused because it exposes the token in process argv" >&2
        echo "use CLOUDFLARE_TUNNEL_TOKEN or --token-file <path>" >&2
        exit 2
        ;;
      *)
        echo "usage: $SELF tunnel [--token-file <path>]" >&2
        exit 2
        ;;
    esac
    if ! alive; then
      echo "error: console is not running; start it first" >&2
      exit 1
    fi
    if tunnel_alive; then
      echo "tunnel already running (pid $(cat "$TUNNEL_PID_FILE"))"
      tunnel_url || echo "(named tunnel: hostname is your configured Cloudflare DNS record)"
      exit 0
    fi
    ensure_cloudflared || exit 1
    touch "$TUNNEL_LOG"
    chmod 600 "$TUNNEL_LOG"
    if [[ -n "$TUNNEL_TOKEN_FILE" || -n "$TUNNEL_TOKEN" ]]; then
      RUNTIME_TOKEN_FILE=""
      if [[ -z "$TUNNEL_TOKEN_FILE" ]]; then
        RUNTIME_TOKEN_FILE="$(mktemp "$DATA_DIR/.cloudflared-token.XXXXXX")"
        chmod 600 "$RUNTIME_TOKEN_FILE"
        printf '%s' "$TUNNEL_TOKEN" >"$RUNTIME_TOKEN_FILE"
        TUNNEL_TOKEN_FILE="$RUNTIME_TOKEN_FILE"
        TUNNEL_TOKEN=""
      fi
      TUNNEL_TOKEN=""
      unset CLOUDFLARE_TUNNEL_TOKEN
      trap '[[ -z "${RUNTIME_TOKEN_FILE:-}" ]] || rm -f -- "$RUNTIME_TOKEN_FILE"' EXIT
      nohup "$CLOUDFLARED" tunnel --protocol http2 --no-autoupdate run \
        --token-file "$TUNNEL_TOKEN_FILE" >"$TUNNEL_LOG" 2>&1 &
      echo $! > "$TUNNEL_PID_FILE"
      sleep 3
      if [[ -n "$RUNTIME_TOKEN_FILE" ]]; then
        rm -f -- "$RUNTIME_TOKEN_FILE"
        RUNTIME_TOKEN_FILE=""
      fi
      trap - EXIT
      if tunnel_alive; then
        echo "named tunnel started (pid $(cat "$TUNNEL_PID_FILE"))"
        echo "public url: your Cloudflare DNS hostname for this tunnel (see dashboard)"
      else
        echo "named tunnel failed; see $TUNNEL_LOG" >&2
        tail -10 "$TUNNEL_LOG" >&2
        exit 1
      fi
      exit 0
    fi
    nohup "$CLOUDFLARED" tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate \
      --protocol http2 >"$TUNNEL_LOG" 2>&1 &
    echo $! > "$TUNNEL_PID_FILE"
    for _ in $(seq 1 20); do
      sleep 1
      url="$(tunnel_url)"
      if [[ -n "$url" ]]; then
        echo "public url: $url"
        echo "(login required; a human may reveal the password locally with: $SELF passwd --reveal)"
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
  passwd)
    case "${2:-}" in
      "")
        echo "password retrieval is not shown by default because agent output may be logged" >&2
        echo "a human may run locally: $SELF passwd --reveal" >&2
        exit 2
        ;;
      --reveal)
        if [[ -n "${WEB_CONSOLE_PASSWORD:-}" ]]; then
          echo "password is injected via WEB_CONSOLE_PASSWORD and will not be printed"
        elif [[ -f "$DATA_DIR/console_password" ]]; then
          echo "user:     admin"
          echo "password: $(cat "$DATA_DIR/console_password")"
        else
          echo "no stored password found (custom auth file in use?)"
          echo "reset one: $SELF passwd --reset    or    $SELF passwd --set"
          exit 1
        fi
        ;;
      --set)
        if [[ $# -gt 2 ]]; then
          echo "error: do not put passwords in process arguments; use '$SELF passwd --set'" >&2
          exit 2
        fi
        read -r -s -p "New password: " newpw
        echo >&2
        read -r -s -p "Confirm password: " confirm
        echo >&2
        [[ "$newpw" == "$confirm" ]] || {
          echo "error: passwords do not match" >&2
          exit 2
        }
        set_console_password "$newpw"
        echo "user:     admin"
        echo "password updated; a human may use '$SELF passwd --reveal' locally"
        ;;
      --password-stdin)
        newpw=""
        IFS= read -r newpw || [[ -n "$newpw" ]]
        set_console_password "$newpw"
        echo "user:     admin"
        echo "password updated"
        ;;
      --reset)
        newpw="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(12))')"
        set_console_password "$newpw"
        echo "user:     admin"
        echo "password reset and stored privately; use --reveal locally if needed"
        ;;
      *)
        echo "usage: $SELF passwd [--reveal|--set|--password-stdin|--reset]" >&2
        exit 2
        ;;
    esac
    ;;
  auth-off)
    echo "note: authentication is optional; to disable it, start the console with"
    echo "  LLM_API_TEST_DISABLE_AUTH=1 bash $SELF start"
    echo "(only do this on trusted networks; it affects the next start)"
    ;;
  *)
    echo "usage: $0 {start|stop|status|url|logs|passwd [--reveal|--set|--password-stdin|--reset]|tunnel [--token-file PATH]|tunnel-stop|tunnel-url|auth-off}" >&2
    exit 2
    ;;
esac
