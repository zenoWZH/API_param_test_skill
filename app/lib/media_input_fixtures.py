"""Read and verify fixed media fixtures without synthesis or network access."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any


_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "media_input"
_MIME_TYPES = {
    "png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
    "wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
    "m4a": "audio/m4a", "aac": "audio/aac", "aiff": "audio/aiff",
    "mp4": "video/mp4",
}
_REMOTE_VIDEO_URLS = {
    "zhipu_elephants": "https://sfile.chatglm.cn/testpath/video/b844f8f1-5df9-556c-a515-3d3bfaa736e8_0.mp4",
}


class FixtureIntegrityError(ValueError):
    """The fixture manifest or bytes do not satisfy the checked-in contract."""


def _read_manifest() -> dict[str, Any]:
    result = {"schema_version": 1, "fixtures": {}}
    paths = [_FIXTURE_ROOT / "manifest.json"]
    if (_FIXTURE_ROOT / "real_manifest.json").exists():
        paths.append(_FIXTURE_ROOT / "real_manifest.json")
    for path in paths:
        manifest = _read_manifest_file(path)
        duplicate = set(result["fixtures"]).intersection(manifest["fixtures"])
        if duplicate:
            raise FixtureIntegrityError("Duplicate media fixture names across manifests")
        result["fixtures"].update(manifest["fixtures"])
    return result


def _read_manifest_file(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FixtureIntegrityError("Cannot read the media fixture manifest") from exc
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or not isinstance(manifest.get("fixtures"), dict)):
        raise FixtureIntegrityError("Invalid media fixture manifest schema")
    return manifest


def fixture(name: str, format: str | None = None) -> dict[str, Any]:
    """Load a fixed image, speech clip or video, verifying every read.

    The default format is PNG for images and WAV for audio. ``jpg`` aliases
    ``jpeg``. No ffmpeg or Pillow import is needed at runtime.
    """
    manifest = _read_manifest()
    item = manifest["fixtures"].get(name)
    if item is None:
        raise ValueError(f"Unknown media fixture: {name!r}")
    if not isinstance(item, dict) or item.get("kind") not in {"image", "audio", "video"} or not isinstance(item.get("formats"), dict):
        raise FixtureIntegrityError(f"Invalid fixture entry: {name!r}")
    extension = format or item.get("default_format") or {"image": "png", "audio": "wav", "video": "mp4"}[item["kind"]]
    extension = "jpeg" if extension == "jpg" else extension
    row = item["formats"].get(extension)
    if row is None:
        raise ValueError(f"Unsupported format {extension!r} for fixture {name!r}")
    if not isinstance(row, dict):
        raise FixtureIntegrityError("Invalid media format record")
    expected_mime = _MIME_TYPES.get(extension)
    if not expected_mime or not expected_mime.startswith(item["kind"] + "/") or row.get("mime_type") != expected_mime:
        raise FixtureIntegrityError("Fixture MIME type does not match its format")
    filename = row.get("file")
    if not isinstance(filename, str) or filename != f"{name}.{extension}":
        raise FixtureIntegrityError("Unexpected fixture filename")
    path = (_FIXTURE_ROOT / filename).resolve()
    if path.parent != _FIXTURE_ROOT.resolve():
        raise FixtureIntegrityError("Fixture path leaves its asset directory")
    digest = row.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise FixtureIntegrityError("Invalid fixture SHA256 declaration")
    if type(row.get("byte_length")) is not int or row["byte_length"] <= 0:
        raise FixtureIntegrityError("Invalid fixture byte length")
    if not isinstance(row.get("expected_text"), str) or not row["expected_text"] or not isinstance(row.get("expected_semantic"), dict):
        raise FixtureIntegrityError("Fixture semantic expectation is missing")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FixtureIntegrityError("Cannot read media fixture bytes") from exc
    if len(raw) != row["byte_length"] or hashlib.sha256(raw).hexdigest() != digest:
        raise FixtureIntegrityError("Media fixture size or SHA256 mismatch")
    return {
        "data_base64": base64.b64encode(raw).decode("ascii"),
        "mime_type": expected_mime,
        "sha256": digest,
        "expected_text": row["expected_text"],
        "expected_semantic": dict(row["expected_semantic"]),
        "byte_length": len(raw),
        "path": str(path),
        "kind": item["kind"],
    }


def image_fixture(color: str = "red", format: str = "png") -> dict[str, Any]:
    if color not in {"red", "blue"}:
        raise ValueError(f"Unknown image fixture color: {color!r}")
    return fixture(color, format)


def audio_fixture(variant: str = "green_seven", format: str = "wav") -> dict[str, Any]:
    if variant not in {"green_seven", "blue_nine", "nasa_eagle"}:
        raise ValueError(f"Unknown audio fixture variant: {variant!r}")
    return fixture(variant, format)


def photo_fixture(subject: str = "earth", format: str = "jpeg") -> dict[str, Any]:
    """Read an attributed spacecraft photograph without text labels/EXIF."""
    if subject not in {"earth", "moon"}:
        raise ValueError(f"Unknown photograph fixture: {subject!r}")
    return fixture("photo_" + subject, format)


def video_fixture(variant: str = "earth_moon", format: str = "mp4") -> dict[str, Any]:
    """Read a four-second MP4 with two real photographs in fixed order."""
    if variant not in {"earth_moon", "moon_earth"}:
        raise ValueError(f"Unknown video fixture variant: {variant!r}")
    return fixture("video_" + variant, format)


def remote_video_fixture(variant: str = "zhipu_elephants") -> dict[str, Any]:
    """Read pinned metadata for one reviewed public URL; never download it.

    The live dispatcher must separately verify the actual public bytes against
    this hash before and after a URL-based model request.
    """
    if variant not in _REMOTE_VIDEO_URLS:
        raise ValueError(f"Unknown remote video fixture: {variant!r}")
    manifest = _read_manifest_file(_FIXTURE_ROOT / "remote_manifest.json")
    row = manifest["fixtures"].get(variant)
    if not isinstance(row, dict) or row.get("kind") != "video":
        raise FixtureIntegrityError("Invalid remote video fixture record")
    if row.get("url") != _REMOTE_VIDEO_URLS[variant]:
        raise FixtureIntegrityError("Remote fixture URL is outside the reviewed HTTPS allowlist")
    if row.get("mime_type") != "video/mp4":
        raise FixtureIntegrityError("Remote fixture MIME type must be video/mp4")
    digest = row.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise FixtureIntegrityError("Invalid remote fixture SHA256 declaration")
    if type(row.get("byte_length")) is not int or not 0 < row["byte_length"] <= 10_000_000:
        raise FixtureIntegrityError("Invalid remote fixture byte length")
    if not isinstance(row.get("expected_text"), str) or not row["expected_text"]:
        raise FixtureIntegrityError("Remote fixture semantic expectation is missing")
    documentation = row.get("documentation_url")
    if not isinstance(documentation, str) or not documentation.startswith("https://docs.bigmodel.cn/"):
        raise FixtureIntegrityError("Remote fixture official documentation URL is missing")
    sources, evidence = manifest.get("sources"), row.get("evidence")
    if not isinstance(sources, dict) or not isinstance(evidence, list) or not evidence:
        raise FixtureIntegrityError("Remote fixture source evidence is missing")
    if any(not isinstance(key, str) or key not in sources for key in evidence):
        raise FixtureIntegrityError("Remote fixture source evidence is not closed")
    for key in evidence:
        source = sources[key]
        if (not isinstance(source, dict) or source.get("url") != documentation
                or not isinstance(source.get("facts"), list) or not source["facts"]):
            raise FixtureIntegrityError("Remote fixture documentation evidence differs")
    return {"external_url": row["url"], "mime_type": row["mime_type"], "sha256": digest,
            "byte_length": row["byte_length"], "expected_text": row["expected_text"],
            "evidence": list(evidence), "documentation_url": documentation}
