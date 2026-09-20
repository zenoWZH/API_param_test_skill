"""Calendar-month retention for explicitly registered reports from this run.

The caller owns report creation. This module never discovers files to take
ownership of, follows symlinks, or removes unregistered/changed files.
"""
from __future__ import annotations

import calendar
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterable, Iterator
import uuid


DEFAULT_REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "approved_live_20260907"
MANIFEST_NAME = ".report-retention.json"
OWNER = "approved-report-retention-v1"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_FILES = 4096
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_META_KEYS = {"schema_version", "owner", "period", "batch", "created_at", "expires_at", "owned_files"}
_FILE_KEYS = {"path", "sha256", "size", "mtime_ns", "device", "inode"}


class RetentionError(ValueError):
    """Unsafe path, malformed metadata, or a changed report; no deletion allowed."""


def _utc(value: datetime | None = None) -> datetime:
    value = value if value is not None else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise RetentionError("retention timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def calendar_month_expiry(created_at: datetime) -> datetime:
    """Add one UTC calendar month, clamping to the last day of that month."""
    created = _utc(created_at)
    year, month = (created.year + 1, 1) if created.month == 12 else (created.year, created.month + 1)
    if year > 9999:
        raise RetentionError("retention expiry is outside supported dates")
    return created.replace(year=year, month=month, day=min(created.day, calendar.monthrange(year, month)[1]))


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise RetentionError("invalid UTC retention timestamp")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RetentionError("invalid UTC retention timestamp") from exc


def _relative_file(value: str | Path, batch: Path) -> str:
    raw = str(value)
    if "\x00" in raw or "\\" in raw:
        raise RetentionError("unsafe report path")
    if os.path.isabs(raw):
        try:
            raw = str(Path(raw).relative_to(batch))
        except ValueError as exc:
            raise RetentionError("report is outside its batch") from exc
    if raw in {"", "."} or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise RetentionError("unsafe report path")
    if PurePosixPath(raw).as_posix() != raw or raw.split("/")[0].startswith(MANIFEST_NAME):
        raise RetentionError("reserved or noncanonical report path")
    return raw


def _batch_paths(batch_dir: str | Path, root: str | Path) -> tuple[Path, Path]:
    root_path = Path(os.path.abspath(root))
    batch = Path(os.path.abspath(batch_dir))
    if batch.parent != root_path or batch.name.startswith("."):
        raise RetentionError("batch must be a visible direct child of the approved report root")
    return batch, root_path


@contextmanager
def _directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Open each path component relative to its parent, without symlink traversal."""
    current = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        yield current
    finally:
        os.close(current)


@contextmanager
def _batch_directory(batch: Path, *, create: bool = False) -> Iterator[int]:
    with _directory(batch, create=create) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _file_parent(batch_fd: int, relative: str) -> Iterator[tuple[int, str]]:
    parts = relative.split("/")
    current = os.dup(batch_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        yield current, parts[-1]
    finally:
        os.close(current)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _snapshot(batch_fd: int, relative: str) -> dict:
    with _file_parent(batch_fd, relative) as (parent_fd, name):
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise RetentionError("report must be a regular file without hard links")
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(before) != _identity(after) or _identity(after) != _identity(named):
                raise RetentionError("report changed during inspection")
            return {"path": relative, "sha256": digest.hexdigest(), "size": after.st_size,
                    "mtime_ns": after.st_mtime_ns, "device": after.st_dev, "inode": after.st_ino}
        finally:
            os.close(fd)


def _validate_metadata(meta: object, batch: Path, now: datetime) -> dict:
    if not isinstance(meta, dict) or set(meta) != _META_KEYS:
        raise RetentionError("invalid retention manifest schema")
    if type(meta["schema_version"]) is not int or meta["schema_version"] != 1 or meta["owner"] != OWNER:
        raise RetentionError("unknown retention manifest owner or version")
    if meta["period"] != "P1M" or meta["batch"] != batch.name:
        raise RetentionError("retention period or batch does not match")
    created = _parse_timestamp(meta["created_at"])
    expires = _parse_timestamp(meta["expires_at"])
    if created > now or expires != calendar_month_expiry(created):
        raise RetentionError("future creation time or incorrect calendar-month expiry")
    files = meta["owned_files"]
    if not isinstance(files, list) or len(files) > _MAX_FILES:
        raise RetentionError("invalid owned report list")
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != _FILE_KEYS:
            raise RetentionError("invalid owned report entry")
        if not isinstance(entry["path"], str):
            raise RetentionError("invalid owned report path")
        relative = _relative_file(entry["path"], batch)
        if relative != entry["path"] or relative in seen:
            raise RetentionError("duplicate or noncanonical owned report path")
        seen.add(relative)
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise RetentionError("invalid report digest")
        if any(type(entry[key]) is not int or entry[key] < 0 for key in ("size", "mtime_ns", "device", "inode")):
            raise RetentionError("invalid report file identity")
    return meta


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RetentionError("duplicate JSON manifest key")
        result[key] = value
    return result


def _read_manifest(batch_fd: int, batch: Path, now: datetime) -> dict:
    named = os.stat(MANIFEST_NAME, dir_fd=batch_fd, follow_symlinks=False)
    if not stat.S_ISREG(named.st_mode) or named.st_nlink != 1 or named.st_size > _MAX_MANIFEST_BYTES:
        raise RetentionError("invalid retention manifest file")
    fd = os.open(MANIFEST_NAME, _FILE_FLAGS, dir_fd=batch_fd)
    try:
        info = os.fstat(fd)
        if _identity(info) != _identity(named):
            raise RetentionError("invalid retention manifest file")
        data = bytearray()
        while chunk := os.read(fd, min(65536, _MAX_MANIFEST_BYTES + 1 - len(data))):
            data.extend(chunk)
            if len(data) > _MAX_MANIFEST_BYTES:
                raise RetentionError("retention manifest is too large")
        if (_identity(info) != _identity(os.fstat(fd)) or
                _identity(info) != _identity(os.stat(MANIFEST_NAME, dir_fd=batch_fd, follow_symlinks=False))):
            raise RetentionError("retention manifest changed during inspection")
        try:
            meta = json.loads(data, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RetentionError("unreadable retention manifest JSON") from exc
        return _validate_metadata(meta, batch, now)
    finally:
        os.close(fd)


def _write_manifest(batch_fd: int, meta: dict) -> None:
    payload = (json.dumps(meta, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise RetentionError("retention manifest is too large")
    temporary = MANIFEST_NAME + "." + uuid.uuid4().hex + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=batch_fd)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, MANIFEST_NAME, src_dir_fd=batch_fd, dst_dir_fd=batch_fd)
        os.fsync(batch_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=batch_fd)
        except FileNotFoundError:
            pass


def initialize_report_retention(batch_dir: str | Path, *, root: str | Path = DEFAULT_REPORT_ROOT,
                                created_at: datetime | None = None, now: datetime | None = None) -> dict:
    """Create a batch manifest without taking ownership of any existing file.

    Reinitialization validates and returns the existing manifest without extending
    its expiry. Pass the batch start time when initializing after report creation.
    """
    batch, _ = _batch_paths(batch_dir, root)
    current = _utc(now)
    created = _utc(created_at) if created_at is not None else current
    if created > current:
        raise RetentionError("batch creation cannot be in the future")
    with _batch_directory(batch, create=True) as batch_fd:
        try:
            return _read_manifest(batch_fd, batch, current)
        except FileNotFoundError:
            meta = {"schema_version": 1, "owner": OWNER, "period": "P1M", "batch": batch.name,
                    "created_at": _timestamp(created), "expires_at": _timestamp(calendar_month_expiry(created)),
                    "owned_files": []}
            _write_manifest(batch_fd, meta)
            return meta


def register_report_files(batch_dir: str | Path, files: Iterable[str | Path], *,
                          root: str | Path = DEFAULT_REPORT_ROOT, now: datetime | None = None) -> dict:
    """Register explicit completed/failed/partial reports, including their hashes.

    The producer should call this in ``finally`` after closing its report files.
    Re-registering an unchanged report is harmless; changing a registered report
    requires a new report path, so ownership cannot silently absorb later edits.
    """
    batch, _ = _batch_paths(batch_dir, root)
    current = _utc(now)
    with _batch_directory(batch) as batch_fd:
        meta = _read_manifest(batch_fd, batch, current)
        if current >= _parse_timestamp(meta["expires_at"]):
            raise RetentionError("cannot register reports in an expired batch")
        owned = {entry["path"]: entry for entry in meta["owned_files"]}
        for file in files:
            relative = _relative_file(file, batch)
            entry = _snapshot(batch_fd, relative)
            if relative in owned and owned[relative] != entry:
                raise RetentionError("registered report changed; use a new report path")
            owned[relative] = entry
        meta["owned_files"] = [owned[path] for path in sorted(owned)]
        _validate_metadata(meta, batch, current)
        _write_manifest(batch_fd, meta)
        return meta


def cleanup_approved_reports(*, root: str | Path = DEFAULT_REPORT_ROOT, apply: bool = False,
                             now: datetime | None = None) -> dict:
    """Inspect direct batch children; delete only unchanged owned files at expiry.

    Missing roots are a safe no-op. Malformed manifests, changed files, symlinks,
    and inaccessible entries are reported and preserved. Directories and manifests
    are kept as small audit records, and unregistered files are never scanned.
    """
    root_path = Path(os.path.abspath(root))
    current = _utc(now)
    result = {"period": "P1M", "checked_at": _timestamp(current), "apply": bool(apply), "batches": []}
    try:
        with _directory(root_path) as root_fd:
            names = sorted(os.listdir(root_fd))
    except FileNotFoundError:
        return result
    for name in names:
        if name.startswith("."):
            continue
        batch = root_path / name
        item = {"batch": name, "status": "preserved", "files": []}
        try:
            if stat.S_ISREG(os.stat(batch, follow_symlinks=False).st_mode):
                continue  # Unregistered files at the root are not report batches.
            with _batch_directory(batch) as batch_fd:
                try:
                    meta = _read_manifest(batch_fd, batch, current)
                except FileNotFoundError:
                    continue  # A directory without our manifest is not owned.
                item["expires_at"] = meta["expires_at"]
                if current < _parse_timestamp(meta["expires_at"]):
                    item["status"] = "not_expired"
                else:
                    item["status"] = "expired"
                    for entry in meta["owned_files"]:
                        file_result = {"path": entry["path"], "status": "preserved"}
                        try:
                            if _snapshot(batch_fd, entry["path"]) != entry:
                                file_result["reason"] = "report_changed"
                            elif apply:
                                with _file_parent(batch_fd, entry["path"]) as (parent_fd, filename):
                                    info = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
                                    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                                    expected = tuple(entry[key] for key in ("device", "inode", "size", "mtime_ns"))
                                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or identity != expected:
                                        raise RetentionError("report changed before deletion")
                                    os.unlink(filename, dir_fd=parent_fd)
                                    os.fsync(parent_fd)
                                file_result["status"] = "deleted"
                            else:
                                file_result["status"] = "would_delete"
                        except FileNotFoundError:
                            file_result["status"] = "already_missing"
                        except (OSError, RetentionError):
                            file_result["reason"] = "unsafe_or_unreadable_report"
                        item["files"].append(file_result)
        except (OSError, RetentionError):
            item["reason"] = "unsafe_or_invalid_batch"
        result["batches"].append(item)
    return result
