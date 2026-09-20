"""Atomic durable run evidence and exact resource ownership for crash recovery."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

from .common import IntegrityError, PlanValidationError, canonical_json, digest_json, json_copy

_SECRETS = {"authorization", "proxy-authorization", "x-api-key", "api-key", "api_key", "apikey", "access_token", "refresh_token", "client_secret", "password"}


def evidence_copy(value):
    """Keep structured receipts and templates, redacting common credential fields."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in _SECRETS else evidence_copy(child) for key, child in value.items()}
    if isinstance(value, list):
        return [evidence_copy(child) for child in value]
    return json_copy(value)


class ResourceLedger:
    def __init__(self, evidence_dir, *, run_id: str, plan: dict, run_index: int, recover: bool = False):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", run_id):
            raise PlanValidationError("Unsafe run ID")
        if evidence_dir is None:
            if recover:
                raise PlanValidationError("Cleanup recovery requires an evidence directory")
            evidence_dir = tempfile.mkdtemp(prefix="test-runner-")
        self.base_dir = Path(evidence_dir).absolute()
        self.directory = self.base_dir / run_id
        if recover:
            if not self.directory.is_dir() or self.directory.is_symlink():
                raise PlanValidationError("Original run evidence directory is missing or unsafe")
        else:
            self.base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.mkdir(mode=0o700)
        self.path = self.directory / "ledger.json"
        self._lock = os.open(self.directory / "run.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if recover:
                with self.path.open(encoding="utf-8") as stream:
                    self.state = json.load(stream)
                checksum = self.state.pop("ledger_digest", None)
                if checksum != digest_json(self.state):
                    raise IntegrityError("Run ledger digest mismatch")
                if type(self.state.get("ledger_schema_version")) is not int or self.state["ledger_schema_version"] != 1 or self.state.get("run_id") != run_id or self.state.get("plan_digest") != plan["plan_digest"] or digest_json(self.state.get("target")) != digest_json(plan["target"]):
                    raise IntegrityError("Cleanup recovery does not match the exact original run and plan")
                for creation in self.state["creations"]:
                    if creation["status"] == "pending":
                        creation["status"] = "unknown"
                for resource in self.state["resources"]:
                    if resource["status"] == "cleaning":
                        resource["status"] = "cleanup_unknown"
                self.state["recovery_count"] += 1
                self.state["phase"] = "cleanup"
                self.state["active_request"] = None
            else:
                self.state = {
                    "ledger_schema_version": 1,
                    "plan_digest": plan["plan_digest"],
                    "target": json_copy(plan["target"]),
                    "run_id": run_id,
                    "run_index": run_index,
                    "phase": "business",
                    "status": "running",
                    "steps": {},
                    "attempts": [],
                    "creations": [],
                    "resources": [],
                    "events": [],
                    "recovery_count": 0,
                    "active_request": None,
                    "fatal_reason": None,
                }
            self.save()
        except BaseException:
            os.close(self._lock)
            self._lock = None
            raise

    def save(self) -> None:
        data = evidence_copy(self.state)
        data["ledger_digest"] = digest_json(data)
        descriptor, temporary = tempfile.mkstemp(prefix=".ledger-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(canonical_json(data))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def close(self) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            os.close(self._lock)
            self._lock = None
