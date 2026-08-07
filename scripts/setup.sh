#!/usr/bin/env bash
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="$SKILL_ROOT/app"
DATA_DIR="${LLM_API_TEST_DATA_DIR:-$HOME/.config/llm-api-test}"
FROM_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM_DIR="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PYBIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    major="${version%%.*}"; minor="${version##*.}"
    if [[ "$major" == "3" && "$minor" -ge 11 ]]; then
      PYBIN="$candidate"
      break
    fi
  fi
done

UV=""
for candidate in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if command -v "$candidate" >/dev/null 2>&1; then
    UV="$candidate"
    break
  fi
done
if [[ -z "$UV" ]]; then
  echo "uv not found; installing standalone uv..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$HOME/.local/bin/uv"
  else
    echo "error: uv and curl are both missing." >&2
    echo "  install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  fi
fi
if [[ ! -x "$UV" ]]; then
  resolved="$(command -v "$UV" 2>/dev/null || true)"
  [[ -n "$resolved" ]] && UV="$resolved"
fi
if [[ ! -x "$UV" ]]; then
  echo "error: uv installation did not produce an executable (got: $UV)" >&2
  exit 1
fi
echo "using $UV ($("$UV" --version))"

if [[ ! -x "$SKILL_ROOT/.venv/bin/python" ]]; then
  if [[ -n "$PYBIN" ]]; then
    echo "using system $PYBIN ($version)"
    if ! "$UV" venv --python "$PYBIN" "$SKILL_ROOT/.venv"; then
      rm -rf "$SKILL_ROOT/.venv" 2>/dev/null || true
      echo "error: uv failed to create virtualenv from $PYBIN" >&2
      exit 1
    fi
  else
    echo "no system Python 3.11+; uv will fetch a managed Python 3.12"
    if ! "$UV" venv --python 3.12 "$SKILL_ROOT/.venv"; then
      rm -rf "$SKILL_ROOT/.venv" 2>/dev/null || true
      echo "error: uv failed to fetch/create Python 3.12 virtualenv" >&2
      exit 1
    fi
  fi
fi
"$UV" pip install --python "$SKILL_ROOT/.venv/bin/python" --quiet -r "$APP_ROOT/requirements.txt" pytest

mkdir -p "$DATA_DIR/reports/jobs" "$DATA_DIR/workflows"
touch "$DATA_DIR/.env" "$DATA_DIR/upstream_fingerprints.json"
if [[ ! -s "$DATA_DIR/upstream_fingerprints.json" ]]; then
  echo '{"schema_version": 1, "entries": []}' > "$DATA_DIR/upstream_fingerprints.json"
fi
if [[ ! -f "$DATA_DIR/providers.local.yaml" ]]; then
  cp "$APP_ROOT/providers.local.example.yaml" "$DATA_DIR/providers.local.yaml"
  echo "created $DATA_DIR/providers.local.yaml from example template"
fi
chmod 600 "$DATA_DIR/.env" "$DATA_DIR/providers.local.yaml"

if [[ -n "$FROM_DIR" ]]; then
  if [[ -f "$FROM_DIR/.env" && ! -s "$DATA_DIR/.env" ]]; then
    cp "$FROM_DIR/.env" "$DATA_DIR/.env"
    chmod 600 "$DATA_DIR/.env"
    echo "copied .env from $FROM_DIR"
  fi
  if [[ -f "$FROM_DIR/providers.local.yaml" && ! -f "$DATA_DIR/providers.local.yaml" ]]; then
    cp "$FROM_DIR/providers.local.yaml" "$DATA_DIR/providers.local.yaml"
    chmod 600 "$DATA_DIR/providers.local.yaml"
    echo "copied providers.local.yaml from $FROM_DIR"
  fi
  if [[ -f "$FROM_DIR/fixtures/upstream_fingerprints.json" ]]; then
    entries=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("entries", [])))' "$FROM_DIR/fixtures/upstream_fingerprints.json" 2>/dev/null || echo 0)
    if [[ "$entries" != "0" ]]; then
      cp "$FROM_DIR/fixtures/upstream_fingerprints.json" "$DATA_DIR/upstream_fingerprints.json"
      echo "copied upstream fingerprint corpus ($entries entries)"
    fi
  fi
fi

echo "setup complete"
echo "  skill:  $SKILL_ROOT"
echo "  data:   $DATA_DIR"
echo "  next:   1) edit $DATA_DIR/.env and providers.local.yaml (add API keys/providers)"
echo "          2) bash $SKILL_ROOT/scripts/console.sh start"
