from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from lib import media_input_fixtures as media


@pytest.mark.parametrize("color,channel", [("red", 0), ("blue", 2)])
@pytest.mark.parametrize("format", ["png", "jpeg", "webp", "gif"])
def test_images_decode_to_the_expected_solid_square(color, channel, format):
    image_module = pytest.importorskip("PIL.Image")
    data = media.image_fixture(color, format)
    raw = base64.b64decode(data["data_base64"], validate=True)
    with image_module.open(io.BytesIO(raw)) as image:
        assert image.size == (256, 256)
        colors = image.convert("RGB").getextrema()
        assert colors[channel][0] >= 250
        assert all(colors[i][1] <= 5 for i in range(3) if i != channel)
        assert not any(key.lower() in {"comment", "description", "exif"} for key in image.info)
    assert data["expected_text"] == color


@pytest.mark.parametrize("variant", ["green_seven", "blue_nine"])
def test_wav_contains_nonempty_varying_speech_samples(variant):
    data = media.audio_fixture(variant)
    raw = base64.b64decode(data["data_base64"], validate=True)
    with wave.open(io.BytesIO(raw)) as audio:
        assert audio.getnchannels() == 1
        assert audio.getframerate() == 16000
        assert audio.getsampwidth() == 2
        assert 2 < audio.getnframes() / audio.getframerate() < 5
        frames = audio.readframes(audio.getnframes())
    assert len(set(frames)) > 128
    assert data["expected_text"].startswith("The lantern is ")
    assert data["expected_semantic"]["number"] in {7, 9}


@pytest.mark.parametrize("variant", ["green_seven", "blue_nine"])
@pytest.mark.parametrize("format", ["wav", "mp3", "flac", "ogg", "m4a", "aac", "aiff"])
def test_audio_containers_decode_to_mono_speech(variant, format):
    decoder = shutil.which("ffmpeg")
    if decoder is None:
        pytest.skip("ffmpeg is only needed for the offline container decode check")
    data = media.audio_fixture(variant, format)
    result = subprocess.run(
        [decoder, "-v", "error", "-i", data["path"], "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        capture_output=True, check=True, timeout=10,
    )
    assert 2 * 16000 * 2 < len(result.stdout) < 5 * 16000 * 2
    assert len(set(result.stdout)) > 128


def test_manifest_sizes_hashes_and_variant_identity():
    manifest = json.loads((media._FIXTURE_ROOT / "manifest.json").read_text())
    total = 0
    for name, item in manifest["fixtures"].items():
        digests = set()
        for format in item["formats"]:
            data = media.fixture(name, format)
            raw = base64.b64decode(data["data_base64"], validate=True)
            assert hashlib.sha256(raw).hexdigest() == data["sha256"]
            digests.add(data["sha256"])
            total += data["byte_length"]
        assert len(digests) == len(item["formats"])
    assert total == manifest["total_asset_bytes"] < 500_000
    assert media.fixture("red")["sha256"] != media.fixture("blue")["sha256"]
    assert media.fixture("green_seven")["sha256"] != media.fixture("blue_nine")["sha256"]
    assert media.fixture("red", "jpg") == media.fixture("red", "jpeg")


@pytest.fixture
def isolated_fixtures(tmp_path, monkeypatch):
    root = tmp_path / "media_input"
    shutil.copytree(media._FIXTURE_ROOT, root)
    monkeypatch.setattr(media, "_FIXTURE_ROOT", root)
    return root


def test_modified_bytes_are_rejected_even_after_a_successful_read(isolated_fixtures):
    media.image_fixture()
    path = isolated_fixtures / "red.png"
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(media.FixtureIntegrityError, match="SHA256 mismatch"):
        media.image_fixture()


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("mime_type", "audio/wav"), ("file", "../red.png"), ("byte_length", 1)])
def test_manifest_tampering_is_rejected(isolated_fixtures, field, value):
    path = isolated_fixtures / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["fixtures"]["red"]["formats"]["png"][field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(media.FixtureIntegrityError):
        media.image_fixture()


def test_unknown_names_and_formats_are_rejected():
    for call in (lambda: media.fixture("missing"), lambda: media.image_fixture("green"),
                 lambda: media.audio_fixture("red"), lambda: media.image_fixture(format="wav"),
                 lambda: media.photo_fixture("mars"), lambda: media.video_fixture("unknown")):
        with pytest.raises(ValueError):
            call()


def test_real_media_has_attributed_retrievable_sources_and_verified_source_bytes():
    manifest = json.loads((media._FIXTURE_ROOT / "real_manifest.json").read_text())
    for source in manifest["sources"].values():
        raw = (media._FIXTURE_ROOT / source["file"]).read_bytes()
        assert len(raw) == source["byte_length"]
        assert hashlib.sha256(raw).hexdigest() == source["sha256"]
        assert source["url"].startswith("https://") and source["page_url"].startswith("https://")
        assert source["credit"] and source["license"] and source["license_url"].startswith("https://www.nasa.gov/")
    total = 0
    for name, item in manifest["fixtures"].items():
        for format, row in item["formats"].items():
            value = media.fixture(name, format)
            assert row["source_ids"] and set(row["source_ids"]) <= set(manifest["sources"])
            assert row["generation_commands"]
            total += value["byte_length"]
    assert total == manifest["total_asset_bytes"] < 1_000_000


@pytest.mark.parametrize("subject", ["earth", "moon"])
def test_photographs_decode_with_spatial_detail_and_no_answer_metadata(subject):
    image_module = pytest.importorskip("PIL.Image")
    data = media.photo_fixture(subject)
    with image_module.open(io.BytesIO(base64.b64decode(data["data_base64"]))) as image:
        assert image.format == "JPEG" and image.size == (512, 512)
        pixels = image.resize((64, 64)).convert("RGB").tobytes()
        assert len(set(zip(pixels[::3], pixels[1::3], pixels[2::3]))) > 1000
        assert not any(key.lower() in {"exif", "comment", "description"} for key in image.info)
    assert data["kind"] == "image" and data["expected_text"] == subject
    assert media.fixture("photo_" + subject) == data


@pytest.mark.parametrize("variant,order", [("earth_moon", ["earth", "moon"]), ("moon_earth", ["moon", "earth"])])
def test_mp4_decodes_and_has_the_actual_photographs_in_expected_temporal_order(variant, order):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe are only needed for offline fixture verification")
    image_module = pytest.importorskip("PIL.Image")
    image_chops = pytest.importorskip("PIL.ImageChops")
    image_stat = pytest.importorskip("PIL.ImageStat")
    data = media.video_fixture(variant)
    probe = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", data["path"]],
                           capture_output=True, text=True, check=True, timeout=10)
    info = json.loads(probe.stdout)
    assert len(info["streams"]) == 1
    stream = info["streams"][0]
    assert (stream["codec_name"], stream["codec_type"], stream["width"], stream["height"], stream["pix_fmt"]) == ("h264", "video", 512, 512, "yuv420p")
    assert float(info["format"]["duration"]) == 4.0
    assert stream["r_frame_rate"] == "4/1"
    tags = json.dumps([info["format"].get("tags", {}), stream.get("tags", {})]).lower()
    assert "earth" not in tags and "moon" not in tags
    for timestamp, subject in zip((0.5, 2.5), order):
        decoded = subprocess.run([ffmpeg, "-v", "error", "-ss", str(timestamp), "-i", data["path"],
                                  "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"],
                                 capture_output=True, check=True, timeout=10)
        with image_module.open(io.BytesIO(decoded.stdout)) as frame, image_module.open(media.photo_fixture(subject)["path"]) as photo:
            difference = image_chops.difference(frame.convert("RGB"), photo.convert("RGB"))
            assert max(image_stat.Stat(difference).mean) < 8
    assert data["mime_type"] == "video/mp4" and data["kind"] == "video"
    assert data["expected_semantic"]["object_order"] == order
    assert data["expected_text"] == ",".join(order)


def test_historical_human_audio_is_short_decodable_and_has_an_official_transcript_source():
    data = media.audio_fixture("nasa_eagle", "wav")
    with wave.open(io.BytesIO(base64.b64decode(data["data_base64"]))) as audio:
        assert audio.getnchannels() == 1 and audio.getframerate() == 16000 and audio.getsampwidth() == 2
        assert 5 < audio.getnframes() / audio.getframerate() < 6
        assert len(set(audio.readframes(audio.getnframes()))) > 128
    assert data["expected_semantic"]["speech_origin"] == "human_historical_recording"
    assert data["expected_text"] == "Houston, Tranquility Base here. The Eagle has landed."
    manifest = json.loads((media._FIXTURE_ROOT / "real_manifest.json").read_text())
    source = manifest["sources"]["nasa_eagle_landed"]
    assert source["transcript_url"].startswith("https://www.nasa.gov/")
    mp3 = media.audio_fixture("nasa_eagle", "mp3")
    assert mp3["mime_type"] == "audio/mpeg"
    assert not base64.b64decode(mp3["data_base64"]).startswith(b"ID3")


@pytest.mark.parametrize("name", ["photo_earth", "video_earth_moon", "nasa_eagle"])
def test_changed_real_media_is_rejected(isolated_fixtures, name):
    value = media.fixture(name)
    path = Path(value["path"])
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(media.FixtureIntegrityError, match="SHA256 mismatch"):
        media.fixture(name)


def test_duplicate_fixture_names_in_real_manifest_are_rejected(isolated_fixtures):
    path = isolated_fixtures / "real_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["fixtures"]["red"] = manifest["fixtures"]["photo_earth"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(media.FixtureIntegrityError, match="Duplicate"):
        media.image_fixture("red")


def test_reviewed_remote_video_metadata_is_bound_to_one_public_url():
    value = media.remote_video_fixture("zhipu_elephants")
    assert value["external_url"] == "https://sfile.chatglm.cn/testpath/video/b844f8f1-5df9-556c-a515-3d3bfaa736e8_0.mp4"
    assert value["mime_type"] == "video/mp4" and value["byte_length"] == 3220300
    assert value["sha256"] == "c29bea4b8e3c8a2d837aa463cb7a27116525a99cd40c1934c2f8e65528d9fbf7"
    assert value["expected_text"] == "elephants"
    assert value["evidence"] == ["cn_glm4v_plus_0111_official_video_sample"]
    assert "data_base64" not in value and "path" not in value
    with pytest.raises(ValueError, match="Unknown remote"):
        media.remote_video_fixture("unreviewed")


@pytest.mark.parametrize("field,value", [
    ("url", "https://127.0.0.1/video.mp4"), ("url", "http://sfile.chatglm.cn/testpath/video.mp4"),
    ("url", "https://sfile.chatglm.cn/testpath/video/b844f8f1-5df9-556c-a515-3d3bfaa736e8_0.mp4?changed=1"),
    ("mime_type", "video/quicktime"), ("sha256", "not-a-sha256"), ("byte_length", True),
    ("byte_length", 10_000_001), ("expected_text", ""), ("evidence", ["missing-source"]),
    ("documentation_url", None),
])
def test_invalid_remote_video_manifest_is_rejected(isolated_fixtures, field, value):
    path = isolated_fixtures / "remote_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["fixtures"]["zhipu_elephants"][field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(media.FixtureIntegrityError):
        media.remote_video_fixture("zhipu_elephants")
