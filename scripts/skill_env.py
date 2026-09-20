from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = SKILL_ROOT / "app"
JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")


def data_dir() -> Path:
    configured = os.getenv("LLM_API_TEST_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    config_home = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_home / "llm-api-test"


def runtime_dir() -> Path:
    configured = os.getenv("LLM_API_TEST_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser()
    cache_home = Path(os.getenv("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return cache_home / "llm-api-test"


def configure_skill_env() -> Path:
    """Configure paths and imports without touching the filesystem."""

    data = data_dir()
    runtime = runtime_dir()
    os.environ.setdefault("LLM_API_TEST_DATA_DIR", str(data))
    os.environ.setdefault("LLM_API_TEST_RUNTIME_DIR", str(runtime))
    os.environ.setdefault("LLM_API_TEST_DOTENV", str(data / ".env"))
    os.environ.setdefault(
        "LLM_API_TEST_PROVIDERS_LOCAL", str(data / "providers.local.yaml")
    )
    os.environ.setdefault("LLM_API_TEST_REPORTS_DIR", str(data / "reports"))
    os.environ.setdefault(
        "LLM_API_TEST_UPSTREAM_CORPUS", str(data / "upstream_fingerprints.json")
    )
    if str(APP_ROOT) not in sys.path:
        sys.path.insert(0, str(APP_ROOT))
    return data


def ensure_skill_env() -> Path:
    os.umask(0o077)
    data = configure_skill_env()
    runtime = runtime_dir()
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    data.chmod(0o700)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    return data


def resolve_job_dir(jobs_root: Path, job_id: str) -> Path:
    """Resolve one generated job id without allowing path or symlink escapes."""
    value = str(job_id or "").strip()
    if not JOB_ID_PATTERN.fullmatch(value):
        raise ValueError("job_id must be a single generated identifier")
    root = jobs_root.resolve()
    raw_candidate = root / value
    if raw_candidate.is_symlink():
        raise ValueError("job_id must not resolve through a symlink")
    candidate = raw_candidate.resolve()
    if candidate.parent != root:
        raise ValueError("job_id resolves outside the jobs directory")
    return candidate


def _fsync_parent(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_private_text(path: Path, text: str) -> None:
    """Flush and atomically replace a private UTF-8 text file."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
        _fsync_parent(path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def atomic_backup_private(source: Path, backup: Path) -> None:
    source = Path(source)
    atomic_write_private_text(backup, source.read_text(encoding="utf-8"))


def venv_python() -> Path:
    candidate = runtime_dir() / "venv" / "bin" / "python"
    if candidate.exists():
        return candidate
    # Compatibility with installs made before runtime state moved out of the
    # skill directory. New setup runs never create this legacy path.
    legacy = SKILL_ROOT / ".venv" / "bin" / "python"
    return legacy if legacy.exists() else Path(sys.executable)


ENV_KEYS = (
    "LLM_API_TEST_DATA_DIR",
    "LLM_API_TEST_RUNTIME_DIR",
    "LLM_API_TEST_DOTENV",
    "LLM_API_TEST_PROVIDERS_LOCAL",
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
    elif len(sys.argv) > 1 and sys.argv[1] == "venv-python":
        configure_skill_env()
        print(venv_python())
    else:
        print(ensure_skill_env())
