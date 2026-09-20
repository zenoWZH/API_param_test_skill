from __future__ import annotations

import pytest

from lib.token_audit import audit_image_usage, summarize_token_audits


EXPECTATION = {
    "tokens": 1120,
    "min": 1100,
    "max": 1140,
    "source": "https://ai.google.dev/gemini-api/docs/generate-content/image-generation",
    "evidence_level": "official_range",
    "component": "image",
}


def _audit(
    candidates=1120, *, image_tokens=1120, text_tokens=None, thoughts=None,
    text="", extra_modalities=(), total_delta=0, expectation=None, output_limit=None,
    thinking_requested=False,
):
    details = [] if image_tokens is None else [{"modality": "IMAGE", "tokenCount": image_tokens}]
    if text_tokens is not None:
        details.append({"modality": "TEXT", "tokenCount": text_tokens})
    details.extend(
        {"modality": modality, "tokenCount": count}
        for modality, count in extra_modalities
    )
    usage = {
        "promptTokenCount": 10,
        "candidatesTokenCount": candidates,
        "candidatesTokensDetails": details,
        "totalTokenCount": 10 + candidates + (thoughts or 0) + total_delta,
    }
    if thoughts is not None:
        usage["thoughtsTokenCount"] = thoughts
    parts = [{
        "inlineData": {"mimeType": "image/png", "data": "AAAA"},
        "thoughtSignature": "opaque-signature",
    }]
    if text:
        parts.insert(0, {"text": text})
    request = {"contents": [{"role": "user", "parts": [{"text": "Draw a square."}]}]}
    if output_limit is not None:
        request["generationConfig"] = {"maxOutputTokens": output_limit}
    if thinking_requested:
        request.setdefault("generationConfig", {})["thinkingConfig"] = {"thinkingBudget": 1024}
    return audit_image_usage(
        request,
        {"candidates": [{"finishReason": "STOP", "content": {"parts": parts}}],
         "usageMetadata": usage},
        usage, {}, provider=None, model="gemini-3.1-flash-image",
        transport="gemini_generate_content",
        image_output_expectation=expectation or EXPECTATION,
    )


@pytest.mark.parametrize("candidates,thoughts,residual", [
    (1220, 197, 100),
    (1253, 209, 133),
    (1524, None, 404),
    (1612, None, 492),
    (1121, None, 1),
])
def test_native_image_reasonable_residual_passes_approximate_quantity(candidates, thoughts, residual):
    audit = _audit(candidates, thoughts=thoughts)
    exchange = audit["exchanges"][0]
    quantity = exchange["gross_plausibility"]["output"]

    assert exchange["usage_arithmetic"]["status"] == "pass"
    assert quantity["image_component"]["status"] == "pass"
    assert quantity["candidate_breakdown"]["unclassified_tokens"] == residual
    assert quantity["candidate_breakdown"]["status"] == "partial"
    assert quantity["output_estimate"]["reported_tokens"] == candidates
    assert quantity["output_estimate"]["nominal_image_tokens"] == [1120]
    assert quantity["output_estimate"]["visible_text_estimate"] == 0
    assert quantity["text_component"]["reported_tokens"] is None
    assert quantity["text_component"]["status"] == "not_available"
    assert quantity["status"] == "pass"
    assert quantity["validation_scope"] == "official_image_token_estimate"
    assert quantity["exact_output_count_verified"] is False
    assert exchange["output_accuracy"]["status"] == "not_available"
    assert exchange["validation_pass"] is True
    assert audit["validation_pass"] is True
    assert summarize_token_audits([{"status_code": 200, "token_audit": audit}])["pass"] is True
    if thoughts is None:
        assert exchange["usage_accounting"]["thinking_source"] is None
        assert "thoughtsTokenCount" not in exchange["usage_accounting"]["raw_usage"]


def test_native_image_extreme_residual_fails_approximate_quantity():
    audit = _audit(101120)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["image_component"]["status"] == "pass"
    assert quantity["candidate_breakdown"]["unclassified_tokens"] == 100000
    assert quantity["candidate_breakdown"]["status"] == "partial"
    assert quantity["status"] == "fail"
    assert quantity["exact_output_count_verified"] is False
    assert audit["validation_pass"] is False


def test_native_image_fully_assigned_candidates_do_not_invent_text_usage():
    audit = _audit()
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["candidate_breakdown"]["unclassified_tokens"] == 0
    assert quantity["text_component"]["reported_tokens"] is None
    assert quantity["text_component"]["status"] == "not_applicable"
    assert quantity["status"] == "pass"
    assert quantity["validation_scope"] == "official_image_token_estimate"
    assert quantity["exact_output_count_verified"] is False
    assert audit["validation_pass"] is True


@pytest.mark.parametrize("candidates,output_limit,status", [
    (1120, 1000, "fail"),
    (1120, 1120, "pass"),
    (1120, 1200, "pass"),
    (1220, 1300, "pass"),
    (1220, 1200, "fail"),
])
def test_native_image_details_cannot_bypass_requested_output_limit(candidates, output_limit, status):
    audit = _audit(candidates, output_limit=output_limit)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["image_component"]["status"] == "pass"
    assert quantity["candidate_breakdown"]["unclassified_tokens"] == candidates - 1120
    assert quantity["request_limit_tokens"] == output_limit
    assert quantity["status"] == status
    assert audit["validation_pass"] is (status == "pass")
    if candidates > output_limit:
        assert quantity["note"] == "reported image output tokens exceed the requested output-token limit"


@pytest.mark.parametrize("residual", [0, 100])
def test_native_image_text_uses_explicit_modality_count(residual):
    audit = _audit(1122 + residual, text_tokens=2, thoughts=197, text="OK")
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["text_component"]["reported_tokens"] == 2
    assert quantity["text_component"]["status"] == "pass"
    assert quantity["candidate_breakdown"]["unclassified_tokens"] == residual
    assert quantity["status"] == "pass"
    assert quantity["exact_output_count_verified"] is False
    assert audit["validation_pass"] is True


@pytest.mark.parametrize("candidates", [1120, 1140])
def test_native_image_visible_text_without_text_count_passes_only_approximate_quantity(candidates):
    audit = _audit(candidates, text="A red square.")
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["text_component"]["reported_tokens"] is None
    assert quantity["text_component"]["status"] == "not_available"
    assert quantity["status"] == "pass"
    assert quantity["validation_scope"] == "official_image_token_estimate"
    assert quantity["exact_output_count_verified"] is False
    assert audit["validation_pass"] is True


@pytest.mark.parametrize("image_tokens", [None, 1120])
@pytest.mark.parametrize("thinking_requested", [False, True])
def test_native_image_missing_breakdowns_can_pass_approximate_quantity(image_tokens, thinking_requested):
    audit = _audit(1524, image_tokens=image_tokens, text="A red square.", thinking_requested=thinking_requested)
    exchange = audit["exchanges"][0]
    quantity = exchange["gross_plausibility"]["output"]

    assert quantity["status"] == "pass"
    assert quantity["validation_scope"] == "official_image_token_estimate"
    assert quantity["exact_output_count_verified"] is False
    assert exchange["usage_accounting"]["thinking_source"] is None
    assert "thoughtsTokenCount" not in exchange["usage_accounting"]["raw_usage"]
    assert audit["validation_pass"] is True


def test_native_image_authoritative_thought_count_is_outside_candidate_estimate():
    audit = _audit(1220, thoughts=5000)
    exchange = audit["exchanges"][0]
    quantity = exchange["gross_plausibility"]["output"]

    assert exchange["usage_accounting"]["output_tokens"] == 6220
    assert quantity["output_estimate"]["reported_tokens"] == 1220
    assert quantity["status"] == "pass"
    assert quantity["exact_output_count_verified"] is False
    assert audit["validation_pass"] is True


@pytest.mark.parametrize("text_tokens,text", [(0, "OK"), (200, "")])
def test_native_image_explicit_text_contradictions_still_fail(text_tokens, text):
    audit = _audit(1120 + text_tokens, text_tokens=text_tokens, text=text)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["image_component"]["status"] == "pass"
    assert quantity["text_component"]["status"] == "fail"
    assert quantity["status"] == "fail"
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("candidates,text_tokens", [(1119, None), (1125, 10)])
def test_native_image_components_cannot_exceed_candidates_even_with_thought_tokens(candidates, text_tokens):
    audit = _audit(candidates, text_tokens=text_tokens, thoughts=197)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["candidate_breakdown"]["unclassified_tokens"] < 0
    assert quantity["candidate_breakdown"]["status"] == "fail"
    assert quantity["status"] == "fail"
    assert audit["validation_pass"] is False


def test_native_image_range_mismatch_is_failure_even_with_unclassified_tokens():
    audit = _audit(1500, image_tokens=1400)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["image_component"]["status"] == "fail"
    assert quantity["candidate_breakdown"]["status"] == "partial"
    assert quantity["status"] == "fail"
    assert audit["validation_pass"] is False


def test_native_image_discrete_range_gap_cannot_pass_component_check():
    expectation = {
        **EXPECTATION, "min": 700, "max": 1200,
        "ranges": [{"min": 700, "max": 800}, {"min": 1100, "max": 1200}],
    }
    audit = _audit(950, image_tokens=900, expectation=expectation)
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["image_component"]["status"] == "fail"
    assert quantity["status"] == "fail"
    assert audit["validation_pass"] is False


def test_native_image_additional_modality_requires_its_own_quantity_evidence():
    audit = _audit(1150, extra_modalities=[("AUDIO", 30)])
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["candidate_breakdown"]["unclassified_tokens"] == 0
    assert quantity["candidate_breakdown"]["unverified_modalities"] == ["audio_tokens"]
    assert quantity["candidate_breakdown"]["status"] == "partial"
    assert quantity["status"] in {"partial", "not_available"}
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("ranges", [None, [], [{"min": -1, "max": 1200}], [{"min": 1200, "max": 1100}]])
def test_native_image_invalid_ranges_cannot_claim_a_quantity_pass(ranges):
    audit = _audit(expectation={**EXPECTATION, "ranges": ranges})
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["status"] == "not_available"
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("kwargs", [{"text_tokens": -1}, {"total_delta": 1}])
def test_native_image_residual_classification_does_not_relax_usage_arithmetic(kwargs):
    audit = _audit(**kwargs)
    exchange = audit["exchanges"][0]

    assert exchange["usage_arithmetic"]["status"] == "fail"
    assert exchange["validation_pass"] is False
    assert audit["validation_pass"] is False


@pytest.mark.parametrize("nominal", [0, -1, 100000])
def test_native_image_invalid_nominal_cannot_expand_approximate_envelope(nominal):
    audit = _audit(1220, expectation={**EXPECTATION, "tokens": nominal})
    quantity = audit["exchanges"][0]["gross_plausibility"]["output"]

    assert quantity["status"] == "not_available"
    assert audit["validation_pass"] is False
