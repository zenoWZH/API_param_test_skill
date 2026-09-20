"""Build small media probes from the checked-in, attributed NASA sources.

Run from the repository root: python fixtures/media_input/generate_real_fixtures.py
This script is offline; it never calls a model or downloads media at runtime.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import PIL
from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parent
POLICY_URL = "https://www.nasa.gov/nasa-brand-center/images-and-media/"
SOURCES = {
    "nasa_earth_apollo17": {
        "file": "sources/earth_original.jpg",
        "url": "https://science.nasa.gov/wp-content/uploads/2024/03/blue-marble-apollo-17-16x9-1.jpg",
        "page_url": "https://science.nasa.gov/earth/earth-observatory/history-of-the-blue-marble/",
        "title": "Apollo 17 Blue Marble photograph of Earth",
        "credit": "NASA; Apollo 17 crew; image AS17-148-22727",
        "sha256": "c9edb8f844b76c0cd5ca4486df95277e5eff1f8fd7d650d7e50b4370f46c4f32",
        "byte_length": 138049,
    },
    "nasa_moon_galileo": {
        "file": "sources/moon_original.jpg",
        "url": "https://assets.science.nasa.gov/dynamicimage/assets/science/psd/photojournal/pia/pia00/pia00405/PIA00405.jpg?w=768&h=768&fit=clip",
        "page_url": "https://science.nasa.gov/photojournal/earths-moon/",
        "title": "Earth's Moon, photographed by Galileo on December 7, 1992",
        "credit": "NASA/JPL; Galileo; image PIA00405",
        "sha256": "0e863f60f4f747c602e65e7f54081f6f7aaff0ea0f9e9a32fa779f76e9247cf8",
        "byte_length": 102031,
    },
    "nasa_eagle_landed": {
        "file": "sources/eagle_original.mp3",
        "url": "https://www.nasa.gov/wp-content/uploads/2015/01/569462main_eagle_has_landed.mp3",
        "page_url": "https://www.nasa.gov/historical-sounds/",
        "title": "Apollo 11: Eagle Has Landed",
        "credit": "NASA; Apollo 11 air-to-ground voice recording",
        "sha256": "c2cf9c2754ec3c2f7315acd4fcdd7d892fb2027919c1a80adf90a49f63c0316c",
        "byte_length": 26388,
        "transcript_url": "https://www.nasa.gov/missions/apollo/apollo-11/wide-awake-on-the-sea-of-tranquillity/",
        "transcript_basis": "NASA's published Armstrong quotation; punctuation is editorial. The short source clip is preserved in full. No local speech-recognition result is claimed.",
    },
}
TRANSCRIPT = "Houston, Tranquility Base here. The Eagle has landed."


def checked_source(source_id):
    source = SOURCES[source_id]
    path = ROOT / source["file"]
    raw = path.read_bytes()
    if len(raw) != source["byte_length"] or hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("Public source media changed: " + source_id)
    return path


def record(path, mime, expected, semantic, source_ids, commands, **metadata):
    raw = path.read_bytes()
    return {"file": path.name, "mime_type": mime, "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_length": len(raw), "expected_text": expected, "expected_semantic": semantic,
            "source_ids": source_ids, "generation_commands": commands, **metadata}


def main():
    fixtures = {}
    for subject, source_id in (("earth", "nasa_earth_apollo17"), ("moon", "nasa_moon_galileo")):
        source_path = checked_source(source_id)
        path = ROOT / f"photo_{subject}.jpeg"
        with Image.open(source_path) as original:
            photo = ImageOps.fit(original.convert("RGB"), (512, 512), method=Image.Resampling.LANCZOS)
            # Saving a new image without original EXIF/comments prevents a
            # source title from acting as a textual answer in the payload.
            photo.save(path, format="JPEG", quality=88, subsampling=0, optimize=True)
        commands = [{"tool": "Pillow", "input": SOURCES[source_id]["file"],
                     "operation": "ImageOps.fit(image.convert('RGB'), (512, 512), method=LANCZOS).save",
                     "output": path.name, "save_options": {"format": "JPEG", "quality": 88, "subsampling": 0, "optimize": True}}]
        fixtures[f"photo_{subject}"] = {"kind": "image", "default_format": "jpeg", "width": 512, "height": 512,
            "content_origin": "spacecraft_photograph", "formats": {"jpeg": record(path, "image/jpeg", subject,
                {"object": subject}, [source_id], commands)}}

    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for variant, subjects in (("earth_moon", ["earth", "moon"]), ("moon_earth", ["moon", "earth"])):
        path = ROOT / f"video_{variant}.mp4"
        command = base + ["-loop", "1", "-framerate", "4", "-t", "2", "-i", f"photo_{subjects[0]}.jpeg",
            "-loop", "1", "-framerate", "4", "-t", "2", "-i", f"photo_{subjects[1]}.jpeg",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]", "-map", "[v]",
            "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-r", "4", "-t", "4", "-an",
            "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-movflags", "+faststart", path.name]
        subprocess.run(command, cwd=ROOT, check=True)
        fixtures[f"video_{variant}"] = {"kind": "video", "default_format": "mp4", "width": 512, "height": 512,
            "duration_seconds": 4.0, "content_origin": "ordered_real_photographs",
            "formats": {"mp4": record(path, "video/mp4", ",".join(subjects),
                {"object_order": subjects, "segments": [{"start_seconds": 0, "end_seconds": 2, "object": subjects[0]},
                                                        {"start_seconds": 2, "end_seconds": 4, "object": subjects[1]}]},
                ["nasa_earth_apollo17", "nasa_moon_galileo"], [command], codec="h264", pixel_format="yuv420p", fps=4,
                has_audio=False, duration_seconds=4.0)}}

    checked_source("nasa_eagle_landed")
    formats = {}
    for extension, mime, codec in (("wav", "audio/wav", ["-c:a", "pcm_s16le"]),
                                    ("mp3", "audio/mpeg", ["-c:a", "libmp3lame", "-b:a", "32k", "-write_xing", "0", "-id3v2_version", "0"])):
        path = ROOT / f"nasa_eagle.{extension}"
        command = base + ["-i", "sources/eagle_original.mp3", "-ar", "16000", "-ac", "1"] + codec + [
            "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact", path.name]
        subprocess.run(command, cwd=ROOT, check=True)
        formats[extension] = record(path, mime, TRANSCRIPT, {"transcript": TRANSCRIPT, "speech_origin": "human_historical_recording"},
                                   ["nasa_eagle_landed"], [command], sample_rate_hz=16000, channels=1)
    fixtures["nasa_eagle"] = {"kind": "audio", "default_format": "wav", "content_origin": "human_historical_recording", "formats": formats}
    sources = {}
    for key, source in SOURCES.items():
        sources[key] = {**source, "retrieved_at": "2026-09-20", "license_url": POLICY_URL,
                       "license": "NASA media usage guidelines: factual educational/informational use with source acknowledgement; no endorsement implied.",
                       "retrieval": {"method": "GET", "authentication": "none", "final_url": source["url"]}}
    manifest = {"schema_version": 1, "sources": sources,
        "provenance": {"type": "derived_from_public_nasa_media", "generator": "fixtures/media_input/generate_real_fixtures.py",
            "regenerate_command": "python fixtures/media_input/generate_real_fixtures.py", "command_working_directory": "fixtures/media_input",
            "ffmpeg_version": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0],
            "pillow_version": PIL.__version__, "runtime_tools_required": [],
            "video_scope": "A real MP4 encoding of ordered spacecraft photographs. This tests visual temporal order, not natural scene motion or live action.",
            "source_attribution": "NASA source material is included as fixture input. Model outputs are attributable to the tested model, not NASA."},
        "total_asset_bytes": sum(row["byte_length"] for item in fixtures.values() for row in item["formats"].values()),
        "fixtures": fixtures}
    (ROOT / "real_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(fixtures)} real-content fixtures ({manifest['total_asset_bytes']} asset bytes)")


if __name__ == "__main__":
    main()
