from __future__ import annotations

import os
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = SKILL_ROOT / "app"


def data_dir() -> Path:
    return Path(
        os.getenv("LLM_API_TEST_DATA_DIR") or (Path.home() / ".config" / "llm-api-test")
    ).expanduser()


def ensure_skill_env() -> Path:
    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LLM_API_TEST_DATA_DIR", str(data))
    os.environ.setdefault("LLM_API_TEST_DOTENV", str(data / ".env"))
    os.environ.setdefault(
        "LLM_API_TEST_PROVIDERS_LOCAL", str(data / "providers.local.yaml")
    )
    os.environ.setdefault(
        "LLM_API_TEST_PROFILES_LOCAL",
        str(data / "model_capability_profiles.local.yaml"),
    )
    os.environ.setdefault("LLM_API_TEST_REPORTS_DIR", str(data / "reports"))
    os.environ.setdefault(
        "LLM_API_TEST_UPSTREAM_CORPUS", str(data / "upstream_fingerprints.json")
    )
    if str(APP_ROOT) not in sys.path:
        sys.path.insert(0, str(APP_ROOT))
    return data


def venv_python() -> Path:
    candidate = SKILL_ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


ENV_KEYS = (
    "LLM_API_TEST_DATA_DIR",
    "LLM_API_TEST_DOTENV",
    "LLM_API_TEST_PROVIDERS_LOCAL",
    "LLM_API_TEST_PROFILES_LOCAL",
    "LLM_API_TEST_REPORTS_DIR",
    "LLM_API_TEST_UPSTREAM_CORPUS",
)


def printenv() -> None:
    import shlex

    ensure_skill_env()
    for key in ENV_KEYS:
        print(f"{key}={shlex.quote(os.environ[key])}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "printenv":
        printenv()
    else:
        print(ensure_skill_env())
