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
if [[ -z "$PYBIN" ]]; then
  echo "error: Python 3.11+ is required (datetime.UTC is used by image tests)." >&2
  exit 1
fi
echo "using $PYBIN ($version)"

if [[ ! -x "$SKILL_ROOT/.venv/bin/python" ]]; then
  "$PYBIN" -m venv "$SKILL_ROOT/.venv"
fi
"$SKILL_ROOT/.venv/bin/pip" install --quiet --upgrade pip
"$SKILL_ROOT/.venv/bin/pip" install --quiet -r "$APP_ROOT/requirements.txt" pytest

mkdir -p "$DATA_DIR/reports/jobs" "$DATA_DIR/workflows"
touch "$DATA_DIR/.env" "$DATA_DIR/upstream_fingerprints.json"
if [[ ! -s "$DATA_DIR/upstream_fingerprints.json" ]]; then
  echo '{"schema_version": 1, "entries": []}' > "$DATA_DIR/upstream_fingerprints.json"
fi
chmod 600 "$DATA_DIR/.env"

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
echo "  next:   bash $SKILL_ROOT/scripts/console.sh start"
