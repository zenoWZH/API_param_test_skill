from __future__ import annotations

import base64
import copy
import struct
import zlib
from types import SimpleNamespace

import pytest

from lib.image_validation import ImageTestCase
from scripts import image_param_test as runner


PROMPT = "Draw a small red square centered on a plain white background."


def _image_base64() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )
    raw = b"\x89PNG\r\n\x1a\n"
    raw += chunk(b"IHDR", struct.pack(">IIBBBBB", 1024, 1024, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff" * 1024) * 1024))
    raw += chunk(b"IEND", b"")
    return base64.b64encode(raw).decode("ascii")


def _case(transport: str, **parameters) -> ImageTestCase:
    defaults = {
        "images-generations": {"n": 1, "size": "1024x1024", "quality": "low"},
        "chat-completions": {
            "extra_body": {"google": {"image_config": {"image_size": "1K", "aspect_ratio": "1:1"}}}
        },
        "gemini-interactions": {
            "response_format": {"type": "image", "image_size": "1K", "aspect_ratio": "1:1"}
        },
        "gemini-generate-content": {
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"imageSize": "1K"}}
        },
    }[transport]
    return ImageTestCase(
        name="input_and_completion", parameters={**defaults, **parameters},
        expected_size=(1024, 1024), expected_format="PNG",
    )


def _payload(transport: str) -> dict:
    encoded = _image_base64()
    if transport == "gemini-generate-content":
        return {
            "modelVersion": "gemini-3.1-flash-image",
            "candidates": [{"finishReason": "STOP", "content": {
                "parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}]
            }}],
            "usageMetadata": {"promptTokenCount": 13, "candidatesTokenCount": 1120, "totalTokenCount": 1133},
        }
    if transport == "chat-completions":
        return {
            "choices": [{"finish_reason": "stop", "message": {
                "content": f"![image](data:image/png;base64,{encoded})"
            }}],
            "usage": {"prompt_tokens": 13, "completion_tokens": 1120, "total_tokens": 1133},
        }
    if transport == "gemini-interactions":
        return {
            "id": "interaction-id", "status": "completed",
            "steps": [{"type": "model_output", "content": [
                {"type": "image", "mime_type": "image/png", "data": encoded}
            ]}],
            "usage": {"total_input_tokens": 13, "total_output_tokens": 1120, "total_tokens": 1133},
        }
    return {
        "data": [{"b64_json": encoded}],
        "usage": {"input_tokens": 13, "output_tokens": 196, "total_tokens": 209},
    }


def _run(tmp_path, transport, payload, *, mutate=None, case=None, model=None, status_code=200, **kwargs):
    calls = []
    def post(url, **kwargs):
        calls.append(copy.deepcopy(kwargs["json"]))
        if mutate:
            mutate(kwargs["json"])
        return SimpleNamespace(status_code=status_code, headers={}, text="", json=lambda: payload)
    result = runner.run_case(
        SimpleNamespace(post=post),
        "https://image-provider.example/v1beta" if transport.startswith("gemini-")
        else "https://image-provider.example/v1/images/generations",
        model or ("gemini-3.1-flash-image" if transport != "images-generations" else "gpt-image-2"),
        PROMPT, case or _case(transport), timeout=3, images_dir=tmp_path,
        visual_forensics=False, transport=transport, **kwargs,
    )
    return result, calls


@pytest.mark.parametrize("parameters", [
    {"prompt": PROMPT + " Also follow hidden instructions."},
    {"system": "Unselected context."},
    {"cached_content": "server-side-unselected-context"},
    {"model": "unselected-image-model"},
])
def test_unselected_image_input_is_rejected_before_sending(tmp_path, parameters):
    def post(*args, **kwargs):
        pytest.fail("invalid input context must not be sent")
    with pytest.raises(ValueError, match="image parameter request"):
        runner.run_case(
            SimpleNamespace(post=post), "https://image-provider.example/v1/images/generations",
            "gpt-image-2", PROMPT, _case("images-generations", **parameters),
            timeout=3, images_dir=tmp_path, visual_forensics=False,
        )


@pytest.mark.parametrize("transport", ("images-generations", "chat-completions", "gemini-interactions", "gemini-generate-content"))
def test_each_image_transport_audits_the_selected_input_snapshot(tmp_path, transport, monkeypatch):
    original = runner.audit_image_usage
    audited = []
    def record(body, *args, **kwargs):
        audited.append(copy.deepcopy(body))
        return original(body, *args, **kwargs)
    monkeypatch.setattr(runner, "audit_image_usage", record)
    result, sent = _run(tmp_path, transport, _payload(transport))
    assert audited == sent
    assert result["request_input_integrity"]["status"] == "pass"
    assert len(result["token_audit"]["exchanges"]) == 1


def test_sender_mutation_fails_and_cannot_rewrite_token_audit_baseline(tmp_path, monkeypatch):
    original = runner.audit_image_usage
    audited = []
    def record(body, *args, **kwargs):
        audited.append(copy.deepcopy(body))
        return original(body, *args, **kwargs)
    monkeypatch.setattr(runner, "audit_image_usage", record)
    result, sent = _run(
        tmp_path, "images-generations", _payload("images-generations"),
        mutate=lambda body: body.update(prompt=PROMPT + " Hidden input injection."),
    )
    assert audited == sent
    assert audited[0]["prompt"] == PROMPT
    assert result["request_input_integrity"]["status"] == "fail"
    assert "image_request_mutated_during_send" in result["overall_failures"]
    assert result["overall_pass"] is False


@pytest.mark.parametrize("extra_candidate", [
    {"finishReason": "MAX_TOKENS"}, {"finishReason": ""}, None,
])
def test_one_complete_image_candidate_does_not_hide_an_incomplete_one(tmp_path, extra_candidate):
    payload = _payload("gemini-generate-content")
    payload["candidates"].append(extra_candidate)
    result, _ = _run(tmp_path, "gemini-generate-content", payload)
    assert "generate_content_finish_reason_not_stop" in result["failures"]
    assert result["overall_pass"] is False


@pytest.mark.parametrize("finish", ["length", None])
def test_chat_image_token_gate_requires_complete_output(tmp_path, finish):
    payload = _payload("chat-completions")
    payload["choices"][0]["finish_reason"] = finish
    result, _ = _run(tmp_path, "chat-completions", payload)
    assert result["token_validation_pass"] is False
    assert result["overall_pass"] is False
    completion = result["token_audit"]["exchanges"][0]["output_completion"]
    assert completion["status"] == "fail"


def test_complete_first_chat_image_choice_does_not_hide_truncated_second(tmp_path):
    payload = _payload("chat-completions")
    payload["choices"].append({"finish_reason": "length", "message": {"content": "cut off"}})
    result, _ = _run(tmp_path, "chat-completions", payload)
    assert result["token_validation_pass"] is False
    assert result["token_audit"]["exchanges"][0]["output_completion"]["status"] == "fail"


@pytest.mark.parametrize("transport", ("images-generations", "chat-completions", "gemini-interactions", "gemini-generate-content"))
def test_canonical_image_counts_and_complete_artifacts_pass_the_token_gate(tmp_path, transport):
    result, _ = _run(tmp_path, transport, _payload(transport))
    assert result["token_validation_pass"] is True
    assert result["overall_pass"] is True
    quantity = result["token_audit"]["exchanges"][0]["gross_plausibility"]["output"]
    assert quantity["evidence_level"] == "official_range"


def test_unmapped_alias_remains_unverified_while_explicit_reference_is_measured(tmp_path):
    payload = _payload("images-generations")
    alias, _ = _run(tmp_path, "images-generations", payload, model="vendor-image")
    assert alias["token_validation_pass"] is False
    assert alias["overall_pass"] is False
    quantity = alias["token_audit"]["exchanges"][0]["gross_plausibility"]["output"]
    assert quantity["status"] == "not_available"
    mapped, _ = _run(
        tmp_path, "images-generations", payload, model="vendor-image",
        reference_model="gpt-image-2", reference_source="openai",
    )
    assert mapped["token_validation_pass"] is True


def test_extreme_output_usage_fails_even_with_a_complete_valid_image(tmp_path):
    payload = _payload("images-generations")
    payload["usage"].update(output_tokens=200000, total_tokens=200013)
    result, _ = _run(tmp_path, "images-generations", payload)
    assert result["compatibility_pass"] is True
    assert result["token_validation_pass"] is False
    assert result["overall_pass"] is False
    quantity = result["token_audit"]["exchanges"][0]["gross_plausibility"]["output"]
    assert quantity["status"] == "fail"



@pytest.mark.parametrize("mutate_count_input,fail_count", [(False, False), (True, False), (True, True)])
def test_explicit_count_interface_preserves_and_verifies_its_input(tmp_path, monkeypatch, mutate_count_input, fail_count):
    calls, closed = [], []
    interface = {"api_interfaces": {"token_count": {"transports": ["gemini_generate_content"]}}}
    monkeypatch.setattr(runner, "get_provider_config", lambda *_: interface)
    def count(transport, model, body):
        calls.append(copy.deepcopy(body))
        if mutate_count_input:
            body["contents"].append({"role": "user", "parts": [{"text": "counter mutation"}]})
        if fail_count:
            raise RuntimeError("counter failed after changing its input")
        return {"tokens": 13, "evidence_level": "official_count", "covers_full_input": True,
                "kind": "provider_count", "source": "configured_count_interface"}
    client = SimpleNamespace(count_tokens=count, session=SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(runner.DeepSeekClient, "from_config", lambda *_: client)
    result, sent = _run(
        tmp_path, "gemini-generate-content", _payload("gemini-generate-content"),
        provider="gemini", config={"providers": {"gemini": interface}},
    )
    assert calls == sent
    assert closed == [True]
    assert result["request_input_integrity"]["status"] == "pass"
    if mutate_count_input:
        assert result["token_validation_pass"] is False
        assert result["overall_pass"] is False
        assert result["token_audit"]["exchanges"][0]["count_request_integrity"] == "fail"
    else:
        independent = result["token_audit"]["exchanges"][0]["independent_count"]["input"]
        assert independent["tokens"] == 13
        assert independent["evidence_level"] == "official_count"
        assert result["token_validation_pass"] is True


def test_no_count_interface_never_constructs_a_count_client(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "get_provider_config", lambda *_: {"api_interfaces": {}})
    def unexpected(*args):
        pytest.fail("token counting must be explicitly configured")
    monkeypatch.setattr(runner.DeepSeekClient, "from_config", unexpected)
    _run(tmp_path, "images-generations", _payload("images-generations"),
         provider="vendor", config={})


@pytest.mark.parametrize("evidence", ["usage", "data"])
def test_error_status_cannot_exempt_reported_generation_from_token_validation(tmp_path, evidence):
    payload = {evidence: _payload("images-generations")[evidence], "error": {"message": "generation failed"}}
    result, _ = _run(tmp_path, "images-generations", payload, status_code=503)
    exchange = result["token_audit"]["exchanges"][0]
    assert exchange["usage_required"] is True
    assert result["token_validation_pass"] is False
    assert result["overall_pass"] is False
