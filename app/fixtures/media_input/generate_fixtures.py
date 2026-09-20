"""Regenerate the checked-in synthetic media with Pillow and ffmpeg/flite.

This maintenance script is never imported by the parameter-test runtime.
Run from the repository root: python fixtures/media_input/generate_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import PIL
from PIL import Image


ROOT = Path(__file__).resolve().parent
IMAGE_FORMATS = {
    "png": ("PNG", "image/png", {}),
    "jpeg": ("JPEG", "image/jpeg", {"quality": 95, "subsampling": 0}),
    "webp": ("WEBP", "image/webp", {"lossless": True, "method": 6}),
    "gif": ("GIF", "image/gif", {}),
}
AUDIO_FORMATS = {
    "wav": ("audio/wav", ["-c:a", "pcm_s16le"]),
    "mp3": ("audio/mpeg", ["-c:a", "libmp3lame", "-b:a", "24k", "-write_xing", "0", "-id3v2_version", "0"]),
    "flac": ("audio/flac", ["-c:a", "flac", "-compression_level", "12"]),
    "ogg": ("audio/ogg", ["-c:a", "libvorbis", "-q:a", "0"]),
    "m4a": ("audio/m4a", ["-c:a", "aac", "-b:a", "24k", "-movflags", "+faststart"]),
    "aac": ("audio/aac", ["-c:a", "aac", "-b:a", "24k"]),
    "aiff": ("audio/aiff", ["-c:a", "pcm_s16be"]),
}
SPEECH = {
    "green_seven": "The lantern is green. The number is seven.",
    "blue_nine": "The lantern is blue. The number is nine.",
}


def record(path: Path, mime: str, expected_text: str, semantic: dict, commands: list) -> dict:
    raw = path.read_bytes()
    return {
        "file": path.name,
        "mime_type": mime,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "expected_text": expected_text,
        "expected_semantic": semantic,
        "generation_commands": commands,
    }


def main() -> None:
    fixtures = {}
    for color, rgb in (("red", (255, 0, 0)), ("blue", (0, 0, 255))):
        formats = {}
        for extension, (pillow_format, mime, options) in IMAGE_FORMATS.items():
            path = ROOT / f"{color}.{extension}"
            Image.new("RGB", (256, 256), rgb).save(path, format=pillow_format, **options)
            formats[extension] = record(path, mime, color, {"dominant_color": color}, [
                {"tool": "Pillow", "operation": "Image.new('RGB', (256, 256), rgb).save", "rgb": list(rgb), "format": pillow_format, "options": options}
            ])
        fixtures[color] = {"kind": "image", "width": 256, "height": 256, "formats": formats}

    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for name, speech in SPEECH.items():
        wav = ROOT / f"{name}.wav"
        synth = base + ["-f", "lavfi", "-i", f"flite=text='{speech}':voice=slt", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact", wav.name]
        subprocess.run(synth, cwd=ROOT, check=True)
        formats = {}
        for extension, (mime, codec_options) in AUDIO_FORMATS.items():
            path = ROOT / f"{name}.{extension}"
            commands = [synth]
            # Keep the primary WAV at 16 kHz; narrowband AIFF avoids doubling
            # the uncompressed storage while retaining intelligible speech.
            output_rate = 8000 if extension == "aiff" else 16000
            if extension != "wav":
                transcode = base + ["-i", wav.name, "-ar", str(output_rate), "-ac", "1"] + codec_options + ["-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact", path.name]
                subprocess.run(transcode, cwd=ROOT, check=True)
                commands.append(transcode)
            formats[extension] = record(path, mime, speech, {"lantern_color": name.split("_")[0], "number": 7 if name == "green_seven" else 9}, commands)
            formats[extension]["sample_rate_hz"] = output_rate
            formats[extension]["channels"] = 1
        fixtures[name] = {"kind": "audio", "formats": formats}

    total = sum(row["byte_length"] for item in fixtures.values() for row in item["formats"].values())
    manifest = {
        "schema_version": 1,
        "provenance": {
            "type": "locally_generated_synthetic_media",
            "generator": "fixtures/media_input/generate_fixtures.py",
            "regenerate_command": "python fixtures/media_input/generate_fixtures.py",
            "command_working_directory": "fixtures/media_input",
            "image_description": "Solid red or blue 256 by 256 squares, with no text or metadata containing the answer.",
            "audio_description": "Offline flite SLT speech synthesis of the exact expected_text. Other audio containers are transcoded from the same WAV.",
            "ffmpeg_version": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0],
            "pillow_version": PIL.__version__,
            "runtime_tools_required": [],
        },
        "total_asset_bytes": total,
        "fixtures": fixtures,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {sum(len(item['formats']) for item in fixtures.values())} assets ({total} bytes)")


if __name__ == "__main__":
    main()
