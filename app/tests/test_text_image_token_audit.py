from __future__ import annotations

import base64
import copy
import struct
import zlib
from functools import lru_cache
from types import SimpleNamespace

import pytest

from lib.token_audit import audit_exchange, audit_image_usage
from lib.image_token_expectations import image_output_token_expectation


@lru_cache
def _png() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"\x89PNG\r\n\x1a\n"
    raw += chunk(b"IHDR", struct.pack(">IIBBBBB", 1024, 1024, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff" * 1024) * 1024))
    raw += chunk(b"IEND", b"")
    return base64.b64encode(raw).decode("ascii")


def _native_image_audit(*, count=1, tokens=None, broken=False, model="gemini-3.1-flash-image", source="google_ai_studio"):
    body = {"contents": [{"parts": [{"text": "Draw a red square."}]}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "maxOutputTokens": 8192}}
    parts = [{"inlineData": {"mimeType": "image/png", "data": _png()}} for _ in range(count)]
    if broken:
        parts[-1]["inlineData"]["data"] = "AAAA"
    response = {"modelVersion": "gemini-3.1-flash-image", "candidates": [{"finishReason": "STOP", "content": {"parts": parts}}]}
    usage = {"promptTokenCount": 20, "candidatesTokenCount": count * 1120 if tokens is None else tokens}
    return audit_exchange(body, SimpleNamespace(response_json=response, usage=usage), "gemini_generate_content", {}, "initial", model=model, accounting_source_id=source)


@pytest.mark.parametrize("count", [1, 2])
def test_text_matrix_image_output_uses_all_decoded_images_and_official_range(count):
    audit = _native_image_audit(count=count)
    assert audit["validation_pass"] is True
    assert audit["image_output_evidence"]["decoded_count"] == count
    assert audit["gross_plausibility"]["output"]["expectation"]["tokens"] == count * 1120
    assert audit["output_accuracy"]["status"] == "not_available"


@pytest.mark.parametrize("kwargs", [{"tokens": 5000}, {"count": 2, "tokens": 1120}, {"count": 2, "broken": True}, {"model": "gemini-3.7-flash"}, {"source": "google_ai_studio_untrusted"}])
def test_text_matrix_image_output_never_passes_bad_counts_bytes_or_unbound_formula(kwargs):
    assert _native_image_audit(**kwargs)["validation_pass"] is False


def test_generated_model_name_cannot_replace_requested_text_model_for_formula():
    audit = _native_image_audit(model="gemini-3.7-flash")
    assert audit["image_output_evidence"]["status"] == "pass"
    assert audit["gross_plausibility"]["output"]["status"] == "not_available"


def test_remote_image_in_text_matrix_is_unverified_without_downloading():
    response = {"choices": [{"finish_reason": "stop", "message": {"images": [{"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}]}}]}
    audit = audit_exchange({"messages": [{"role": "user", "content": "draw"}]}, SimpleNamespace(response_json=response, usage={"prompt_tokens": 10, "completion_tokens": 1120}), "chat_completions", {}, "initial", model="gemini-3.1-flash-image", accounting_source_id="google_ai_studio")
    assert audit["image_output_evidence"]["remote_count"] == 1
    assert audit["validation_pass"] is False


def test_thought_images_are_not_final_generated_artifacts():
    response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"thought": True, "inlineData": {"mimeType": "image/png", "data": _png()}}, {"text": "OK"}]}}]}
    body = {"contents": [{"parts": [{"text": "Draw a square."}]}], "generationConfig": {"responseModalities": ["IMAGE"]}}
    audit = audit_exchange(body, SimpleNamespace(response_json=response, usage={"promptTokenCount": 20, "candidatesTokenCount": 2}), "gemini_generate_content", {}, "initial", model="gemini-3.1-flash-image", accounting_source_id="google_ai_studio")
    assert audit["image_output_evidence"]["artifact_count"] == 0
    assert audit["validation_pass"] is False


def test_unsolicited_claude_thinking_summary_without_breakdown_remains_unverified():
    response = {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "Brief summary."}, {"type": "text", "text": "OK"}]}
    audit = audit_exchange({"messages": [{"role": "user", "content": "Say OK"}]}, SimpleNamespace(response_json=response, usage={"input_tokens": 10, "output_tokens": 12}), "claude_messages", {}, "initial")
    assert audit["gross_plausibility"]["output"]["status"] == "partial"
    assert audit["validation_pass"] is False


def test_audio_output_cannot_pass_as_tiny_visible_text():
    response = {"choices": [{"finish_reason": "stop", "message": {"content": "OK", "audio": {"data": "AAAA"}}}]}
    audit = audit_exchange({"messages": [{"role": "user", "content": "hello"}]}, SimpleNamespace(response_json=response, usage={"prompt_tokens": 10, "completion_tokens": 2}), "chat_completions", {}, "initial")
    assert audit["gross_plausibility"]["output"]["status"] == "not_available"
    assert audit["validation_pass"] is False


def test_tool_argument_named_audio_is_not_media_output():
    response = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "record", "arguments": {"type": "audio", "audio": {"data": "ordinary value"}}}}]}}]}
    audit = audit_exchange({"messages": [{"role": "user", "content": "record a category"}]}, SimpleNamespace(response_json=response, usage={"prompt_tokens": 10, "completion_tokens": 40}), "chat_completions", {}, "initial")
    assert audit["gross_plausibility"]["output"]["status"] == "pass"
    assert audit["validation_pass"] is True


def test_auto_quality_without_image_details_uses_approximate_discrete_envelopes():
    expectation = image_output_token_expectation("gpt-image-2", {"quality": "auto"}, [{"width": 1024, "height": 1024}], reference_source="openai")
    for count, passed in [(196, True), (1756, True), (7024, True), (4000, False), (16000, False)]:
        audit = audit_image_usage({"prompt": "Draw a square", "quality": "auto"}, {"data": [{"b64_json": _png()}]}, {"input_tokens": 10, "output_tokens": count}, {}, provider=None, model="gpt-image-2", image_output_expectation=copy.deepcopy(expectation))
        assert audit["validation_pass"] is passed
        quantity = audit["exchanges"][0]["gross_plausibility"]["output"]
        assert quantity["validation_scope"] == "official_image_token_estimate"
        assert quantity["exact_output_count_verified"] is False


def test_multiple_generated_images_missing_detail_count_uses_approximate_total():
    audit = _native_image_audit(count=2, tokens=2800)
    quantity = audit["gross_plausibility"]["output"]

    assert audit["image_output_evidence"]["decoded_count"] == 2
    assert quantity["expectation"]["tokens"] == 2240
    assert quantity["validation_scope"] == "official_image_token_estimate"
    assert quantity["exact_output_count_verified"] is False
    assert audit["validation_pass"] is True


def test_auto_quality_explicit_image_detail_still_rejects_source_range_gap():
    expectation = image_output_token_expectation("gpt-image-2", {"quality": "auto"}, [{"width": 1024, "height": 1024}], reference_source="openai")
    audit = audit_image_usage(
        {"prompt": "Draw a square", "quality": "auto"},
        {"data": [{"b64_json": _png()}]},
        {"input_tokens": 10, "output_tokens": 4000, "output_tokens_details": {"image_tokens": 4000}},
        {}, provider=None, model="gpt-image-2", image_output_expectation=expectation,
    )

    assert audit["exchanges"][0]["gross_plausibility"]["output"]["image_component"]["status"] == "fail"
    assert audit["validation_pass"] is False
