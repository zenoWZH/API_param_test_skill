#!/usr/bin/env bash
set -euo pipefail
umask 077

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="$SKILL_ROOT/app"
MODEL_PROFILE_PACKAGE_ROOT="$SKILL_ROOT/packages/model-profile-db"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
DATA_DIR="${LLM_API_TEST_DATA_DIR:-$CONFIG_HOME/llm-api-test}"
RUNTIME_DIR="${LLM_API_TEST_RUNTIME_DIR:-$CACHE_HOME/llm-api-test}"
VENV_DIR="$RUNTIME_DIR/venv"
FROM_DIR=""
CHECK_ONLY=0
JSON_OUTPUT=0
OFFLINE=0
WITH_TOKEN_COUNTERS=0

usage() {
  cat <<'EOF'
Usage: setup.sh [--from OLD_DATA_DIR] [--data-dir DIR] [--runtime-dir DIR]
                [--offline] [--with-token-counters] [--check] [--json]

The default install writes only to the data and runtime directories, never to
the skill checkout. --check is read-only and never downloads or installs.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM_DIR="${2:?--from requires a directory}"; shift 2 ;;
    --data-dir) DATA_DIR="${2:?--data-dir requires a directory}"; shift 2 ;;
    --runtime-dir) RUNTIME_DIR="${2:?--runtime-dir requires a directory}"; VENV_DIR="$RUNTIME_DIR/venv"; shift 2 ;;
    --check) CHECK_ONLY=1; shift ;;
    --json) JSON_OUTPUT=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    --with-token-counters) WITH_TOKEN_COUNTERS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

UV=""
for candidate in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if command -v "$candidate" >/dev/null 2>&1; then
    UV="$(command -v "$candidate")"
    break
  fi
done

if [[ "$CHECK_ONLY" == 1 ]]; then
  ready=true
  [[ -n "$UV" ]] || ready=false
  [[ -x "$VENV_DIR/bin/python" ]] || ready=false
  [[ -f "$MODEL_PROFILE_PACKAGE_ROOT/pyproject.toml" ]] || ready=false
  if [[ "$JSON_OUTPUT" == 1 ]]; then
    DATA_DIR="$DATA_DIR" RUNTIME_DIR="$RUNTIME_DIR" UV="$UV" VENV_PYTHON="$VENV_DIR/bin/python" READY="$ready" \
      python3 -c 'import json,os; print(json.dumps({"schema":"llm-api-test.setup-check.v1","ready":os.environ["READY"] == "true","uv":os.environ["UV"] or None,"venv_python":os.environ["VENV_PYTHON"] if os.path.isfile(os.environ["VENV_PYTHON"]) else None,"data_dir":os.environ["DATA_DIR"],"runtime_dir":os.environ["RUNTIME_DIR"]}, ensure_ascii=False))'
  else
    echo "ready=$ready"
    echo "uv=${UV:-missing}"
    echo "venv_python=$VENV_DIR/bin/python"
    echo "data_dir=$DATA_DIR"
    echo "runtime_dir=$RUNTIME_DIR"
  fi
  [[ "$ready" == true ]]
  exit
fi

if [[ -z "$UV" ]]; then
  echo "error: uv is required and was not found." >&2
  echo "Install it using the verified method for your platform, then rerun setup." >&2
  echo "Official instructions: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi
if [[ ! -f "$MODEL_PROFILE_PACKAGE_ROOT/pyproject.toml" ]]; then
  echo "error: missing bundled model-profile package: $MODEL_PROFILE_PACKAGE_ROOT" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR" "$DATA_DIR/reports/jobs" "$DATA_DIR/workflows"
chmod 700 "$RUNTIME_DIR" "$DATA_DIR" "$DATA_DIR/reports" "$DATA_DIR/reports/jobs" "$DATA_DIR/workflows"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$RUNTIME_DIR/uv-cache}"
mkdir -p "$UV_CACHE_DIR"
chmod 700 "$UV_CACHE_DIR"
if [[ "$OFFLINE" == 1 ]]; then
  export UV_OFFLINE=1
  export UV_PYTHON_DOWNLOADS=never
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "creating uv-managed Python 3.12 environment in $VENV_DIR..."
  "$UV" venv --managed-python --python 3.12 "$VENV_DIR"
fi

# Install the locked runtime dependencies. The standalone skill consumes the
# bundled checkout package directly through PYTHONPATH, so building it can
# never create setuptools artifacts in a read-only skill directory.
"$UV" pip install \
  --python "$VENV_DIR/bin/python" \
  --quiet \
  -r "$APP_ROOT/requirements.lock"
if [[ "$WITH_TOKEN_COUNTERS" == 1 ]]; then
  "$UV" pip install \
    --python "$VENV_DIR/bin/python" \
    --quiet \
    -r "$APP_ROOT/requirements-token-audit.txt"
fi

# Migrate existing private state before creating defaults.
if [[ -n "$FROM_DIR" ]]; then
  if [[ -f "$FROM_DIR/.env" && ! -s "$DATA_DIR/.env" ]]; then
    cp "$FROM_DIR/.env" "$DATA_DIR/.env"
    echo "copied .env from the previous data directory"
  fi
  if [[ -f "$FROM_DIR/providers.local.yaml" && ! -f "$DATA_DIR/providers.local.yaml" ]]; then
    cp "$FROM_DIR/providers.local.yaml" "$DATA_DIR/providers.local.yaml"
    echo "copied providers.local.yaml from the previous data directory"
  fi
  old_corpus="$FROM_DIR/upstream_fingerprints.json"
  [[ -f "$old_corpus" ]] || old_corpus="$FROM_DIR/fixtures/upstream_fingerprints.json"
  if [[ -f "$old_corpus" ]]; then
    entries=$("$VENV_DIR/bin/python" -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("entries", [])))' "$old_corpus" 2>/dev/null || echo 0)
    if [[ "$entries" != "0" ]]; then
      cp "$old_corpus" "$DATA_DIR/upstream_fingerprints.json"
      echo "copied upstream fingerprint corpus ($entries entries)"
    fi
  fi
fi

touch "$DATA_DIR/.env"
if [[ ! -f "$DATA_DIR/providers.local.yaml" ]]; then
  cp "$APP_ROOT/providers.local.example.yaml" "$DATA_DIR/providers.local.yaml"
  echo "created providers.local.yaml from the bundled template"
fi
if [[ ! -s "$DATA_DIR/upstream_fingerprints.json" ]]; then
  printf '%s\n' '{"schema_version": 1, "entries": []}' > "$DATA_DIR/upstream_fingerprints.json"
fi
chmod 600 "$DATA_DIR/.env" "$DATA_DIR/providers.local.yaml" "$DATA_DIR/upstream_fingerprints.json"

export LLM_API_TEST_DATA_DIR="$DATA_DIR"
export LLM_API_TEST_RUNTIME_DIR="$RUNTIME_DIR"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$MODEL_PROFILE_PACKAGE_ROOT:$APP_ROOT${PYTHONPATH:+:$PYTHONPATH}"
"$UV" pip check --python "$VENV_DIR/bin/python"
"$VENV_DIR/bin/python" -m model_profile_db.cli verify-artifacts >/dev/null
"$VENV_DIR/bin/python" "$SKILL_ROOT/scripts/doctor.py" --json --strict >/dev/null

if [[ "$JSON_OUTPUT" == 1 ]]; then
  "$VENV_DIR/bin/python" "$SKILL_ROOT/scripts/doctor.py" --json
else
  echo "setup complete"
  echo "  skill:   $SKILL_ROOT"
  echo "  data:    $DATA_DIR"
  echo "  runtime: $RUNTIME_DIR"
  echo "  next: edit $DATA_DIR/.env and providers.local.yaml, then run doctor"
fi
