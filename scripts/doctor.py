from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = SKILL_ROOT / "app"
PACKAGE_ROOT = SKILL_ROOT / "packages" / "model-profile-db"
DATA_ROOT = Path(
    os.getenv("LLM_API_TEST_DATA_DIR")
    or Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    / "llm-api-test"
).expanduser()
RUNTIME_ROOT = Path(
    os.getenv("LLM_API_TEST_RUNTIME_DIR")
    or Path(os.getenv("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    / "llm-api-test"
).expanduser()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _writable_destination(path: Path) -> bool:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


def _artifact_audit() -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    manifest_path = PACKAGE_ROOT / "model_profile_db" / "data" / "manifest.json"
    if not manifest_path.is_file():
        return {}, [f"missing MPDB manifest: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, [f"invalid MPDB manifest: {exc}"]
    data_root = manifest_path.parent.resolve()
    package_module_root = data_root.parent
    verified = 0
    for name, row in (manifest.get("artifacts") or {}).items():
        artifact = (data_root / str(row.get("path") or "")).resolve()
        if artifact != package_module_root and package_module_root not in artifact.parents:
            errors.append(f"artifact escapes package data: {name}")
            continue
        if not artifact.is_file():
            errors.append(f"missing artifact: {name}")
            continue
        if artifact.stat().st_size != row.get("size"):
            errors.append(f"artifact size mismatch: {name}")
            continue
        if _sha256(artifact) != row.get("sha256"):
            errors.append(f"artifact digest mismatch: {name}")
            continue
        verified += 1
    sqlite_path = data_root / "catalog.sqlite3"
    if sqlite_path.is_file():
        try:
            uri = f"file:{sqlite_path}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                errors.append(f"SQLite integrity check failed: {integrity}")
        except Exception as exc:
            errors.append(f"SQLite integrity check error: {exc}")
    return {
        "package_version": "0.3.0",
        "mpdb_schema_version": manifest.get("mpdb_schema_version"),
        "catalog_version": manifest.get("catalog_version"),
        "catalog_digest": manifest.get("catalog_digest"),
        "test_extension_digest": manifest.get("test_extension_digest"),
        "counts": manifest.get("counts"),
        "verified_artifacts": verified,
        "artifact_count": len(manifest.get("artifacts") or {}),
    }, errors


def _runtime_audit(python: Path) -> tuple[dict[str, Any] | None, list[str]]:
    if not python.is_file():
        return None, ["managed runtime is not installed; run setup"]
    code = (
        "import json,sys;"
        f"sys.path.insert(0,{str(APP_ROOT)!r});"
        "from lib.model_profile_catalog import catalog_metadata;"
        "print(json.dumps(catalog_metadata(),sort_keys=True))"
    )
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=APP_ROOT,
        )
    except Exception as exc:
        return None, [f"runtime audit failed: {exc}"]
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return None, [f"runtime consumer failed: {(detail[-1] if detail else 'unknown error')}"]
    try:
        return json.loads(proc.stdout), []
    except json.JSONDecodeError:
        return None, ["runtime consumer returned invalid JSON"]


def _migration_audit(mpdb: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Verify the declared database and portable files against the same import."""
    errors: list[str] = []
    try:
        manifest = json.loads((SKILL_ROOT / "MIGRATION_MANIFEST.json").read_text(encoding="utf-8"))
        expected = manifest["model_profile_database"]
        for field in ("package_version", "mpdb_schema_version", "catalog_version",
                      "catalog_digest", "test_extension_digest"):
            if expected.get(field) != mpdb.get(field):
                errors.append(f"migration/database identity mismatch: {field}")
        files = manifest.get("bundled_files")
        if not isinstance(files, dict) or not files:
            errors.append("migration manifest has no verified bundled file inventory")
            files = {}
        root = SKILL_ROOT.resolve()
        verified = 0
        for name, expected_file in files.items():
            raw_path = SKILL_ROOT / name
            path = raw_path.resolve()
            if path == root or root not in path.parents or raw_path.is_symlink():
                errors.append(f"unsafe migration inventory path: {name}")
            elif not path.is_file():
                errors.append(f"missing bundled file: {name}")
            elif path.stat().st_size != expected_file.get("size") or _sha256(path) != expected_file.get("sha256"):
                errors.append(f"bundled file drift: {name}")
            else:
                verified += 1
        return {"source": manifest.get("source"), "verified_files": verified,
                "file_count": len(files), "has_worktree_snapshot": bool(manifest.get("source_worktree_snapshot"))}, errors
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {}, [f"invalid migration manifest: {exc}"]


def _bundle_stats() -> dict[str, Any]:
    ignored_parts = {
        ".git",
        ".venv",
        ".kilo",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
    }
    files = [
        path
        for path in SKILL_ROOT.rglob("*")
        if path.is_file()
        and not ignored_parts.intersection(path.relative_to(SKILL_ROOT).parts)
        and not any(part.endswith(".egg-info") for part in path.parts)
    ]
    largest = max(files, key=lambda path: path.stat().st_size) if files else None
    oversize = [
        str(path.relative_to(SKILL_ROOT))
        for path in files
        if path.stat().st_size > 1024 * 1024
    ]
    return {
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "largest_file": str(largest.relative_to(SKILL_ROOT)) if largest else None,
        "largest_file_bytes": largest.stat().st_size if largest else 0,
        "openclaw_managed_bundle_compatible": len(files) <= 256
        and not oversize
        and sum(path.stat().st_size for path in files) <= 8 * 1024 * 1024,
        "files_over_1_mib": oversize,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline, read-only llm-api-test audit.")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="fail unless runtime is ready")
    args = parser.parse_args()

    errors: list[str] = []
    required = [
        APP_ROOT / "config.yaml",
        PACKAGE_ROOT / "pyproject.toml",
        SKILL_ROOT / "MIGRATION_MANIFEST.json",
        SKILL_ROOT / "bin" / "llm-api-test",
    ]
    missing = [str(path.relative_to(SKILL_ROOT)) for path in required if not path.is_file()]
    errors.extend(f"missing required file: {path}" for path in missing)
    mpdb, artifact_errors = _artifact_audit()
    errors.extend(artifact_errors)
    migration, migration_errors = _migration_audit(mpdb)
    errors.extend(migration_errors)
    runtime_python = RUNTIME_ROOT / "venv" / "bin" / "python"
    runtime, runtime_errors = _runtime_audit(runtime_python)
    errors.extend(runtime_errors)
    writable = {
        "data": _writable_destination(DATA_ROOT),
        "runtime": _writable_destination(RUNTIME_ROOT),
    }
    for label, value in writable.items():
        if not value:
            errors.append(f"{label} destination is not writable")
    payload = {
        "schema": "llm-api-test.doctor.v1",
        "ready": not errors,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "paths": {
            "skill": str(SKILL_ROOT),
            "data": str(DATA_ROOT),
            "runtime": str(RUNTIME_ROOT),
            "runtime_python": str(runtime_python),
        },
        "writable_destinations": writable,
        "mpdb": mpdb,
        "migration": migration,
        "runtime_consumer": runtime,
        "bundle": _bundle_stats(),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("ready=" + str(payload["ready"]).lower())
        print("skill=" + str(SKILL_ROOT))
        print("data=" + str(DATA_ROOT))
        print("runtime=" + str(RUNTIME_ROOT))
        print("mpdb=" + str(mpdb.get("catalog_version") or "unavailable"))
        for error in errors:
            print("error: " + error, file=sys.stderr)
    return 1 if args.strict and errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
