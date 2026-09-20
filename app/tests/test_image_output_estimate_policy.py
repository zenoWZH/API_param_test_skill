from __future__ import annotations

import pytest

from lib.token_audit import audit_image_usage, summarize_token_audits


TRANSPORTS = [
    "image_generation", "chat_completions", "openai_responses",
    "gemini_generate_content", "gemini_interactions",
]
EXPECTATION = {
    "tokens": 1120, "min": 1100, "max": 1140,
    "source": "https://ai.google.dev/gemini-api/docs/generate-content/image-generation",
    "evidence_level": "official_range", "component": "image",
}


def _audit(
    transport, *, output=1400, image_tokens=None, text_tokens=None,
    text="", extra_details=None, expectation=EXPECTATION,
    thoughts=None, hidden=False, output_limit=None,
):
    details = dict(extra_details or {})
    if image_tokens is not None:
        details["image_tokens"] = image_tokens
    if text_tokens is not None:
        details["text_tokens"] = text_tokens
    if transport == "image_generation":
        request = {"prompt": "Draw a square."}
        response = {"data": [{"b64_json": "AAAA"}]}
    elif transport == "chat_completions":
        request = {"messages": [{"role": "user", "content": "Draw a square."}]}
        response = {"choices": [{"finish_reason": "stop", "message": {
            "content": text,
            "images": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
        }}]}
    elif transport == "openai_responses":
        request = {"input": "Draw a square."}
        response = {"status": "completed", "output": [
            {"type": "image_generation_call", "status": "completed", "result": "AAAA"},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ]}
    elif transport == "gemini_generate_content":
        request = {"contents": [{"role": "user", "parts": [{"text": "Draw a square."}]}]}
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}, {"text": text},
        ]}}]}
    else:
        request = {"input": "Draw a square."}
        response = {"status": "completed", "steps": [{"type": "model_output", "content": [
            {"type": "image", "mime_type": "image/png", "data": "AAAA"},
            {"type": "text", "text": text},
        ]}]}

    if transport == "gemini_generate_content":
        usage = {
            "promptTokenCount": 10, "candidatesTokenCount": output,
            "totalTokenCount": 10 + output + (thoughts or 0),
            "candidatesTokensDetails": [
                {"modality": name.removesuffix("_tokens").upper(), "tokenCount": count}
                for name, count in details.items()
            ],
        }
        if thoughts is not None:
            usage["thoughtsTokenCount"] = thoughts
        response["usageMetadata"] = usage
    elif transport == "gemini_interactions":
        usage = {
            "total_input_tokens": 10, "total_output_tokens": output,
            "total_tokens": 10 + output + (thoughts or 0),
            "output_tokens_by_modality": [
                {"modality": name.removesuffix("_tokens"), "tokens": count}
                for name, count in details.items()
            ],
        }
        if thoughts is not None:
            usage["total_thought_tokens"] = thoughts
        response["usage"] = usage
    elif transport == "chat_completions":
        usage = {"prompt_tokens": 10, "completion_tokens": output,
                 "total_tokens": 10 + output, "completion_tokens_details": details}
        if thoughts is not None:
            details["reasoning_tokens"] = thoughts
    else:
        usage = {"input_tokens": 10, "output_tokens": output,
                 "total_tokens": 10 + output, "output_tokens_details": details}
        if thoughts is not None:
            details["reasoning_tokens"] = thoughts
    if hidden:
        request["reasoning"] = {"effort": "high"}
    if output_limit is not None:
        request["max_output_tokens"] = output_limit
    return audit_image_usage(
        request, response, usage, {}, provider=None, model="image-model",
        transport=transport, image_output_expectation=expectation,
    )


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("image_tokens", [None, 1120])
def test_image_transports_allow_reasonable_approximation_without_claiming_exact_count(transport, image_tokens):
    audit = _audit(transport, image_tokens=image_tokens, hidden=True)
    exchange = audit["exchanges"][0]
    quantity = exchange["gross_plausibility"]["output"]

    assert quantity["status"] == "pass"
    assert quantity["validation_scope"] == "official_image_token_estimate"
    assert quantity["exact_output_count_verified"] is False
    assert quantity["output_estimate"]["reported_tokens"] == 1400
    assert quantity["output_estimate"]["nominal_image_tokens"] == [1120]
    assert exchange["gross_plausibility"]["input"]["evidence_level"] == "estimate"
    assert exchange["gross_plausibility"]["input"]["status"] == "pass"
    assert exchange["output_accuracy"]["status"] == "not_available"
    assert exchange["usage_accounting"]["thinking_source"] is None
    assert audit["validation_pass"] is True
    assert summarize_token_audits([{"status_code": 200, "token_audit": audit}])["pass"] is True


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("output", [500, 1099, 2257, 3000])
def test_image_transports_reject_output_outside_approximate_envelope(transport, output):
    audit = _audit(transport, output=output)

    assert audit["exchanges"][0]["gross_plausibility"]["output"]["status"] == "fail"
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("output", [1100, 2256])
def test_image_transports_accept_inclusive_source_floor_and_rough_upper_bound(transport, output):
    audit = _audit(transport, output=output)
    estimate = audit["exchanges"][0]["gross_plausibility"]["output"]["output_estimate"]

    assert estimate["minimum_plausible_tokens"] == 1100
    assert estimate["maximum_plausible_tokens"] == 2256
    assert audit["validation_pass"] is True


@pytest.mark.parametrize("transport", TRANSPORTS[1:])
def test_image_transports_include_visible_text_in_approximate_output(transport):
    audit = _audit(transport, image_tokens=1120, text="OK", text_tokens=2)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["text_component"]["status"] == "pass"
    assert quantity["text_component"]["reported_tokens"] == 2
    assert quantity["output_estimate"]["visible_text_estimate"] > 0
    assert quantity["output_estimate"]["maximum_plausible_tokens"] > 2256
    assert quantity["exact_output_count_verified"] is False
    assert audit["validation_pass"] is True


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_image_transports_keep_explicit_image_detail_source_range_strict(transport):
    audit = _audit(transport, output=1500, image_tokens=1400)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["image_component"]["status"] == "fail"
    assert quantity["output_estimate"]["status"] == "pass"
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("extra_details", [{"text_tokens": -1}, {"audio_tokens": 30}, {"text_tokens": 200}])
def test_image_transports_do_not_approximate_away_bad_or_unverified_details(transport, extra_details):
    audit = _audit(transport, image_tokens=1120, extra_details=extra_details)

    assert audit["validation_pass"] is False


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_image_transports_cannot_pass_without_official_expectation(transport):
    audit = _audit(transport, expectation=None)

    assert audit["exchanges"][0]["gross_plausibility"]["output"]["status"] == "not_available"
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_image_transports_cannot_bypass_output_limit_with_approximate_pass(transport):
    audit = _audit(transport, image_tokens=1120, output_limit=1300)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["request_limit_tokens"] == 1300
    assert quantity["status"] == "fail"
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("transport", ["gemini_generate_content", "gemini_interactions", "openai_responses"])
def test_image_approximation_subtracts_only_authoritative_thought_counts(transport):
    reported = 6220 if transport == "openai_responses" else 1220
    audit = _audit(transport, output=reported, image_tokens=1120, thoughts=5000)
    exchange = audit["exchanges"][0]

    assert exchange["usage_accounting"]["output_tokens"] == 6220
    assert exchange["gross_plausibility"]["output"]["output_estimate"]["reported_tokens"] == 1220
    assert audit["validation_pass"] is True


def test_image_approximation_cannot_subtract_advisory_chat_reasoning():
    audit = _audit("chat_completions", output=6220, image_tokens=1120, thoughts=5000)
    exchange = audit["exchanges"][0]

    assert exchange["usage_accounting"]["thinking_source"] is None
    assert exchange["gross_plausibility"]["output"]["output_estimate"]["reported_tokens"] == 6220
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("transport", ["gemini_generate_content", "gemini_interactions", "openai_responses"])
def test_image_output_cap_still_includes_authoritative_thought_counts(transport):
    reported = 6220 if transport == "openai_responses" else 1220
    audit = _audit(transport, output=reported, image_tokens=1120, thoughts=5000, output_limit=6000)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["output_estimate"]["status"] == "pass"
    assert quantity["request_limit_tokens"] == 6000
    assert quantity["status"] == "fail"
    assert audit["validation_pass"] is False
