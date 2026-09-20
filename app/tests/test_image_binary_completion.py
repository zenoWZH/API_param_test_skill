from __future__ import annotations

import io
import struct
import sys
import zlib
from types import SimpleNamespace

import pytest

from lib.image_validation import inspect_image_bytes


def _encoded(image_format: str) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (230, 80, 40)).save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_complete_binary_decode_is_required_without_visual_forensics(image_format):
    info = inspect_image_bytes(_encoded(image_format), visual_forensics=False)
    assert (info.format, info.width, info.height) == (image_format, 32, 24)
    assert info.visual_metrics["reason"] == "disabled"


def _png(scanlines: bytes, *, interlace: int, width: int = 32, height: int = 24) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, interlace))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def test_truncated_adam7_png_with_valid_chunks_is_rejected():
    with pytest.raises(ValueError, match="PNG image decoding failed"):
        inspect_image_bytes(_png(b"\0", interlace=1), visual_forensics=False)


def test_complete_adam7_png_is_accepted():
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8),
              (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    scanlines = b"".join(
        (b"\0" + b"\0" * (len(range(x, 32, dx)) * 3)) * len(range(y, 24, dy))
        for x, y, dx, dy in passes
    )
    info = inspect_image_bytes(_png(scanlines, interlace=1), visual_forensics=False)
    assert (info.format, info.width, info.height) == ("PNG", 32, 24)


def test_png_with_complete_scanlines_but_invalid_filter_is_rejected():
    scanlines = (b"\x05" + b"\0" * (32 * 3)) * 24
    with pytest.raises(ValueError, match="PNG image decoding failed"):
        inspect_image_bytes(_png(scanlines, interlace=0), visual_forensics=False)


def test_text_parameter_image_audit_cannot_pass_a_truncated_adam7_image():
    import base64
    from lib.token_audit import audit_exchange

    encoded = base64.b64encode(
        _png(b"\0", interlace=1, width=1024, height=1024)
    ).decode("ascii")
    body = {
        "contents": [{"parts": [{"text": "Draw a red square."}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    response = {"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}]
    }}]}
    audit = audit_exchange(
        body, SimpleNamespace(response_json=response, usage={
            "promptTokenCount": 20, "candidatesTokenCount": 1120,
        }),
        "gemini_generate_content", {}, "initial",
        model="gemini-3.1-flash-image", accounting_source_id="google_ai_studio",
    )
    assert audit["image_output_evidence"]["status"] == "fail"
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("image_format", ["JPEG", "WEBP"])
def test_missing_image_footer_is_rejected_even_when_header_is_readable(image_format):
    raw = _encoded(image_format)
    with pytest.raises(ValueError, match="truncated|decoding failed"):
        inspect_image_bytes(raw[:-2], visual_forensics=False)


def test_jpeg_sof_and_eoi_without_scan_data_is_not_a_complete_image():
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 24, 32)
    sof += b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    with pytest.raises(ValueError, match="decoding failed"):
        inspect_image_bytes(b"\xff\xd8" + sof + b"\xff\xd9", visual_forensics=False)


def test_webp_extended_header_without_image_data_is_not_complete():
    header = b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8X" + struct.pack("<I", 10)
    header += b"\0" * 4 + (31).to_bytes(3, "little") + (23).to_bytes(3, "little")
    with pytest.raises(ValueError, match="decoding failed"):
        inspect_image_bytes(header, visual_forensics=False)


def test_webp_truncated_payload_cannot_forge_a_shorter_riff_length():
    raw = _encoded("WEBP")[:-8]
    rewritten = raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:]
    with pytest.raises(ValueError, match="decoding failed"):
        inspect_image_bytes(rewritten, visual_forensics=False)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_missing_complete_decoder_fails_closed(image_format, monkeypatch):
    raw = _encoded(image_format)
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ValueError, match="validation requires Pillow"):
        inspect_image_bytes(raw, visual_forensics=False)


def test_global_truncation_tolerance_cannot_disable_required_validation(monkeypatch):
    from PIL import ImageFile
    raw = _encoded("JPEG")
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    with pytest.raises(ValueError, match="LOAD_TRUNCATED_IMAGES"):
        inspect_image_bytes(raw, visual_forensics=False)
