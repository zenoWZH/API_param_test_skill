#!/usr/bin/env bash
set -euo pipefail
SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP="${PYTHON:-python3}"
if ! command -v "$BOOTSTRAP" >/dev/null 2>&1; then
  echo "error: python3 is required to locate the managed runtime" >&2
  exit 1
fi
PYTHON_BIN="$($BOOTSTRAP "$SKILL_ROOT/scripts/skill_env.py" venv-python)"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "error: managed runtime is not installed; run scripts/setup.sh first" >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$SKILL_ROOT/packages/model-profile-db:$SKILL_ROOT/app${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" "$@"
