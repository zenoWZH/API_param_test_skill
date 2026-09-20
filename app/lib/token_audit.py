from __future__ import annotations

import json
import math
import re
import base64
import binascii
from types import SimpleNamespace
from typing import Any, TypedDict

from .token_counter import count_semantic_tokens


DEFAULT_RELATIVE_TOLERANCE = 0.50
DEFAULT_INPUT_ABSOLUTE_TOLERANCE = 16
DEFAULT_OUTPUT_ABSOLUTE_TOLERANCE = 8
DEFAULT_GROSS_INPUT_RATIO = 2.0
DEFAULT_GROSS_OUTPUT_RATIO = 2.0
DEFAULT_GROSS_INPUT_ABSOLUTE_TOLERANCE = 32
DEFAULT_GROSS_OUTPUT_ABSOLUTE_TOLERANCE = 16
DEFAULT_GROSS_OUTPUT_LIMIT_MULTIPLIER = 1.0
TOKEN_AUDIT_SCHEMA_VERSION = 4
XAI_ADDITIVE_CHAT_REASONING_CONTRACTS = frozenset({"grok_chat_completions"})
OPENAI_INCLUSIVE_CHAT_REASONING_CONTRACTS = frozenset({
    "openai_chat_base", "openai_gpt5_chat", "openai_gpt56_chat", "openai_gpt6_astra_chat",
})


class TokenUsage(TypedDict, total=False):
    transport: str
    input_tokens: int | None
    input_primary_tokens: int | None
    answer_tokens: int | None
    thinking_tokens: int | None
    rejected_prediction_tokens: int | None
    image_tokens: int | None
    image_token_scope: str | None
    output_tokens: int | None
    total_tokens: int | None
    provider_total_tokens: int | None
    cache_tokens: int
    cache_tokens_present: bool
    tool_use_tokens: int | None
    tool_use_source: str | None
    input_source: str | None
    output_source: str | None
    thinking_source: str | None
    details_advisory: dict[str, int]
    input_details: dict[str, int]
    output_details: dict[str, int]
    input_image_tokens: int | None
    output_image_tokens: int | None
    errors: list[str]
    raw_usage: dict[str, Any]


def normalize_usage(
    usage: dict[str, Any] | None,
    transport: str | None,
    *,
    additive_chat_reasoning: bool = False,
    inclusive_chat_reasoning: bool = False,
) -> TokenUsage:
    """Normalize provider usage without double-counting reasoning tokens.

    Chat-completions transports normally trust prompt_tokens and
    completion_tokens only. input_tokens/output_tokens and details fields are
    retained as advisory diagnostics because compatible gateways do not apply
    those fields consistently.  The documented xAI additive-reasoning layout
    is enabled only by an exact MPDB source binding supplied by the caller.
    """

    raw = usage if isinstance(usage, dict) else {}
    mode = str(transport or "generic")
    errors: list[str] = []

    if mode == "gemini_interactions":
        input_tokens = _first_int(raw, "total_input_tokens")
        answer_tokens = _first_int(raw, "total_output_tokens")
        thinking_tokens = _first_int(raw, "total_thought_tokens")
        output_tokens = (
            (answer_tokens or 0) + (thinking_tokens or 0)
            if answer_tokens is not None or thinking_tokens is not None
            else None
        )
        provider_total = _first_int(raw, "total_tokens")
        calculated_total = _sum_if_all(input_tokens, output_tokens)
        cache_tokens = _first_int(raw, "total_cached_tokens") or 0
        tool_use_tokens = _first_int(raw, "total_tool_use_tokens")
        payload = _normalized_payload(
            transport=mode,
            input_tokens=input_tokens,
            input_primary_tokens=input_tokens,
            answer_tokens=answer_tokens,
            thinking_tokens=thinking_tokens,
            output_tokens=output_tokens,
            calculated_total=calculated_total,
            provider_total=provider_total,
            cache_tokens=cache_tokens,
            input_source="usage.total_input_tokens" if input_tokens is not None else None,
            output_source=(
                "usage.total_output_tokens + total_thought_tokens"
                if answer_tokens is not None and thinking_tokens is not None
                else "usage.total_output_tokens"
                if answer_tokens is not None
                else "usage.total_thought_tokens"
                if thinking_tokens is not None
                else None
            ),
            thinking_source="usage.total_thought_tokens"
            if thinking_tokens is not None
            else None,
            details_advisory={},
            errors=errors,
        )
        payload["tool_use_tokens"] = tool_use_tokens
        payload["tool_use_source"] = (
            "usage.total_tool_use_tokens" if tool_use_tokens is not None else None
        )
        return _with_raw_usage(payload, raw)

    if mode == "gemini_generate_content" or _looks_like_gemini_usage(raw):
        input_tokens = _first_int(raw, "promptTokenCount", "prompt_token_count")
        answer_tokens = _first_int(raw, "candidatesTokenCount", "candidates_token_count")
        thinking_tokens = _first_int(raw, "thoughtsTokenCount", "thoughts_token_count")
        tool_use_tokens = _first_int(
            raw, "toolUsePromptTokenCount", "tool_use_prompt_token_count"
        )
        thinking_tokens = thinking_tokens if thinking_tokens is not None else 0
        output_tokens = (
            (answer_tokens or 0) + thinking_tokens
            if answer_tokens is not None or thinking_tokens
            else None
        )
        provider_total = _first_int(raw, "totalTokenCount", "total_token_count", "total_tokens")
        calculated_total = _sum_if_all(input_tokens, output_tokens)
        cache_tokens = _first_int(raw, "cachedContentTokenCount", "cached_content_token_count") or 0
        payload = _normalized_payload(
            transport=mode,
            input_tokens=input_tokens,
            input_primary_tokens=input_tokens,
            answer_tokens=answer_tokens,
            thinking_tokens=thinking_tokens,
            output_tokens=output_tokens,
            calculated_total=calculated_total,
            provider_total=provider_total,
            cache_tokens=cache_tokens,
            input_source="usageMetadata.promptTokenCount" if input_tokens is not None else None,
            output_source="usageMetadata.candidatesTokenCount + thoughtsTokenCount"
            if output_tokens is not None
            else None,
            thinking_source="usageMetadata.thoughtsTokenCount"
            if _first_int(raw, "thoughtsTokenCount", "thoughts_token_count") is not None
            else None,
            details_advisory={},
            errors=errors,
        )
        # GenerateContent documents tool-use prompt tokens as a separate
        # diagnostic field.  totalTokenCount is prompt + candidates + thoughts,
        # so tool-use tokens must not be added a second time.
        payload["tool_use_tokens"] = tool_use_tokens
        payload["tool_use_source"] = (
            "usageMetadata.toolUsePromptTokenCount"
            if tool_use_tokens is not None
            else None
        )
        return _with_raw_usage(payload, raw)

    if mode == "claude_messages":
        primary_input = _first_int(raw, "input_tokens")
        cache_creation = _first_int(raw, "cache_creation_input_tokens") or 0
        cache_read = _first_int(raw, "cache_read_input_tokens") or 0
        cache_tokens = cache_creation + cache_read
        input_tokens = (
            (primary_input or 0) + cache_tokens
            if primary_input is not None or cache_tokens
            else None
        )
        output_tokens = _first_int(raw, "output_tokens")
        thinking_tokens = _first_int(raw, "thinking_tokens", "reasoning_tokens")
        if thinking_tokens is None:
            thinking_tokens = _nested_first_int(
                raw,
                ("output_tokens_details", "thinking_tokens"),
                ("output_tokens_details", "reasoning_tokens"),
            )
        if thinking_tokens is not None and output_tokens is not None and thinking_tokens > output_tokens:
            errors.append("thinking tokens exceed output_tokens")
        answer_tokens = (
            max(output_tokens - thinking_tokens, 0)
            if output_tokens is not None and thinking_tokens is not None
            else None
        )
        provider_total = _first_int(raw, "total_tokens")
        calculated_total = _sum_if_all(input_tokens, output_tokens)
        return _with_raw_usage(_normalized_payload(
            transport=mode,
            input_tokens=input_tokens,
            input_primary_tokens=primary_input,
            answer_tokens=answer_tokens,
            thinking_tokens=thinking_tokens,
            output_tokens=output_tokens,
            calculated_total=calculated_total,
            provider_total=provider_total,
            cache_tokens=cache_tokens,
            input_source="usage.input_tokens + cache usage" if input_tokens is not None else None,
            output_source="usage.output_tokens" if output_tokens is not None else None,
            thinking_source="usage thinking/reasoning token detail"
            if thinking_tokens is not None
            else None,
            details_advisory={},
            errors=errors,
        ), raw)

    if mode == "openai_responses":
        input_tokens = _first_int(raw, "input_tokens")
        output_tokens = _first_int(raw, "output_tokens")
        thinking_tokens = _nested_first_int(
            raw,
            ("output_tokens_details", "reasoning_tokens"),
            ("output_tokens_details", "thinking_tokens"),
        )
        cache_tokens = _nested_first_int(raw, ("input_tokens_details", "cached_tokens")) or 0
        answer_tokens = (
            max(output_tokens - thinking_tokens, 0)
            if output_tokens is not None and thinking_tokens is not None
            else None
        )
        provider_total = _first_int(raw, "total_tokens")
        calculated_total = _sum_if_all(input_tokens, output_tokens)
        return _with_raw_usage(_normalized_payload(
            transport=mode,
            input_tokens=input_tokens,
            input_primary_tokens=input_tokens,
            answer_tokens=answer_tokens,
            thinking_tokens=thinking_tokens,
            output_tokens=output_tokens,
            calculated_total=calculated_total,
            provider_total=provider_total,
            cache_tokens=cache_tokens,
            input_source="usage.input_tokens" if input_tokens is not None else None,
            output_source="usage.output_tokens" if output_tokens is not None else None,
            thinking_source="usage.output_tokens_details.reasoning_tokens"
            if thinking_tokens is not None
            else None,
            details_advisory={},
            errors=errors,
        ), raw)

    if mode in {"image_generation", "images-generations"}:
        input_tokens = _first_int(raw, "input_tokens", "prompt_tokens")
        output_tokens = _first_int(raw, "output_tokens", "completion_tokens")
        provider_total = _first_int(raw, "total_tokens")
        calculated_total = _sum_if_all(input_tokens, output_tokens)
        return _with_raw_usage(
            _normalized_payload(
                transport=mode,
                input_tokens=input_tokens,
                input_primary_tokens=input_tokens,
                answer_tokens=None,
                thinking_tokens=None,
                output_tokens=output_tokens,
                calculated_total=calculated_total,
                provider_total=provider_total,
                cache_tokens=_openai_cached_tokens(raw),
                input_source=(
                    "usage.input_tokens/prompt_tokens"
                    if input_tokens is not None
                    else None
                ),
                output_source=(
                    "usage.output_tokens/completion_tokens"
                    if output_tokens is not None
                    else None
                ),
                thinking_source=None,
                details_advisory={},
                errors=errors,
            ),
            raw,
        )

    prompt_tokens = _first_int(raw, "prompt_tokens")
    completion_tokens = _first_int(raw, "completion_tokens")
    advisory_input = _first_int(raw, "input_tokens")
    advisory_output = _first_int(raw, "output_tokens")
    advisory_reasoning = _nested_first_int(
        raw,
        ("completion_tokens_details", "reasoning_tokens"),
        ("output_tokens_details", "reasoning_tokens"),
        ("output_tokens_details", "thinking_tokens"),
    )
    rejected_prediction = _nested_first_int(raw, ("completion_tokens_details", "rejected_prediction_tokens"))

    if mode == "chat_completions":
        input_tokens = prompt_tokens
        # xAI's Chat Completions contract reports reasoning_tokens outside
        # completion_tokens.  That alternate accounting is enabled only when
        # the caller has an exact MPDB source binding to xAI; an arbitrary
        # compatible gateway cannot opt itself into the exception via usage.
        if additive_chat_reasoning:
            thinking_tokens = advisory_reasoning
            answer_tokens = completion_tokens
            output_tokens = (
                completion_tokens + (thinking_tokens or 0)
                if completion_tokens is not None
                else None
            )
        elif inclusive_chat_reasoning:
            thinking_tokens = advisory_reasoning
            if thinking_tokens is None and rejected_prediction is not None:
                thinking_tokens = 0
            answer_tokens = (
                max(completion_tokens - (thinking_tokens or 0) - (rejected_prediction or 0), 0)
                if completion_tokens is not None and (thinking_tokens is not None or rejected_prediction is not None)
                else None
            )
            output_tokens = completion_tokens
        else:
            thinking_tokens = None
            answer_tokens = None
            output_tokens = completion_tokens
        calculated_total = (
            prompt_tokens + output_tokens
            if prompt_tokens is not None and output_tokens is not None
            else None
        )
        provider_total = _first_int(raw, "total_tokens")
        payload = _with_raw_usage(_normalized_payload(
            transport=mode,
            input_tokens=input_tokens,
            input_primary_tokens=input_tokens,
            answer_tokens=answer_tokens,
            thinking_tokens=thinking_tokens,
            output_tokens=output_tokens,
            calculated_total=calculated_total,
            provider_total=provider_total,
            cache_tokens=_openai_cached_tokens(raw),
            input_source="usage.prompt_tokens" if input_tokens is not None else None,
            output_source=(
                "usage.completion_tokens + completion_tokens_details.reasoning_tokens"
                if output_tokens is not None and additive_chat_reasoning and thinking_tokens is not None
                else "usage.completion_tokens"
                if output_tokens is not None
                else None
            ),
            thinking_source=(
                "usage.completion_tokens_details.reasoning_tokens"
                if thinking_tokens is not None
                else None
            ),
            details_advisory={
                key: value
                for key, value in {
                    "input_tokens": advisory_input,
                    "output_tokens": advisory_output,
                    "reasoning_tokens": advisory_reasoning,
                    "provider_total_tokens": provider_total,
                }.items()
                if value is not None
            },
            errors=errors,
        ), raw)
        if inclusive_chat_reasoning and rejected_prediction is not None:
            payload["rejected_prediction_tokens"] = rejected_prediction
        return payload

    # Generic callers keep backward compatibility while preferring the
    # prompt/completion pair whenever it is present.
    input_tokens = prompt_tokens if prompt_tokens is not None else advisory_input
    output_tokens = completion_tokens if completion_tokens is not None else advisory_output
    thinking_tokens = advisory_reasoning
    if thinking_tokens is not None and output_tokens is not None and thinking_tokens > output_tokens:
        errors.append("thinking tokens exceed output tokens")
    answer_tokens = (
        max(output_tokens - (thinking_tokens or 0), 0)
        if output_tokens is not None
        else None
    )
    provider_total = _first_int(raw, "total_tokens", "totalTokenCount", "total_token_count")
    calculated_total = _sum_if_all(input_tokens, output_tokens)
    return _with_raw_usage(_normalized_payload(
        transport=mode,
        input_tokens=input_tokens,
        input_primary_tokens=input_tokens,
        answer_tokens=answer_tokens,
        thinking_tokens=thinking_tokens,
        output_tokens=output_tokens,
        calculated_total=calculated_total,
        provider_total=provider_total,
        cache_tokens=_openai_cached_tokens(raw),
        input_source="usage.prompt_tokens/input_tokens" if input_tokens is not None else None,
        output_source="usage.completion_tokens/output_tokens" if output_tokens is not None else None,
        thinking_source="usage details" if thinking_tokens is not None else None,
        details_advisory={},
        errors=errors,
    ), raw)


def estimate_token_count(value: Any) -> int:
    text = _semantic_text(value)
    if not text:
        return 0
    units = 0.0
    for char in text:
        if ord(char) >= 128:
            units += 0.75
        elif char.isalpha() or char.isspace():
            units += 0.25
        else:
            units += 0.50
    return int(math.ceil(units))


def token_range(estimate: int, relative_tolerance: float, absolute_tolerance: int) -> dict[str, int]:
    delta = max(int(math.ceil(max(estimate, 0) * relative_tolerance)), int(absolute_tolerance))
    return {
        "min": max(int(estimate) - delta, 0),
        "max": int(estimate) + delta,
    }


def audit_exchange(
    request_body: dict[str, Any] | None,
    result: Any,
    transport: str,
    config: dict[str, Any],
    exchange: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    independent_input_count: dict[str, Any] | None = None,
    usage_required: bool = True,
    output_plausibility_supported: bool = True,
    accounting_source_id: str | None = None,
    accounting_contract_id: str | None = None,
    output_artifact_present: bool = False,
    image_output_expectation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    transport = _canonical_transport(transport)
    settings = _audit_settings(config)
    response_json = getattr(result, "response_json", None) or {}
    actual_image = _image_response_has_artifact(response_json, transport)
    automatic_image_audit = output_plausibility_supported and (
        actual_image or _request_expects_image(request_body or {})
    )
    image_output_evidence = None
    if automatic_image_audit:
        output_plausibility_supported = False
        output_artifact_present = actual_image
        image_output_evidence = _decoded_image_output_evidence(response_json, transport)
        if image_output_evidence["status"] == "pass":
            from .image_token_expectations import image_output_token_expectation

            image_output_expectation = image_output_token_expectation(
                str(model or (request_body or {}).get("model") or ""),
                request_body or {}, image_output_evidence["images"],
                reference_source=accounting_source_id,
            )
    xai_chat_accounting = (
        transport == "chat_completions"
        and str(accounting_source_id or "").strip().casefold() == "xai"
        and str(accounting_contract_id or "").strip()
        in XAI_ADDITIVE_CHAT_REASONING_CONTRACTS
    )
    usage = normalize_usage(
        getattr(result, "usage", None),
        transport,
        additive_chat_reasoning=xai_chat_accounting,
        inclusive_chat_reasoning=(
            transport == "chat_completions"
            and str(accounting_source_id or "").strip().casefold() == "openai"
            and str(accounting_contract_id or "").strip() in OPENAI_INCLUSIVE_CHAT_REASONING_CONTRACTS
        ),
    )
    usage_presence = _usage_presence(usage, required=usage_required)
    count_request_integrity = (
        str(independent_input_count.get("request_integrity") or "")
        if isinstance(independent_input_count, dict) else None
    )
    output_completion = _output_completion(
        result,
        transport,
        output_artifact_present=output_artifact_present,
    )
    if not settings["enabled"]:
        unavailable = {
            "status": "not_available",
            "note": "token accuracy audit is disabled",
        }
        validation_status = "fail" if usage_required or count_request_integrity == "fail" else "not_applicable"
        return {
            "schema_version": TOKEN_AUDIT_SCHEMA_VERSION,
            "exchange": exchange,
            "status": "not_available",
            "validation_status": validation_status,
            "validation_pass": validation_status != "fail",
            "validation_failures": (
                ["token audit is disabled for an exchange that requires usage validation"]
                if usage_required
                else []
            ),
            "usage_required": usage_required,
            "count_request_integrity": count_request_integrity,
            "input": dict(unavailable),
            "output": dict(unavailable),
            "reported": _reported_usage(usage),
            "independent_count": {},
            "usage_arithmetic": dict(unavailable),
            "usage_presence": usage_presence,
            "output_completion": output_completion,
            "gross_plausibility": {
                "status": validation_status,
                "input": dict(unavailable),
                "output": dict(unavailable),
            },
            "input_accuracy": dict(unavailable),
            "output_accuracy": dict(unavailable),
            "evidence_level": "unavailable",
            "usage_accounting": usage,
            "settings": settings,
        }
    input_semantic = _input_semantic_payload(request_body or {}, transport)
    output_semantic = _output_semantic_payload(getattr(result, "response_json", None) or {}, transport)

    input_estimate_value = _strip_opaque_media(input_semantic)
    input_estimate = estimate_token_count(input_estimate_value)
    input_expected = token_range(
        input_estimate,
        settings["relative_tolerance"],
        settings["input_absolute_tolerance"],
    )
    opaque_or_hidden_input = _request_has_external_context(request_body or {})
    # Cached tokens are normally a subset of the effective prompt, not an
    # excuse to compare only the uncached remainder.  Compare the complete
    # authoritative input count to the complete visible request.
    input_compared = usage.get("input_tokens")
    if not usage.get("input_source"):
        input_status = "not_available"
        input_note = "authoritative input usage is unavailable"
    else:
        input_status = _range_status(input_compared, input_expected)
        input_note = None
        if input_status == "fail" and input_compared is not None and input_compared > input_expected["max"]:
            if opaque_or_hidden_input:
                input_status = "partial"
                input_note = "reported input includes cached or server-side context that is not locally visible"
        elif input_status == "pass" and opaque_or_hidden_input:
            input_status = "partial"
            input_note = "visible input passed; cached or server-side context remains unverified"

    answer_estimate = estimate_token_count(output_semantic["answer"])
    reasoning_estimate = estimate_token_count(output_semantic["reasoning"])
    visible_output_estimate = answer_estimate + reasoning_estimate
    answer_expected = token_range(
        answer_estimate,
        settings["relative_tolerance"],
        settings["output_absolute_tolerance"],
    )
    reasoning_expected = token_range(
        reasoning_estimate,
        settings["relative_tolerance"],
        settings["output_absolute_tolerance"],
    )
    visible_total_expected = token_range(
        visible_output_estimate,
        settings["relative_tolerance"],
        settings["output_absolute_tolerance"],
    )

    thinking_requested = _thinking_requested(request_body or {})
    thinking_visibility = output_semantic["thinking_visibility"]
    advisory_reasoning = (usage.get("details_advisory") or {}).get("reasoning_tokens")
    thinking_detected = bool(
        thinking_requested
        or (usage.get("thinking_tokens") or 0) > 0
        or (advisory_reasoning or 0) > 0
        or thinking_visibility in {"visible", "summary", "hidden"}
    )
    hidden_thinking = (
        thinking_detected and thinking_visibility in {"none", "hidden", "summary"}
    ) or (usage.get("rejected_prediction_tokens") or 0) > 0
    short_reply = answer_estimate <= settings["output_absolute_tolerance"]
    total_status, output_note, total_expected = _output_status(
        usage=usage,
        answer_expected=answer_expected,
        visible_total_expected=visible_total_expected,
        hidden_thinking=hidden_thinking,
        short_reply=short_reply,
        thinking_visibility=thinking_visibility,
    )

    answer_status = "not_available"
    if usage.get("answer_tokens") is not None:
        answer_status = _range_status(usage["answer_tokens"], answer_expected)
        if answer_status == "fail" and usage["answer_tokens"] > answer_expected["max"] and short_reply:
            answer_status = "partial"

    thinking_status = "not_available"
    if usage.get("thinking_tokens") is not None:
        if thinking_visibility == "visible":
            thinking_status = _range_status(usage["thinking_tokens"], reasoning_expected)
        elif usage["thinking_tokens"] == 0 and reasoning_estimate == 0:
            thinking_status = "pass"
        else:
            thinking_status = "partial"

    output_status = total_status
    breakdown_statuses: list[str] = []
    if usage.get("answer_tokens") is not None:
        breakdown_statuses.append(answer_status)
    if thinking_visibility == "visible" and usage.get("thinking_tokens") is not None:
        breakdown_statuses.append(thinking_status)
    if breakdown_statuses:
        output_status = _aggregate_statuses([total_status, *breakdown_statuses])
    elif thinking_detected and total_status == "pass":
        output_status = "partial"
        output_note = output_note or (
            "output total is verified, but the answer/thinking split is not authoritative"
        )

    input_payload = {
        "reported_tokens": usage.get("input_tokens"),
        "compared_tokens": input_compared,
        "cache_tokens": usage.get("cache_tokens") or 0,
        "estimated_tokens": input_estimate,
        "expected_min": input_expected["min"],
        "expected_max": input_expected["max"],
        "status": input_status,
        "source": usage.get("input_source"),
        "note": input_note,
    }
    output_payload = {
        "reported_total_tokens": usage.get("output_tokens"),
        "reported_answer_tokens": usage.get("answer_tokens"),
        "reported_thinking_tokens": usage.get("thinking_tokens"),
        "advisory_details": usage.get("details_advisory") or {},
        "estimated_answer_tokens": answer_estimate,
        "estimated_visible_thinking_tokens": reasoning_estimate,
        "estimated_visible_output_tokens": visible_output_estimate,
        "expected_total_min": total_expected["min"],
        "expected_total_max": total_expected["max"],
        "answer_expected_min": answer_expected["min"],
        "answer_expected_max": answer_expected["max"],
        "thinking_expected_min": reasoning_expected["min"],
        "thinking_expected_max": reasoning_expected["max"],
        "answer_status": answer_status,
        "thinking_status": thinking_status,
        "thinking_visibility": thinking_visibility,
        "thinking_requested": thinking_requested,
        "thinking_detected": thinking_detected,
        "short_reply": short_reply,
        "total_status": total_status,
        "status": output_status,
        "source": usage.get("output_source"),
        "thinking_source": usage.get("thinking_source"),
        "note": output_note,
    }
    independent = count_semantic_tokens(
        config,
        provider=provider,
        model=model,
        transport=transport,
        input_text=_semantic_text(input_estimate_value),
        output_text=_semantic_text(
            [output_semantic.get("answer"), output_semantic.get("reasoning")]
        ),
    )
    if isinstance(independent_input_count, dict):
        independent["input"] = {
            "tokens": _optional_int(independent_input_count.get("tokens")),
            "evidence_level": str(
                independent_input_count.get("evidence_level") or "provider_count"
            ),
            "note": independent_input_count.get("note"),
            "covers_full_input": independent_input_count.get("covers_full_input") is True,
            "request_integrity": count_request_integrity,
        }
        independent["source"] = (
            independent_input_count.get("source") or independent.get("source")
        )
        independent["kind"] = (
            independent_input_count.get("kind") or independent.get("kind")
        )
    elif opaque_or_hidden_input and (independent.get("input") or {}).get("evidence_level") == "exact":
        independent["input"] = {
            **independent["input"],
            "evidence_level": "estimate",
            "note": "a tokenizer of visible text cannot exactly count opaque media or server-side context",
        }
    usage_arithmetic = _usage_arithmetic(usage)
    input_accuracy = _accuracy_check(
        usage.get("input_tokens"), independent.get("input") or {}, "input"
    )
    nonimage_media_output = _has_nonimage_media_output(response_json, usage, transport)
    if hidden_thinking or not output_plausibility_supported or nonimage_media_output:
        output_accuracy = {
            "status": "not_available",
            "reported_tokens": usage.get("output_tokens"),
            "independent_tokens": (independent.get("output") or {}).get("tokens"),
            "delta": None,
            "evidence_level": (independent.get("output") or {}).get(
                "evidence_level", "unavailable"
            ),
            "note": (
                "image output uses source-bound ranges, not an exact visible-text count"
                if not output_plausibility_supported else
                "non-text media output has no independent complete output count"
                if nonimage_media_output else
                "hidden or summarized thinking prevents exact visible-output comparison"
            ),
        }
    else:
        output_accuracy = _accuracy_check(
            usage.get("output_tokens"), independent.get("output") or {}, "output"
        )
    status = _aggregate_statuses(
        [
            str(usage_arithmetic.get("status") or "not_available"),
            str(input_accuracy.get("status") or "not_available"),
            str(output_accuracy.get("status") or "not_available"),
        ]
    )
    evidence_level = _aggregate_evidence_level(
        [
            str(input_accuracy.get("evidence_level") or "unavailable"),
            str(output_accuracy.get("evidence_level") or "unavailable"),
        ]
    )
    gross_plausibility = _gross_plausibility(
        request_body=request_body or {},
        transport=transport,
        usage=usage,
        input_estimate=input_estimate,
        answer_estimate=answer_estimate,
        visible_output_estimate=visible_output_estimate,
        input_semantic_present=bool(_semantic_text(input_semantic)),
        output_semantic_present=bool(
            _semantic_text([output_semantic.get("answer"), output_semantic.get("reasoning")])
        ) or output_artifact_present,
        hidden_thinking=hidden_thinking,
        output_plausibility_supported=output_plausibility_supported,
        independent=independent,
        input_accuracy=input_accuracy,
        settings=settings,
        xai_visible_completion_budget=(
            xai_chat_accounting
            and type((request_body or {}).get("max_completion_tokens")) is int
            and (request_body or {})["max_completion_tokens"] > 0
            and not any(key in (request_body or {}) for key in (
                "max_tokens", "max_output_tokens", "maxOutputTokens",
                "generationConfig", "generation_config", "inferenceConfig", "body",
            ))
        ),
    )
    if not output_plausibility_supported:
        image_quantity = _image_output_quantity(
            request_body or {}, response_json, usage, transport, image_output_expectation,
            output_artifact_present=output_artifact_present, settings=settings,
        )
        if image_output_evidence is not None and image_output_evidence["status"] != "pass":
            image_quantity.update(status=image_output_evidence["status"], note=image_output_evidence["note"])
        gross_plausibility["output"] = image_quantity
    if nonimage_media_output:
        gross_plausibility["output"].update(status="not_available", note="non-image media output quantity has no independent source-bound expectation")
    gross_plausibility["status"] = _aggregate_statuses([
        str((gross_plausibility.get("input") or {}).get("status") or "not_available"),
        str((gross_plausibility.get("output") or {}).get("status") or "not_available"),
    ])
    validation = _validation_outcome(
        required=usage_required,
        usage_presence=usage_presence,
        usage_arithmetic=usage_arithmetic,
        gross_plausibility=gross_plausibility,
        input_accuracy=input_accuracy,
        output_accuracy=output_accuracy,
        output_completion=output_completion,
        count_request_integrity=count_request_integrity,
    )
    return {
        "schema_version": TOKEN_AUDIT_SCHEMA_VERSION,
        "exchange": exchange,
        "status": status,
        "validation_status": validation["status"],
        "validation_pass": validation["pass"],
        "validation_failures": validation["failures"],
        "usage_required": usage_required,
        "count_request_integrity": count_request_integrity,
        "input": input_payload,
        "output": output_payload,
        "reported": _reported_usage(usage),
        "independent_count": independent,
        "usage_arithmetic": usage_arithmetic,
        "usage_presence": usage_presence,
        "output_completion": output_completion,
        "image_output_evidence": image_output_evidence,
        "gross_plausibility": gross_plausibility,
        "input_accuracy": input_accuracy,
        "output_accuracy": output_accuracy,
        "evidence_level": evidence_level,
        "usage_accounting": usage,
        "settings": settings,
    }


def audit_image_usage(
    request_body: dict[str, Any],
    response_json: dict[str, Any],
    usage: dict[str, Any],
    config: dict[str, Any],
    *,
    provider: str | None,
    model: str,
    transport: str = "image_generation",
    usage_required: bool = True,
    image_output_expectation: dict[str, Any] | None = None,
    independent_input_count: dict[str, Any] | None = None,
) -> dict[str, Any]:
    transport = _canonical_transport(transport)
    result = SimpleNamespace(usage=usage, response_json=response_json)
    output_artifact_present = _image_response_has_artifact(response_json, transport)
    audit = audit_exchange(
        request_body,
        result,
        transport,
        config,
        "initial",
        provider=provider,
        model=model,
        usage_required=usage_required,
        output_plausibility_supported=False,
        output_artifact_present=output_artifact_present,
        independent_input_count=independent_input_count,
        image_output_expectation=image_output_expectation,
    )
    return combine_exchange_audits([audit])


def combine_exchange_audits(exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = [str(item.get("status") or "not_available") for item in exchanges]
    validation_statuses = [
        str(item.get("validation_status") or "not_available") for item in exchanges
    ]
    validation_failures = [
        str(failure)
        for item in exchanges
        for failure in item.get("validation_failures") or []
    ]
    integrity_failures: list[str] = []
    if not exchanges:
        integrity_failures.append("token audit contains no exchanges")
    elif any(
        not _has_complete_validation_evidence(item)
        for item in exchanges
    ):
        integrity_failures.append(
            "token audit exchange is missing a passing current-schema validation"
        )
    combined_validation_pass = not integrity_failures and not validation_failures
    combined_validation_status = _aggregate_validation_statuses(validation_statuses)
    if integrity_failures:
        combined_validation_status = "fail"
    return {
        "status": _aggregate_statuses(statuses),
        "validation_status": combined_validation_status,
        "validation_pass": combined_validation_pass,
        "validation_failures": list(
            dict.fromkeys([*validation_failures, *integrity_failures])
        ),
        **_completion_counts(exchanges),
        "exchanges": exchanges,
    }


def summarize_token_audits(results: list[dict[str, Any]]) -> dict[str, Any]:
    exchanges = list(_iter_audit_exchanges(results))
    dimensions: list[dict[str, Any]] = []
    for exchange in exchanges:
        if "input_accuracy" in exchange or "output_accuracy" in exchange:
            dimensions.extend(
                [exchange.get("input_accuracy") or {}, exchange.get("output_accuracy") or {}]
            )
        else:
            dimensions.extend([exchange.get("input") or {}, exchange.get("output") or {}])
    status_counts = {
        status: sum(1 for item in dimensions if item.get("status") == status)
        for status in ("pass", "fail", "partial", "not_available")
    }
    eligible = status_counts["pass"] + status_counts["fail"] + status_counts["partial"]
    total_dimensions = len(dimensions)
    arithmetic_checks = [
        item.get("usage_arithmetic") or {}
        for item in exchanges
        if "usage_arithmetic" in item
    ]
    arithmetic_failures = sum(
        1 for item in arithmetic_checks if item.get("status") == "fail"
    )
    required_exchanges = [item for item in exchanges if item.get("usage_required") is True]
    required_exchange_failures = [
        item for item in required_exchanges if not _has_complete_validation_evidence(item)
    ]
    missing_usage_exchanges = [
        item
        for item in required_exchanges
        if (item.get("usage_presence") or {}).get("status") != "pass"
    ]
    gross_checks = [
        item.get("gross_plausibility") or {}
        for item in required_exchanges
        if "gross_plausibility" in item
    ]
    gross_failures = [item for item in gross_checks if item.get("status") == "fail"]
    gross_partial = [item for item in gross_checks if item.get("status") == "partial"]
    missing_audit_results = [
        result
        for result in results
        if _result_requires_token_audit(result)
        and not any(
            isinstance(exchange, dict)
            and exchange.get("usage_required") is True
            and exchange.get("schema_version") == TOKEN_AUDIT_SCHEMA_VERSION
            for exchange in ((result.get("token_audit") or {}).get("exchanges") or [])
        )
    ]
    missing_audit_result_ids = {id(result) for result in missing_audit_results}
    invalid_audit_results: list[dict[str, Any]] = []
    for result in results:
        if (
            not _result_requires_token_audit(result)
            or id(result) in missing_audit_result_ids
        ):
            continue
        audit = result.get("token_audit") or {}
        result_exchanges = [
            exchange
            for exchange in audit.get("exchanges") or []
            if isinstance(exchange, dict)
        ]
        has_failing_required_exchange = any(
            exchange.get("usage_required") is True
            and exchange.get("schema_version") == TOKEN_AUDIT_SCHEMA_VERSION
            and not _has_complete_validation_evidence(exchange)
            for exchange in result_exchanges
        )
        schema_integrity = bool(result_exchanges) and all(
            _has_complete_validation_evidence(exchange)
            for exchange in result_exchanges
        )
        if (
            not has_failing_required_exchange
            and (
                audit.get("validation_pass") is not True
                or not schema_integrity
            )
        ):
            invalid_audit_results.append(result)
    accounting = [item.get("usage_accounting") or {} for item in exchanges]
    input_tokens = _sum_present(item.get("input_tokens") for item in accounting)
    answer_tokens = _sum_present(item.get("answer_tokens") for item in accounting)
    cached_accounting = [item for item in accounting if item.get("cache_tokens_present") is True]
    cached_tokens = _sum_present(item.get("cache_tokens") for item in cached_accounting)
    tool_use_accounting = [
        item for item in accounting if item.get("tool_use_tokens") is not None
    ]
    tool_use_tokens = _sum_present(
        item.get("tool_use_tokens") for item in tool_use_accounting
    )
    thinking_accounting = [item for item in accounting if item.get("thinking_tokens") is not None]
    thinking_tokens = _sum_present(item.get("thinking_tokens") for item in thinking_accounting)
    advisory_thinking = [
        (item.get("details_advisory") or {}).get("reasoning_tokens")
        for item in accounting
        if item.get("thinking_tokens") is None
    ]
    advisory_thinking_tokens = _sum_present(advisory_thinking)
    output_tokens = _sum_present(item.get("output_tokens") for item in accounting)
    total_tokens = _sum_present(item.get("total_tokens") for item in accounting)
    thinking_sample_count = sum(1 for item in accounting if item.get("thinking_tokens") is not None)
    thinking_output_tokens = _sum_present(item.get("output_tokens") for item in thinking_accounting)
    thinking_share = (
        thinking_tokens / thinking_output_tokens
        if thinking_tokens is not None and thinking_output_tokens and thinking_output_tokens > 0
        else None
    )
    evidence_status = _aggregate_statuses(
        [str(item.get("status") or "not_available") for item in exchanges]
    )
    empty_audit = not exchanges
    validation_failure_count = (
        len(required_exchange_failures)
        + len(missing_audit_results)
        + len(invalid_audit_results)
        + int(empty_audit)
    )
    validation_status = (
        "fail"
        if validation_failure_count
        else "partial"
        if gross_partial
        else "pass"
        if required_exchanges
        else "not_applicable"
    )
    mismatch_count = validation_failure_count
    return {
        "schema_version": TOKEN_AUDIT_SCHEMA_VERSION,
        "status": evidence_status,
        "validation_status": validation_status,
        "pass": validation_failure_count == 0,
        "exchange_count": len(exchanges),
        "required_exchange_count": len(required_exchanges),
        "validated_exchange_count": len(required_exchanges) - len(required_exchange_failures),
        "validation_failure_count": validation_failure_count,
        "missing_audit_result_count": len(missing_audit_results),
        "invalid_audit_result_count": len(invalid_audit_results),
        "missing_usage_count": len(missing_usage_exchanges),
        "gross_check_count": len(gross_checks),
        "gross_failure_count": len(gross_failures),
        "gross_partial_count": len(gross_partial),
        **_completion_counts(required_exchanges),
        "total_dimensions": total_dimensions,
        "eligible_dimensions": eligible,
        "passed_dimensions": status_counts["pass"],
        "failed_dimensions": status_counts["fail"],
        "partial_dimensions": status_counts["partial"],
        "not_available_dimensions": status_counts["not_available"],
        "coverage": eligible / total_dimensions if total_dimensions else 0.0,
        "pass_rate": status_counts["pass"] / eligible if eligible else None,
        "mismatch_count": mismatch_count,
        "exact_mismatch_count": status_counts["fail"],
        "arithmetic_check_count": len(arithmetic_checks),
        "arithmetic_failure_count": arithmetic_failures,
        "exact_dimension_count": sum(
            1 for item in dimensions if item.get("evidence_level") == "exact"
        ),
        "estimated_dimension_count": sum(
            1 for item in dimensions if item.get("evidence_level") == "estimate"
        ),
        "input_tokens": input_tokens,
        "answer_tokens": answer_tokens,
        "cached_tokens": cached_tokens,
        "cached_token_sample_count": len(cached_accounting),
        "tool_use_tokens": tool_use_tokens,
        "tool_use_token_sample_count": len(tool_use_accounting),
        "thinking_tokens": thinking_tokens,
        "thinking_token_sample_count": thinking_sample_count,
        "advisory_thinking_tokens": advisory_thinking_tokens,
        "advisory_thinking_token_sample_count": sum(
            1 for value in advisory_thinking if value is not None
        ),
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "thinking_share": thinking_share,
    }


def flatten_token_audits(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        audit = result.get("token_audit") or {}
        for exchange in audit.get("exchanges") or []:
            rows.append(
                {
                    "name": result.get("name"),
                    "provider": result.get("provider"),
                    "model": result.get("model"),
                    "reference_source": result.get("reference_source"),
                    "profile": result.get("profile"),
                    "run_index": result.get("run_index"),
                    **exchange,
                }
            )
    return rows


def _normalized_payload(
    *,
    transport: str,
    input_tokens: int | None,
    input_primary_tokens: int | None,
    answer_tokens: int | None,
    thinking_tokens: int | None,
    output_tokens: int | None,
    calculated_total: int | None,
    provider_total: int | None,
    cache_tokens: int,
    input_source: str | None,
    output_source: str | None,
    thinking_source: str | None,
    details_advisory: dict[str, int],
    errors: list[str],
    validate_provider_total: bool = True,
) -> dict[str, Any]:
    if (
        calculated_total is not None
        and provider_total is not None
        and calculated_total != provider_total
        and validate_provider_total
    ):
        errors.append(
            f"provider total {provider_total} differs from calculated input+output {calculated_total}"
        )
    return {
        "transport": transport,
        "input_tokens": input_tokens,
        "input_primary_tokens": input_primary_tokens,
        "answer_tokens": answer_tokens,
        "thinking_tokens": thinking_tokens,
        "output_tokens": output_tokens,
        "total_tokens": calculated_total if calculated_total is not None else provider_total,
        "provider_total_tokens": provider_total,
        "cache_tokens": cache_tokens,
        "cache_tokens_present": False,
        "input_source": input_source,
        "output_source": output_source,
        "thinking_source": thinking_source,
        "details_advisory": details_advisory,
        "input_details": {},
        "output_details": {},
        "input_image_tokens": None,
        "output_image_tokens": None,
        "errors": errors,
    }


def _with_raw_usage(payload: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    direct_cached = _first_int(raw, "cached_tokens", "cache_tokens")
    if direct_cached is not None and not payload.get("cache_tokens"):
        payload["cache_tokens"] = direct_cached
    payload["cache_tokens_present"] = _has_any_path(
        raw,
        ("cached_tokens",),
        ("cache_tokens",),
        ("cachedContentTokenCount",),
        ("cached_content_token_count",),
        ("total_cached_tokens",),
        ("cache_creation_input_tokens",),
        ("cache_read_input_tokens",),
        ("prompt_tokens_details", "cached_tokens"),
        ("input_tokens_details", "cached_tokens"),
    )

    input_details = _usage_detail_counts(raw, "input")
    output_details = _usage_detail_counts(raw, "output")
    direct_image = _first_int(raw, "image_tokens")
    input_image = input_details.get("image_tokens")
    output_image = output_details.get("image_tokens")
    payload["input_details"] = input_details
    payload["output_details"] = output_details
    payload["input_image_tokens"] = input_image
    payload["output_image_tokens"] = output_image
    if input_image is not None and output_image is not None:
        payload["image_tokens"] = input_image + output_image
        payload["image_token_scope"] = "input_and_output"
    elif output_image is not None:
        payload["image_tokens"] = output_image
        payload["image_token_scope"] = "output"
    elif input_image is not None:
        payload["image_tokens"] = input_image
        payload["image_token_scope"] = "input"
    else:
        payload["image_tokens"] = direct_image
        payload["image_token_scope"] = "unknown" if direct_image is not None else None
    payload["raw_usage"] = raw
    return payload


def _has_any_path(value: dict[str, Any], *paths: tuple[str, ...]) -> bool:
    for path in paths:
        current: Any = value
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            return True
    return False


def _usage_detail_counts(raw: dict[str, Any], dimension: str) -> dict[str, int]:
    parent_keys = (
        ("input_tokens_details", "prompt_tokens_details")
        if dimension == "input"
        else ("output_tokens_details", "completion_tokens_details")
    )
    details: dict[str, int] = {}
    for parent in parent_keys:
        value = raw.get(parent)
        if not isinstance(value, dict):
            continue
        for key, item in value.items():
            parsed = _optional_int(item)
            if parsed is not None and _looks_like_token_key(str(key)):
                details.setdefault(str(key), parsed)

    gemini_parent = (
        "promptTokensDetails" if dimension == "input" else "candidatesTokensDetails"
    )
    gemini_value = raw.get(gemini_parent)
    if not isinstance(gemini_value, list):
        snake_parent = (
            "prompt_tokens_details"
            if dimension == "input"
            else "candidates_tokens_details"
        )
        gemini_value = raw.get(snake_parent)
    if isinstance(gemini_value, list):
        for item in gemini_value:
            if not isinstance(item, dict):
                continue
            modality = str(item.get("modality") or "unknown").strip().casefold()
            count = _first_int(item, "tokenCount", "token_count")
            if count is not None:
                details[f"{modality}_tokens"] = (
                    details.get(f"{modality}_tokens", 0) + count
                )

    interactions_parent = (
        "input_tokens_by_modality"
        if dimension == "input"
        else "output_tokens_by_modality"
    )
    interactions_value = raw.get(interactions_parent)
    if isinstance(interactions_value, list):
        for item in interactions_value:
            if not isinstance(item, dict):
                continue
            modality = str(item.get("modality") or "unknown").strip().casefold()
            count = _first_int(item, "tokens")
            if count is not None:
                details[f"{modality}_tokens"] = (
                    details.get(f"{modality}_tokens", 0) + count
                )
    return details


def _looks_like_token_key(key: str) -> bool:
    normalized = key.casefold()
    return (
        normalized == "tokens"
        or normalized.endswith("tokens")
        or normalized.endswith("token_count")
        or normalized.endswith("tokencount")
    )


def _validate_detail_counts(
    errors: list[str],
    details: dict[str, int],
    parent_tokens: int | None,
    dimension: str,
) -> None:
    if parent_tokens is None:
        return
    for name, value in details.items():
        if value > parent_tokens:
            errors.append(f"{dimension} detail {name} exceeds {dimension} tokens")
    modality_keys = {
        "text_tokens",
        "image_tokens",
        "audio_tokens",
        "video_tokens",
    }
    modality_total = sum(
        value for name, value in details.items() if name.casefold() in modality_keys
    )
    if modality_total > parent_tokens:
        errors.append(f"{dimension} modality token details exceed {dimension} tokens")


def _reported_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": usage.get("input_tokens"),
        "input_primary_tokens": usage.get("input_primary_tokens"),
        "answer_tokens": usage.get("answer_tokens"),
        "thinking_tokens": usage.get("thinking_tokens"),
        "rejected_prediction_tokens": usage.get("rejected_prediction_tokens"),
        "image_tokens": usage.get("image_tokens"),
        "image_token_scope": usage.get("image_token_scope"),
        "input_image_tokens": usage.get("input_image_tokens"),
        "output_image_tokens": usage.get("output_image_tokens"),
        "input_details": usage.get("input_details") or {},
        "output_details": usage.get("output_details") or {},
        "output_tokens": usage.get("output_tokens"),
        "cached_tokens": usage.get("cache_tokens"),
        "tool_use_tokens": usage.get("tool_use_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "provider_total_tokens": usage.get("provider_total_tokens"),
    }


def _usage_arithmetic(usage: dict[str, Any]) -> dict[str, Any]:
    errors = [str(item) for item in usage.get("errors") or []]
    raw = usage.get("raw_usage") if isinstance(usage.get("raw_usage"), dict) else {}
    for path, value in _token_scalars(raw):
        # Optional provider detail fields are commonly present with a JSON null
        # value.  Required top-level input/output counts are enforced separately
        # by _usage_presence, so null advisory details are absence, not malformed
        # arithmetic.
        if value is None:
            continue
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            errors.append(f"{path} must be non-negative")
        elif isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path} must be a non-negative integer")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("provider_total_tokens")
    if input_tokens is not None and output_tokens is not None and total_tokens is not None:
        calculated = int(input_tokens) + int(output_tokens)
        if calculated != int(total_tokens):
            message = f"provider total {total_tokens} differs from input+output {calculated}"
            if message not in errors:
                errors.append(message)
    cached_tokens = usage.get("cache_tokens")
    if (
        cached_tokens is not None
        and input_tokens is not None
        and int(cached_tokens) > int(input_tokens)
    ):
        errors.append("cached tokens exceed input tokens")
    thinking_tokens = usage.get("thinking_tokens")
    if (
        thinking_tokens is not None
        and output_tokens is not None
        and int(thinking_tokens) > int(output_tokens)
    ):
        errors.append("thinking tokens exceed output tokens")
    rejected_prediction_tokens = usage.get("rejected_prediction_tokens")
    if (
        rejected_prediction_tokens is not None and output_tokens is not None
        and rejected_prediction_tokens + (thinking_tokens or 0) > output_tokens
    ):
        errors.append("hidden reasoning and rejected prediction tokens exceed output tokens")
    image_tokens = usage.get("image_tokens")
    image_scope = usage.get("image_token_scope")
    image_parent_tokens = (
        input_tokens
        if image_scope == "input"
        else output_tokens
        if image_scope == "output"
        else None
    )
    if (
        image_tokens is not None
        and image_parent_tokens is not None
        and image_scope in {"input", "output"}
        and int(image_tokens) > int(image_parent_tokens)
    ):
        errors.append(f"image tokens exceed {image_scope} tokens")
    _validate_detail_counts(
        errors,
        usage.get("input_details") or {},
        input_tokens,
        "input",
    )
    output_details = dict(usage.get("output_details") or {})
    _validate_detail_counts(
        errors,
        output_details,
        output_tokens,
        "output",
    )
    has_usage = any(
        usage.get(key) is not None
        for key in (
            "input_tokens",
            "output_tokens",
            "provider_total_tokens",
            "image_tokens",
            "tool_use_tokens",
        )
    )
    return {
        "status": "fail" if errors else "pass" if has_usage else "not_available",
        "errors": list(dict.fromkeys(errors)),
        "calculated_total_tokens": _sum_if_all(input_tokens, output_tokens),
        "provider_total_tokens": total_tokens,
        # Cached and tool-use counts are provider-reported breakdowns.  They
        # are intentionally not added to total_tokens: cached tokens are part
        # of input, while the Interactions API reports tool-use prompt tokens
        # as a separate diagnostic dimension.
        "cached_tokens": cached_tokens,
        "tool_use_tokens": usage.get("tool_use_tokens"),
    }


def _usage_presence(usage: dict[str, Any], *, required: bool) -> dict[str, Any]:
    input_present = usage.get("input_tokens") is not None
    output_present = usage.get("output_tokens") is not None
    missing = [
        name
        for name, present in (
            ("input_tokens", input_present),
            ("output_tokens", output_present),
        )
        if not present
    ]
    if not required:
        status = "not_applicable"
        note = "usage is not required for a non-successful exchange"
    elif missing:
        status = "fail"
        note = "successful exchange is missing authoritative " + ", ".join(missing)
    else:
        status = "pass"
        note = None
    return {
        "required": required,
        "status": status,
        "input_present": input_present,
        "output_present": output_present,
        "missing_fields": missing if required else [],
        "note": note,
    }


def _canonical_transport(transport: str) -> str:
    return {
        "chat-completions": "chat_completions",
        "openai-responses": "openai_responses",
        "claude-messages": "claude_messages",
        "gemini-generate-content": "gemini_generate_content",
        "gemini-interactions": "gemini_interactions",
        "fim-completions": "fim_completions",
        "images-generations": "image_generation",
    }.get(transport, transport)


def _output_completion(
    result: Any,
    transport: str,
    *,
    output_artifact_present: bool,
) -> dict[str, Any]:
    """Require affirmative terminal evidence; plausible usage cannot prove EOF."""
    response = getattr(result, "response_json", None) or {}
    signals: list[dict[str, str]] = []
    failures: list[str] = []
    missing: list[str] = []
    terminal_values = {
        "stop", "stop_sequence", "end_turn", "tool_use", "tool_calls",
        "function_call", "completed", "requires_action",
    }
    terminal_values = {
        "chat_completions": {"stop", "tool_calls", "function_call"},
        "fim_completions": {"stop"},
        "claude_messages": {"end_turn", "stop_sequence", "tool_use"},
        "gemini_generate_content": {"stop"},
        "openai_responses": {"completed"},
        "gemini_interactions": {"completed", "requires_action"},
    }.get(transport, terminal_values)
    truncated_values = {
        "length", "max_tokens", "max_output_tokens", "max_completion_tokens",
        "max_token", "token_limit", "model_length", "max_length",
    }

    def record(source: str, value: Any) -> None:
        if value in (None, ""):
            return
        normalized = str(value).strip().casefold()
        signals.append({"source": source, "value": str(value)})
        if normalized in truncated_values:
            failures.append(f"output was truncated by its token limit ({source}={value})")
        elif normalized not in terminal_values:
            failures.append(f"output did not complete normally ({source}={value})")

    error_type = getattr(result, "error_type", None)
    if error_type:
        failures.append(f"response or stream error prevents verified completion: {error_type}")
    if response.get("error"):
        failures.append("response contains an error instead of a complete output")
    if response.get("incomplete_details"):
        failures.append("response contains incomplete_details; output is incomplete")
    result_finish = getattr(result, "finish_reason", None)
    record("result.finish_reason", result_finish)
    for key in ("finish_reason", "stop_reason", "status"):
        record(key, response.get(key))
    if transport in {"chat_completions", "fim_completions"}:
        choices = response.get("choices") or []
        for index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                missing.append(f"choices[{index}]")
                continue
            reason = choice.get("finish_reason")
            record(f"choices[{index}].finish_reason", reason)
            if not reason and not (len(choices) == 1 and result_finish):
                missing.append(f"choices[{index}].finish_reason")
    elif transport == "gemini_generate_content":
        candidates = response.get("candidates") or []
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                missing.append(f"candidates[{index}]")
                continue
            reason = candidate.get("finishReason") or candidate.get("finish_reason")
            record(f"candidates[{index}].finishReason", reason)
            if not reason and not (len(candidates) == 1 and result_finish):
                missing.append(f"candidates[{index}].finishReason")
    elif transport == "openai_responses":
        for index, item in enumerate(response.get("output") or []):
            if isinstance(item, dict):
                record(f"output[{index}].status", item.get("status"))
    if transport == "image_generation" and output_artifact_present:
        signals.append({"source": "image_artifact", "value": "completed"})
    if missing:
        failures.append("output completion evidence is missing for " + ", ".join(missing))
    if failures:
        status = "fail"
        note = "; ".join(dict.fromkeys(failures))
    elif not signals:
        status = "not_available"
        note = "response has no affirmative output completion evidence"
    else:
        status = "pass"
        note = None
    return {
        "status": status,
        "complete": status == "pass",
        "signals": signals,
        "failures": list(dict.fromkeys(failures)),
        "note": note,
    }


def _has_complete_validation_evidence(exchange: dict[str, Any]) -> bool:
    if (
        exchange.get("schema_version") != TOKEN_AUDIT_SCHEMA_VERSION
        or exchange.get("validation_pass") is not True
        or exchange.get("count_request_integrity") == "fail"
        or bool(exchange.get("validation_failures"))
    ):
        return False
    if exchange.get("usage_required") is False:
        return exchange.get("validation_status") == "not_applicable"
    if exchange.get("usage_required") is not True:
        return False
    gross = exchange.get("gross_plausibility") or {}
    return exchange.get("validation_status") == "pass" and gross.get("status") == "pass" and all(
        check.get("status") == "pass"
        for check in (
            exchange.get("usage_presence") or {},
            exchange.get("usage_arithmetic") or {},
            exchange.get("output_completion") or {},
            gross.get("input") or {},
            gross.get("output") or {},
        )
    ) and not any(
        (exchange.get(key) or {}).get("status") == "fail"
        for key in ("input_accuracy", "output_accuracy")
    )


def _completion_counts(exchanges: list[dict[str, Any]]) -> dict[str, int]:
    required = [item for item in exchanges if item.get("usage_required") is True]
    checks = [item.get("output_completion") or {} for item in required]
    return {
        "completion_check_count": sum(bool(item) for item in checks),
        "completion_failure_count": sum(item.get("status") == "fail" for item in checks),
        "completion_unverified_count": sum(
            item.get("status") not in {"pass", "fail"} for item in checks
        ),
    }


def _image_output_quantity(
    request_body: dict[str, Any],
    response_json: dict[str, Any],
    usage: dict[str, Any],
    transport: str,
    expectation: dict[str, Any] | None,
    *,
    output_artifact_present: bool,
    settings: dict[str, Any],
) -> dict[str, Any]:
    reported = usage.get("output_tokens")
    check: dict[str, Any] = {
        "status": "not_available",
        "reported_tokens": reported,
        "reported_image_tokens": usage.get("output_image_tokens"),
        "minimum_plausible_tokens": None,
        "maximum_plausible_tokens": None,
        "evidence_level": "unavailable",
        "note": "image output quantity has no independent source-bound token expectation",
        "validation_scope": "official_image_token_estimate",
        "exact_output_count_verified": False,
    }
    if reported is not None and reported <= 0 and output_artifact_present:
        check.update(status="fail", note="a non-empty image output cannot plausibly use zero output tokens")
        return check
    if not output_artifact_present:
        check.update(status="fail", note="the response contains no generated-image artifact")
        return check
    if not isinstance(expectation, dict):
        return check
    minimum = _optional_int(expectation.get("min"))
    maximum = _optional_int(expectation.get("max"))
    evidence = expectation.get("evidence_level")
    source = expectation.get("source")
    component = expectation.get("component") or "image"
    if (
        minimum is None or maximum is None or maximum < minimum or not source
        or evidence not in {"official_range", "exact"} or component not in {"image", "total"}
    ):
        check["note"] = "image token expectation is missing a valid range and official source evidence"
        return check
    accepted_ranges = [{"min": minimum, "max": maximum}]
    if "ranges" in expectation:
        raw_ranges = expectation.get("ranges")
        if not isinstance(raw_ranges, list) or not raw_ranges:
            check["note"] = "image token expectation has no valid discrete ranges"
            return check
        accepted_ranges = []
        for item in raw_ranges:
            low = _optional_int(item.get("min")) if isinstance(item, dict) else None
            high = _optional_int(item.get("max")) if isinstance(item, dict) else None
            if low is None or high is None or high < low:
                check["note"] = "image token expectation has an invalid discrete range"
                return check
            accepted_ranges.append({"min": low, "max": high})
    output_semantic = _output_semantic_payload(response_json, transport)
    text_present = bool(output_semantic.get("answer"))
    text_estimate = estimate_token_count(output_semantic.get("answer"))
    thinking = usage.get("thinking_tokens") if usage.get("thinking_source") else None
    candidates = usage.get("answer_tokens")
    if candidates is None and reported is not None:
        candidates = reported - (thinking or 0)
    image_tokens = usage.get("output_image_tokens")
    details = usage.get("output_details") or {}
    modality_details = (
        details if transport == "gemini_generate_content" else
        {name: count for name, count in details.items()
         if name in {"image_tokens", "text_tokens", "audio_tokens", "video_tokens"}}
    )
    modality_total = sum(modality_details.values())
    residual = candidates - modality_total if candidates is not None else None
    other_modalities = sorted(
        name for name, count in modality_details.items()
        if name not in {"image_tokens", "text_tokens"} and count > 0
    )
    # A residual is not a visible TEXT count. Under the image-estimate policy,
    # incomplete modality accounting remains diagnostic, not a pass prerequisite.
    candidate_breakdown = {
        "status": "pass",
        "reported_tokens": candidates,
        "reported_modality_tokens": modality_total,
        "unclassified_tokens": residual,
        "unverified_modalities": other_modalities,
        "note": None,
    }
    if residual is None:
        candidate_breakdown.update(
            status="not_available", note="authoritative candidate token usage is unavailable"
        )
    elif residual < 0:
        candidate_breakdown.update(
            status="fail", note="image output modality tokens exceed candidate tokens"
        )
    elif residual > 0 or other_modalities:
        candidate_breakdown.update(
            status="partial",
            note="candidate output is not fully decomposed by reported modalities",
        )

    text_reported = details.get("text_tokens")
    text_check = _gross_dimension_check(
        dimension="image accompanying text",
        reported=text_reported,
        estimate=text_estimate,
        semantic_present=text_present,
        ratio=settings["gross_output_ratio"],
        absolute_tolerance=settings["gross_output_absolute_tolerance"],
        opaque_high_side=False,
    )
    text_check["reported_text_detail_tokens"] = text_reported
    if text_reported is None and not text_present and residual == 0:
        text_check.update(
            status="not_applicable",
            note="no visible text or unclassified candidate tokens require a text quantity check",
        )

    image_check = {
        "status": "not_available",
        "reported_tokens": image_tokens,
        "minimum_plausible_tokens": minimum,
        "maximum_plausible_tokens": maximum,
        "accepted_ranges": accepted_ranges,
        "evidence_level": evidence,
        "source": source,
        "note": "image-only usage was not separately reported",
    }
    if component == "image" and image_tokens is not None:
        image_check.update(
            status="pass" if any(
                item["min"] <= image_tokens <= item["max"] for item in accepted_ranges
            ) else "fail",
            note=None,
        )
        if image_check["status"] == "fail":
            image_check["note"] = "image output token count is outside its source-bound range"

    # Apply the same approximate output policy with or without IMAGE details.
    # Keep each official quality/count range separate and retain its lower bound;
    # the wider upper envelope accommodates accompanying text and bounded overhead.
    compared_output = candidates if component == "image" else reported
    visible_estimate = text_estimate if component == "image" else 0
    estimate_checks = []
    nominal_counts = []
    raw_ranges = expectation.get("ranges")
    for index, source_range in enumerate(accepted_ranges):
        reference = raw_ranges[index] if isinstance(raw_ranges, list) else expectation
        nominal_value = reference.get("tokens")
        if nominal_value is None and len(accepted_ranges) == 1:
            nominal_value = expectation.get("tokens")
        nominal = _optional_int(nominal_value)
        if nominal_value is not None and nominal is None:
            check["note"] = "nominal image token estimate is not a non-negative integer"
            return check
        if nominal is None:
            nominal = (source_range["min"] + source_range["max"]) // 2
        if nominal <= 0 or not source_range["min"] <= nominal <= source_range["max"]:
            check["note"] = "nominal image token estimate is outside its official source range"
            return check
        nominal_counts.append(nominal)
        estimate_check = _gross_dimension_check(
            dimension="image candidate output",
            reported=compared_output,
            estimate=nominal + visible_estimate,
            semantic_present=True,
            ratio=settings["gross_output_ratio"],
            absolute_tolerance=settings["gross_output_absolute_tolerance"],
            opaque_high_side=False,
        )
        visible_minimum = text_check["minimum_plausible_tokens"] if component == "image" else 0
        estimate_check["minimum_plausible_tokens"] = max(
            estimate_check["minimum_plausible_tokens"],
            source_range["min"] + visible_minimum,
        )
        if compared_output is not None and compared_output < estimate_check["minimum_plausible_tokens"]:
            estimate_check.update(
                status="fail", note="reported image candidate output tokens are below the official image lower bound"
            )
        estimate_checks.append(estimate_check)
    estimate_status = (
        "pass" if any(item["status"] == "pass" for item in estimate_checks)
        else "not_available" if compared_output is None else "fail"
    )
    output_estimate = {
        "status": estimate_status,
        "reported_tokens": compared_output,
        "reported_total_tokens": reported,
        "excluded_thinking_tokens": thinking,
        "nominal_image_tokens": nominal_counts,
        "visible_text_estimate": visible_estimate,
        "minimum_plausible_tokens": min(item["minimum_plausible_tokens"] for item in estimate_checks),
        "maximum_plausible_tokens": max(item["maximum_plausible_tokens"] for item in estimate_checks),
        "ranges": estimate_checks,
        "ratio": settings["gross_output_ratio"],
        "absolute_tolerance": settings["gross_output_absolute_tolerance"],
        "note": None if estimate_status == "pass" else (
            "authoritative output usage is unavailable" if compared_output is None
            else "reported image candidate output tokens are outside the official-image estimate envelope"
        ),
    }
    check.update(
        reported_tokens=image_tokens if component == "image" and image_tokens is not None else reported,
        minimum_plausible_tokens=minimum,
        maximum_plausible_tokens=maximum,
        accepted_ranges=accepted_ranges,
        evidence_level=evidence,
        source=source,
        component=component,
        image_component=image_check,
        text_component=text_check,
        candidate_breakdown=candidate_breakdown,
        output_estimate=output_estimate,
        expectation=dict(expectation),
    )
    if image_check["status"] == "fail":
        check.update(status="fail", note=image_check["note"])
    elif candidate_breakdown["status"] == "fail":
        check.update(status="fail", note=candidate_breakdown["note"])
    elif text_check["status"] == "fail":
        check.update(status="fail", note=text_check["note"])
    elif other_modalities:
        check.update(status="not_available", note="non-image output modalities have no source-bound image-token expectation")
    else:
        check.update(status=estimate_status, note=output_estimate["note"])
    limit = _request_output_token_limit(request_body, transport)
    check["request_limit_tokens"] = limit
    if limit is not None and reported is not None and reported > limit:
        check.update(status="fail", note="reported image output tokens exceed the requested output-token limit")
    return check


def _gross_plausibility(
    *,
    request_body: dict[str, Any],
    transport: str,
    usage: dict[str, Any],
    input_estimate: int,
    answer_estimate: int,
    visible_output_estimate: int,
    input_semantic_present: bool,
    output_semantic_present: bool,
    hidden_thinking: bool,
    output_plausibility_supported: bool,
    independent: dict[str, Any],
    input_accuracy: dict[str, Any],
    settings: dict[str, Any],
    xai_visible_completion_budget: bool = False,
) -> dict[str, Any]:
    opaque_input = _request_has_external_context(request_body)
    counted_input = _optional_int((independent.get("input") or {}).get("tokens"))
    counted_output = _optional_int((independent.get("output") or {}).get("tokens"))
    input_reference = counted_input if counted_input is not None else input_estimate
    official_full_count = bool(
        counted_input is not None
        and independent.get("kind") == "provider_count"
        and (independent.get("input") or {}).get("evidence_level") == "official_count"
        and (independent.get("input") or {}).get("covers_full_input") is True
    )
    # An exact count of the complete effective request also covers requested
    # media or server-side context. A visible-text estimate cannot do so.
    if input_accuracy.get("status") == "pass" or official_full_count:
        opaque_input = False
    input_check = _gross_dimension_check(
        dimension="input",
        reported=usage.get("input_tokens"),
        estimate=input_reference,
        semantic_present=input_semantic_present,
        ratio=1.10 if official_full_count else settings["gross_input_ratio"],
        absolute_tolerance=8 if official_full_count else settings["gross_input_absolute_tolerance"],
        opaque_high_side=opaque_input,
    )
    input_check["evidence_level"] = (
        (independent.get("input") or {}).get("evidence_level", "estimate")
        if counted_input is not None else "estimate"
    )
    input_check["covers_full_input"] = official_full_count or input_accuracy.get("status") == "pass"
    if opaque_input and input_check["status"] == "pass":
        input_check["status"] = "partial"
        input_check["note"] = (
            "the visible input is within range, but requested media or server-side "
            "context has no independent complete input count"
        )
    text_input = (usage.get("input_details") or {}).get("text_tokens")
    if opaque_input and text_input is not None:
        text_check = _gross_dimension_check(
            dimension="input text",
            reported=text_input,
            estimate=input_estimate,
            semantic_present=input_semantic_present,
            ratio=settings["gross_input_ratio"],
            absolute_tolerance=settings["gross_input_absolute_tolerance"],
            opaque_high_side=False,
        )
        input_check["text_component"] = text_check
        if text_check["status"] == "fail":
            input_check["status"] = "fail"
            input_check["note"] = text_check["note"]
    if not _request_has_media(request_body) and (usage.get("input_image_tokens") or 0) > 0:
        input_check["status"] = "fail"
        input_check["note"] = "image input tokens were reported for a request without input media"

    if output_plausibility_supported:
        # xAI Chat's documented max_completion_tokens constrains visible
        # completion_tokens, while normalized output includes reasoning too.
        # Keep total-usage plausibility and arithmetic; change only the budget
        # comparison, and only with the exact source/contract/field binding.
        raw_completion = (usage.get("raw_usage") or {}).get("completion_tokens")
        visible_budget = (
            xai_visible_completion_budget
            and transport == "chat_completions"
            and type(raw_completion) is int and raw_completion >= 0
        )
        request_limit = _request_output_token_limit(request_body, transport)
        thinking_tokens = usage.get("thinking_tokens")
        answer_tokens = usage.get("answer_tokens")
        separated_hidden = hidden_thinking and thinking_tokens is not None and answer_tokens is not None and bool(usage.get("thinking_source"))
        compared_output = answer_tokens if separated_hidden else usage.get("output_tokens")
        output_reference = (
            answer_estimate if separated_hidden else counted_output
            if counted_output is not None and not hidden_thinking else visible_output_estimate
        )
        output_check = _gross_dimension_check(
            dimension="output",
            reported=compared_output,
            estimate=output_reference,
            semantic_present=output_semantic_present,
            ratio=settings["gross_output_ratio"],
            absolute_tolerance=settings["gross_output_absolute_tolerance"],
            opaque_high_side=hidden_thinking and not separated_hidden,
            request_limit=None if visible_budget else request_limit,
            request_limit_multiplier=settings["gross_output_limit_multiplier"],
        )
        output_check["reported_total_tokens"] = usage.get("output_tokens")
        output_check["compared_component"] = "answer" if separated_hidden else "total"
        output_check["hidden_thinking_tokens"] = thinking_tokens if separated_hidden else None
        limited_output = usage.get("output_tokens")
        if visible_budget:
            limited_output = raw_completion
            output_check.update(
                request_limit_tokens=request_limit,
                request_limit_maximum_tokens=(
                    int(math.ceil(request_limit * settings["gross_output_limit_multiplier"]))
                    if request_limit is not None else None
                ),
                request_limit_compared_tokens=raw_completion,
                request_limit_counter="usage.completion_tokens",
                request_limit_scope="xai_visible_completion_tokens",
            )
        limit = output_check.get("request_limit_maximum_tokens")
        if limit is not None and limited_output is not None and limited_output > limit:
            output_check["status"] = "fail"
            output_check["note"] = "reported output tokens exceed the requested output-token limit"
        elif hidden_thinking and not separated_hidden and output_check["status"] == "pass":
            output_check["status"] = "partial"
            output_check["note"] = "hidden reasoning has no authoritative token breakdown for the visible answer"
        output_check["evidence_level"] = "estimate"
    else:
        reported_output = usage.get("output_tokens")
        if reported_output is None:
            output_status = "not_available"
            output_note = "authoritative output usage is unavailable"
        elif output_semantic_present and reported_output == 0:
            output_status = "fail"
            output_note = "a non-empty image output cannot plausibly use zero output tokens"
        else:
            output_status = "not_applicable"
            output_note = (
                "positive image output usage is present, but its quantity cannot be "
                "derived from decoded pixels without a published model formula"
            )
        output_check = {
            "status": output_status,
            "reported_tokens": reported_output,
            "estimated_tokens": visible_output_estimate,
            "minimum_plausible_tokens": None,
            "maximum_plausible_tokens": None,
            "request_limit_tokens": _request_output_token_limit(request_body, transport),
            "note": output_note,
        }

    statuses = [str(input_check["status"]), str(output_check["status"])]
    if "fail" in statuses:
        status = "fail"
    elif "partial" in statuses:
        status = "partial"
    elif "pass" in statuses:
        status = "pass"
    else:
        status = "not_available"
    return {
        "status": status,
        "input": input_check,
        "output": output_check,
        "policy": {
            "input_ratio": settings["gross_input_ratio"],
            "output_ratio": settings["gross_output_ratio"],
            "input_absolute_tolerance": settings[
                "gross_input_absolute_tolerance"
            ],
            "output_absolute_tolerance": settings[
                "gross_output_absolute_tolerance"
            ],
            "output_limit_multiplier": settings[
                "gross_output_limit_multiplier"
            ],
        },
    }


def _gross_dimension_check(
    *,
    dimension: str,
    reported: int | None,
    estimate: int,
    semantic_present: bool,
    ratio: float,
    absolute_tolerance: int,
    opaque_high_side: bool,
    request_limit: int | None = None,
    request_limit_multiplier: float | None = None,
) -> dict[str, Any]:
    estimate = max(int(estimate), 0)
    minimum = 1 if semantic_present else 0
    # The absolute tolerance is a high-side allowance, not permission for a
    # visibly non-trivial payload to collapse to one reported token.  Applying
    # the deliberately wide ratio floor at every size catches extreme
    # under-reporting while small estimates still retain the one-token floor.
    minimum = max(minimum, int(math.floor(estimate / ratio)))
    minimum = max(1 if semantic_present else 0, minimum - int(absolute_tolerance))
    maximum = int(math.ceil(estimate * ratio)) + int(absolute_tolerance)
    limit_maximum = None
    if request_limit is not None and request_limit_multiplier is not None:
        limit_maximum = int(math.ceil(request_limit * request_limit_multiplier))

    status = "not_available"
    note = f"authoritative {dimension} usage is unavailable"
    if reported is not None:
        if semantic_present and reported == 0:
            status = "fail"
            note = f"non-empty {dimension} payload cannot plausibly use zero tokens"
        elif reported < minimum:
            status = "fail"
            note = (
                f"reported {dimension} tokens are below the gross plausibility floor"
            )
        elif (
            limit_maximum is not None
            and reported > limit_maximum
        ):
            status = "fail"
            note = (
                f"reported {dimension} tokens exceed the request limit by more than "
                f"the allowed {request_limit_multiplier:g}x envelope"
            )
        elif reported > maximum and opaque_high_side:
            status = "partial"
            note = (
                f"reported {dimension} tokens exceed the visible envelope, but opaque "
                "server-side context or hidden reasoning prevents a conclusive mismatch"
            )
        elif reported > maximum:
            status = "fail"
            note = f"reported {dimension} tokens exceed the gross plausibility envelope"
        else:
            status = "pass"
            note = None
    return {
        "status": status,
        "reported_tokens": reported,
        "estimated_tokens": estimate,
        "minimum_plausible_tokens": minimum,
        "maximum_plausible_tokens": maximum,
        "request_limit_tokens": request_limit,
        "request_limit_maximum_tokens": limit_maximum,
        "opaque_high_side": opaque_high_side,
        "note": note,
    }


def _validation_outcome(
    *,
    required: bool,
    usage_presence: dict[str, Any],
    usage_arithmetic: dict[str, Any],
    gross_plausibility: dict[str, Any],
    input_accuracy: dict[str, Any],
    output_accuracy: dict[str, Any],
    output_completion: dict[str, Any],
    count_request_integrity: str | None = None,
) -> dict[str, Any]:
    if count_request_integrity == "fail":
        return {"status": "fail", "pass": False, "failures": ["independent token-count request failed request-integrity validation"]}
    if not required:
        return {"status": "not_applicable", "pass": True, "failures": []}

    failures: list[str] = []
    if output_completion.get("status") != "pass":
        failures.extend(
            str(item) for item in output_completion.get("failures") or [
                output_completion.get("note") or "output completion could not be verified"
            ]
        )
    if usage_presence.get("status") != "pass":
        failures.append(str(usage_presence.get("note") or "required usage is missing"))
    if usage_arithmetic.get("status") != "pass":
        errors = usage_arithmetic.get("errors") or []
        failures.append(
            "; ".join(str(item) for item in errors)
            or "usage arithmetic could not be verified"
        )
    if gross_plausibility.get("status") == "fail":
        for dimension in ("input", "output"):
            check = gross_plausibility.get(dimension) or {}
            if check.get("status") == "fail":
                failures.append(
                    str(check.get("note") or f"{dimension} token count is implausible")
                )
    input_check = gross_plausibility.get("input") or {}
    if input_check.get("status") in {"partial", "not_available"}:
        failures.append(str(input_check.get("note") or "complete input token quantity is unverified"))
    output_check = gross_plausibility.get("output") or {}
    if output_check.get("status") in {"partial", "not_available", "not_applicable"}:
        failures.append(str(output_check.get("note") or "complete output token quantity is unverified"))
    for dimension, accuracy in (
        ("input", input_accuracy),
        ("output", output_accuracy),
    ):
        if accuracy.get("status") == "fail":
            failures.append(
                str(accuracy.get("note") or f"exact {dimension} token mismatch")
            )
    failures = list(dict.fromkeys(failures))
    if failures:
        return {"status": "fail", "pass": False, "failures": failures}
    return {
        "status": (
            "partial" if gross_plausibility.get("status") == "partial" else "pass"
        ),
        "pass": True,
        "failures": [],
    }


def _accuracy_check(
    reported_tokens: int | None, independent: dict[str, Any], dimension: str
) -> dict[str, Any]:
    counted = _optional_int(independent.get("tokens"))
    evidence = str(independent.get("evidence_level") or "unavailable")
    delta = (
        int(reported_tokens) - counted
        if reported_tokens is not None and counted is not None
        else None
    )
    if reported_tokens is None:
        status = "not_available"
        note = f"provider did not report authoritative {dimension} tokens"
    elif counted is None:
        status = "not_available"
        note = independent.get("note") or f"independent {dimension} count is unavailable"
    elif evidence != "exact":
        status = "not_available"
        note = independent.get("note") or "estimated counts do not produce accuracy verdicts"
    else:
        status = "pass" if delta == 0 else "fail"
        note = None if delta == 0 else f"reported {dimension} tokens differ from exact count"
    return {
        "status": status,
        "reported_tokens": reported_tokens,
        "independent_tokens": counted,
        "delta": delta,
        "evidence_level": evidence,
        "note": note,
    }


def _aggregate_evidence_level(levels: list[str]) -> str:
    if levels and all(level == "exact" for level in levels):
        return "exact"
    if "exact" in levels:
        return "mixed"
    if "provider_count" in levels:
        return "provider_count"
    if "official_count" in levels:
        return "official_count"
    if "estimate" in levels:
        return "estimate"
    return "unavailable"


def _token_scalars(value: Any, prefix: str = "usage"):
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            normalized_key = str(key).casefold()
            is_token_count = (
                normalized_key == "tokens"
                or normalized_key.endswith("tokens")
                or normalized_key.endswith("token_count")
                or normalized_key.endswith("tokencount")
            )
            if is_token_count and not isinstance(item, (dict, list)):
                yield path, item
            yield from _token_scalars(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _token_scalars(item, f"{prefix}[{index}]")


def _audit_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = ((config.get("test_cases") or {}).get("token_accuracy") or {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "relative_tolerance": _bounded_float(
            raw.get("relative_tolerance"), DEFAULT_RELATIVE_TOLERANCE, 0.0, 10.0
        ),
        "input_absolute_tolerance": _bounded_int(
            raw.get("input_absolute_tolerance"), DEFAULT_INPUT_ABSOLUTE_TOLERANCE, 0, 4096
        ),
        "output_absolute_tolerance": _bounded_int(
            raw.get("output_absolute_tolerance"), DEFAULT_OUTPUT_ABSOLUTE_TOLERANCE, 0, 4096
        ),
        "gross_input_ratio": _bounded_float(
            raw.get("gross_input_ratio"), DEFAULT_GROSS_INPUT_RATIO, 1.0, DEFAULT_GROSS_INPUT_RATIO
        ),
        "gross_output_ratio": _bounded_float(
            raw.get("gross_output_ratio"), DEFAULT_GROSS_OUTPUT_RATIO, 1.0, DEFAULT_GROSS_OUTPUT_RATIO
        ),
        "gross_input_absolute_tolerance": _bounded_int(
            raw.get("gross_input_absolute_tolerance"),
            DEFAULT_GROSS_INPUT_ABSOLUTE_TOLERANCE,
            0,
            DEFAULT_GROSS_INPUT_ABSOLUTE_TOLERANCE,
        ),
        "gross_output_absolute_tolerance": _bounded_int(
            raw.get("gross_output_absolute_tolerance"),
            DEFAULT_GROSS_OUTPUT_ABSOLUTE_TOLERANCE,
            0,
            DEFAULT_GROSS_OUTPUT_ABSOLUTE_TOLERANCE,
        ),
        "gross_output_limit_multiplier": _bounded_float(
            raw.get("gross_output_limit_multiplier"),
            DEFAULT_GROSS_OUTPUT_LIMIT_MULTIPLIER,
            1.0,
            DEFAULT_GROSS_OUTPUT_LIMIT_MULTIPLIER,
        ),
    }


def _output_status(
    *,
    usage: dict[str, Any],
    answer_expected: dict[str, int],
    visible_total_expected: dict[str, int],
    hidden_thinking: bool,
    short_reply: bool,
    thinking_visibility: str,
) -> tuple[str, str | None, dict[str, int]]:
    if usage.get("errors"):
        return "fail", "; ".join(usage["errors"]), visible_total_expected
    reported_total = usage.get("output_tokens")
    if reported_total is None:
        return "not_available", "authoritative output usage is unavailable", visible_total_expected

    reported_thinking = usage.get("thinking_tokens")
    if hidden_thinking and reported_thinking is not None:
        expected = {
            "min": answer_expected["min"] + reported_thinking,
            "max": answer_expected["max"] + reported_thinking,
        }
        reported_answer = usage.get("answer_tokens")
        answer_status = _range_status(reported_answer, answer_expected)
        if answer_status == "fail" and reported_answer is not None and reported_answer < answer_expected["min"]:
            return "fail", "non-thinking output is below the visible answer interval", expected
        return (
            "partial",
            "thinking is included in output usage but its hidden or summarized content cannot be independently verified",
            expected,
        )

    status = _range_status(reported_total, visible_total_expected)
    if status == "fail" and reported_total > visible_total_expected["max"]:
        if hidden_thinking or short_reply or thinking_visibility == "summary":
            return (
                "partial",
                "high-side difference may come from short-response framing or hidden thinking",
                visible_total_expected,
            )
    if hidden_thinking and status == "pass":
        return "partial", "output total includes potentially hidden thinking", visible_total_expected
    return status, None, visible_total_expected


def _input_semantic_payload(body: dict[str, Any], transport: str) -> Any:
    if transport == "fim_completions":
        # FIM input accounting covers both sides of the insertion point.  A
        # prompt-only audit systematically undercounts requests with a suffix.
        return f"{body.get('prompt') or ''}{body.get('suffix') or ''}"
    if transport == "openai_responses":
        return {
            key: body[key]
            for key in (
                "instructions",
                "input",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
                "text",
            )
            if key in body
        }
    if transport in {"image_generation", "images-generations"}:
        return {
            key: body[key]
            for key in ("prompt", "messages")
            if key in body
        }
    if transport == "gemini_interactions":
        return {
            key: body[key]
            for key in ("system_instruction", "input", "tools", "response_format")
            if key in body
        }
    if transport == "gemini_generate_content":
        payload: dict[str, Any] = {
            key: body[key]
            for key in ("contents", "systemInstruction", "tools", "toolConfig", "safetySettings", "cachedContent")
            if key in body
        }
        generation = body.get("generationConfig")
        if isinstance(generation, dict):
            semantic_generation = {
                key: generation[key]
                for key in ("responseMimeType", "responseSchema", "responseJsonSchema", "responseFormat")
                if key in generation
            }
            if semantic_generation:
                payload["generationConfig"] = semantic_generation
        return payload
    if transport == "claude_messages":
        return {
            key: body[key]
            for key in ("system", "messages", "tools", "tool_choice", "output_config")
            if key in body
        }
    return {
        key: body[key]
        for key in (
            "messages",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "response_format",
            "functions",
            "function_call",
            "safetySettings",
            "generationConfig",
        )
        if key in body
    }


def _output_semantic_payload(response: dict[str, Any], transport: str) -> dict[str, str]:
    answers: list[Any] = []
    reasoning: list[Any] = []
    visibility = "none"

    if transport == "fim_completions":
        for choice in response.get("choices") or []:
            if isinstance(choice, dict) and choice.get("text") not in (None, ""):
                answers.append(choice.get("text"))
    elif transport == "openai_responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "reasoning":
                visible = item.get("summary") or item.get("content")
                if visible:
                    reasoning.append(visible)
                    visibility = "summary"
                else:
                    # encrypted_content/signatures are opaque protocol state,
                    # not visible reasoning text that can be tokenized locally.
                    visibility = "hidden"
            elif item_type == "function_call":
                answers.append(
                    {"name": item.get("name"), "arguments": item.get("arguments")}
                )
            else:
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in {"output_text", "text"}:
                        answers.append(part.get("text"))
                    elif part.get("type") == "refusal":
                        answers.append(part.get("refusal"))
    elif transport == "gemini_interactions":
        saw_hidden_thought = False
        for step in response.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_type = str(step.get("type") or "")
            if step_type == "model_output":
                text = _gemini_interactions_content_text(step.get("content"))
                if text:
                    answers.append(text)
            elif step_type == "thought":
                summary = _gemini_interactions_content_text(step.get("summary"))
                if summary:
                    reasoning.append(summary)
                    visibility = "summary"
                else:
                    saw_hidden_thought = True
            elif step_type in {"function_call", "tool_call"}:
                answers.append(
                    {
                        "name": step.get("name"),
                        "arguments": step.get("arguments") or step.get("input"),
                    }
                )
        if visibility == "none" and (
            saw_hidden_thought
            or (_first_int(response.get("usage") or {}, "total_thought_tokens") or 0) > 0
        ):
            visibility = "hidden"
    elif transport == "gemini_generate_content":
        for candidate in response.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            for part in content.get("parts") or [] if isinstance(content, dict) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("thought") is True:
                    reasoning.append(
                        {
                            key: value
                            for key, value in part.items()
                            if key not in {"thought", "thoughtSignature"}
                        }
                    )
                    visibility = "summary"
                elif "text" in part:
                    answers.append(part.get("text"))
                elif "functionCall" in part:
                    answers.append(part.get("functionCall"))
        if not reasoning and _first_int(response.get("usageMetadata") or {}, "thoughtsTokenCount"):
            visibility = "hidden"
    elif transport == "claude_messages":
        for block in response.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "thinking":
                reasoning.append(block.get("thinking") or "")
                visibility = "summary"
            elif block_type == "redacted_thinking":
                visibility = "hidden"
            elif block_type == "text":
                answers.append(block.get("text"))
            elif block_type == "tool_use":
                answers.append({"name": block.get("name"), "input": block.get("input")})
    else:
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or choice.get("delta") or {}
            if not isinstance(message, dict):
                continue
            if message.get("content") not in (None, ""):
                answers.append(message.get("content"))
            if message.get("tool_calls"):
                answers.append(message.get("tool_calls"))
            if message.get("function_call"):
                answers.append(message.get("function_call"))
            if message.get("reasoning_content") not in (None, ""):
                reasoning.append(message.get("reasoning_content"))
                visibility = "visible"

    return {
        "answer": _semantic_text(_strip_output_media(answers)),
        "reasoning": _semantic_text(reasoning),
        "thinking_visibility": visibility,
    }


def _strip_output_media(value: Any) -> Any:
    """Never mistake generated-image bytes or Markdown images for text tokens."""
    if isinstance(value, str):
        text = re.sub(r"!\[[^\]]*\]\([^\s]*(?:\s+\"[^\"]*\")?\)", "", value)
        return re.sub(r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=\s]+", "", text).strip()
    if isinstance(value, dict):
        if str(value.get("type") or "").casefold() in {"image", "image_url", "output_image", "input_image"}:
            return None
        if any(key in value for key in ("inlineData", "inline_data", "b64_json", "image_url")):
            return None
        return {key: cleaned for key, item in value.items() if (cleaned := _strip_output_media(item)) not in (None, "", [], {})}
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := _strip_output_media(item)) not in (None, "", [], {})]
    return value


def _request_expects_image(body: dict[str, Any]) -> bool:
    for key in ("generationConfig", "generation_config"):
        generation = body.get(key)
        if isinstance(generation, dict):
            modalities = generation.get("responseModalities") or generation.get("response_modalities") or []
            if isinstance(modalities, list) and "IMAGE" in {str(item).upper() for item in modalities}:
                return True
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") == "image":
        return True
    extra = body.get("extra_body")
    google = extra.get("google") if isinstance(extra, dict) else None
    if isinstance(google, dict) and isinstance(google.get("image_config"), dict):
        return True
    return any(isinstance(tool, dict) and tool.get("type") == "image_generation" for tool in body.get("tools") or [])


def _image_output_parts(response: dict[str, Any], transport: str) -> list[Any]:
    if transport == "gemini_generate_content":
        return [
            part
            for candidate in response.get("candidates") or [] if isinstance(candidate, dict)
            for part in (candidate.get("content") or {}).get("parts") or []
            if isinstance(part, dict) and part.get("thought") is not True
        ]
    if transport == "gemini_interactions":
        parts = [
            part
            for step in response.get("steps") or []
            if isinstance(step, dict) and step.get("type") == "model_output"
            for part in step.get("content") or []
        ]
        if not parts and isinstance(response.get("output_image"), dict):
            parts.append({"type": "image", **response["output_image"]})
        return parts
    if transport in {"chat_completions", "fim_completions"}:
        return [choice.get("message") or choice.get("delta") or choice.get("text") for choice in response.get("choices") or [] if isinstance(choice, dict)]
    if transport == "openai_responses":
        return [item for item in response.get("output") or [] if isinstance(item, dict) and item.get("type") != "reasoning"]
    return list(response.get("data") or [])


def _embedded_image_payloads(response: dict[str, Any], transport: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []

    def add(value: Any, mime: Any = None, *, url: bool = False) -> None:
        if not isinstance(value, str) or not value:
            payloads.append({"encoded": None, "mime": mime, "error": "empty image payload"})
        elif value.startswith("data:image/"):
            header, separator, encoded = value.partition(",")
            payloads.append({"encoded": encoded if separator and ";base64" in header else None, "mime": header[5:].split(";", 1)[0]})
        elif url:
            payloads.append({"encoded": None, "mime": mime, "remote": True})
        else:
            payloads.append({"encoded": value, "mime": mime})

    def visit(value: Any) -> None:
        if isinstance(value, str):
            for match in re.finditer(r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]+", value):
                add(match.group())
            for match in re.finditer(r"!\[[^\]]*\]\((https?://[^)]+)\)", value):
                add(match.group(1), url=True)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict) or value.get("thought") is True:
            return
        kind = str(value.get("type") or "")
        if kind in {"thought", "thinking", "reasoning", "redacted_thinking"}:
            return
        if kind == "image_generation_call":
            add(value.get("result"))
            return
        if kind in {"image", "output_image", "generated_image"} and "data" in value:
            add(value.get("data"), value.get("mime_type") or value.get("mimeType"))
            return
        if "b64_json" in value:
            add(value.get("b64_json"))
            return
        for key in ("inlineData", "inline_data"):
            inline = value.get(key)
            if isinstance(inline, dict):
                mime = inline.get("mimeType") or inline.get("mime_type")
                if str(mime or "").startswith("image/"):
                    add(inline.get("data"), mime)
                return
        if "image_url" in value:
            image_url = value["image_url"]
            add(image_url.get("url") if isinstance(image_url, dict) else image_url, url=True)
            return
        if kind in {"image", "image_url", "output_image", "generated_image"} or (transport == "image_generation" and any(key in value for key in ("url", "uri"))):
            image_url = value.get("url") or value.get("uri")
            if image_url:
                add(image_url, url=True)
                return
        for item in value.values():
            visit(item)

    visit(_image_output_parts(response, transport))
    return payloads


def _decoded_image_output_evidence(response: dict[str, Any], transport: str) -> dict[str, Any]:
    from .image_validation import MAX_IMAGE_PAYLOAD_BYTES, inspect_image_bytes

    payloads = _embedded_image_payloads(response, transport)
    images: list[dict[str, Any]] = []
    errors: list[str] = []
    remote_count = 0
    for index, payload in enumerate(payloads):
        if payload.get("remote"):
            remote_count += 1
            continue
        encoded = payload.get("encoded")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_PAYLOAD_BYTES + 2) // 3) * 4:
            errors.append(f"image {index} has a missing or oversized encoded payload")
            continue
        try:
            raw = base64.b64decode(encoded, validate=True)
            info = inspect_image_bytes(raw, visual_forensics=False)
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(info.format.upper())
            if payload.get("mime") and payload["mime"] != mime:
                raise ValueError("declared image MIME type differs from decoded image")
            images.append(info.public())
        except (ValueError, binascii.Error, ImportError) as exc:
            errors.append(f"image {index} could not be completely decoded: {exc.__class__.__name__}")
    status = "fail" if errors or not payloads else "not_available" if remote_count else "pass"
    return {
        "status": status, "images": images, "artifact_count": len(payloads),
        "decoded_count": len(images), "remote_count": remote_count, "errors": errors,
        "note": "; ".join(errors) if errors else "image URL output has no locally verified decoded dimensions" if remote_count else "no final generated-image artifact is present" if not payloads else None,
    }


def _has_nonimage_media_output(response: dict[str, Any], usage: dict[str, Any], transport: str) -> bool:
    if any((usage.get("output_details") or {}).get(key, 0) > 0 for key in ("audio_tokens", "video_tokens")):
        return True

    def visit(value: Any) -> bool:
        if isinstance(value, dict):
            if str(value.get("type") or "") in {"function_call", "tool_call", "tool_use", "tool_result"}:
                return False
            if str(value.get("type") or "") in {"audio", "output_audio", "video", "output_video"}:
                return True
            if str(value.get("mimeType") or value.get("mime_type") or "").startswith(("audio/", "video/")):
                return True
            if isinstance(value.get("audio"), dict) and value["audio"]:
                return True
            return any(visit(value.get(key)) for key in ("content", "parts", "inlineData", "inline_data", "fileData", "file_data"))
        return isinstance(value, list) and any(visit(item) for item in value)

    return visit(response.get("content") or []) if transport == "claude_messages" else visit(_image_output_parts(response, transport))


def _image_response_has_artifact(response: dict[str, Any], transport: str) -> bool:
    """Detect a non-empty generated-image artifact without estimating its tokens."""
    return bool(_embedded_image_payloads(response, _canonical_transport(transport)))


def _gemini_interactions_content_text(value: Any) -> str:
    """Extract only user-visible text content from an Interactions step."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_gemini_interactions_content_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    value_type = str(value.get("type") or "")
    if value_type in {"text", "thought_summary"} and isinstance(value.get("text"), str):
        return value["text"]
    content = value.get("content")
    if isinstance(content, (dict, list, str)):
        return _gemini_interactions_content_text(content)
    return ""


def _thinking_requested(body: dict[str, Any]) -> bool:
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        if str(thinking.get("type") or "").lower() not in {"", "disabled", "none"}:
            return True
    if body.get("enable_thinking") is True:
        return True
    if str(body.get("reasoning_effort") or "").strip().casefold() not in {
        "",
        "none",
        "off",
        "disabled",
    }:
        return True
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        effort = str(reasoning.get("effort") or "").strip().casefold()
        mode = str(reasoning.get("mode") or "").strip().casefold()
        if effort not in {"", "none", "off", "disabled"} or mode:
            return True
    generation_config = body.get("generation_config")
    if isinstance(generation_config, dict):
        level = str(generation_config.get("thinking_level") or "").strip().casefold()
        if level and level not in {"none", "off"}:
            return True
    for container_key in ("extra_body", "generationConfig", "generation_config"):
        container = body.get(container_key)
        if isinstance(container, dict) and _contains_enabled_thinking(container):
            return True
    return False


def _contains_enabled_thinking(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"thinking_level", "thinkinglevel"}:
                level = str(item or "").strip().casefold()
                if level and level not in {"none", "off"}:
                    return True
            if lowered in {"thinking", "thinkingconfig", "thinking_config"} and isinstance(item, dict):
                thinking_type = str(item.get("type") or "").lower()
                budget = item.get("thinkingBudget", item.get("thinking_budget"))
                if thinking_type in {"enabled", "adaptive"}:
                    return True
                if item.get("includeThoughts") is True or item.get("include_thoughts") is True:
                    return True
                if budget is not None and _optional_int(budget) not in (None, 0):
                    return True
            if lowered == "enable_thinking" and item is True:
                return True
            if _contains_enabled_thinking(item):
                return True
    elif isinstance(value, list):
        return any(_contains_enabled_thinking(item) for item in value)
    return False


def _request_has_media(body: dict[str, Any]) -> bool:
    media_keys = {
        "image", "images", "image_url", "input_image", "input_audio", "audio_url",
        "video_url", "inline_data", "inlinedata", "filedata", "file_data",
    }

    def visit(value: Any, key: str = "") -> bool:
        if key.replace("-", "_").casefold() in {
            "response_format", "responseformat", "response_modalities", "responsemodalities",
            "image_config", "imageconfig", "output_format", "output_config",
            "generationconfig", "generation_config",
            "tools", "functions", "tool_choice", "parameters", "properties",
        }:
            return False
        if key.replace("-", "_").casefold() in media_keys and value not in (None, "", [], {}):
            return True
        if isinstance(value, dict):
            if str(value.get("type") or "").casefold() in {"image", "input_image", "audio", "input_audio", "video"}:
                return True
            return any(visit(item, str(child_key)) for child_key, item in value.items())
        if isinstance(value, list):
            return any(visit(item, key) for item in value)
        return False

    return visit(body)


def _request_has_external_context(body: dict[str, Any]) -> bool:
    opaque_keys = {
        "cachedcontent",
        "cached_content",
        "previous_response_id",
        "previous_interaction_id",
        "conversation",
        "conversation_id",
        "file_id",
        "file_uri",
        "filedata",
        "file_data",
        "inline_data",
        "inlinedata",
        "image_url",
        "input_image",
        "input_audio",
        "audio_url",
        "video_url",
    }
    server_tool_names = {
        "web_search",
        "web_search_preview",
        "google_search",
        "url_context",
        "code_interpreter",
        "code_execution",
        "file_search",
        "computer_use",
    }

    def visit(value: Any, key: str = "") -> bool:
        normalized_key = key.replace("-", "_").casefold()
        if normalized_key in {
            "parameters", "properties", "response_format", "responseformat",
            "response_schema", "responseschema", "response_json_schema", "responsejsonschema",
        }:
            return False
        if normalized_key in {
            "googlesearch", "google_search", "google_search_retrieval",
            "urlcontext", "url_context", "codeexecution", "code_execution",
        } and isinstance(value, dict):
            return True
        if normalized_key in opaque_keys and value not in (None, "", [], {}):
            return True
        if normalized_key in {"enable_search", "enable_code_interpreter"} and value is True:
            return True
        if isinstance(value, dict):
            tool_type = str(value.get("type") or value.get("name") or "").casefold()
            if tool_type in server_tool_names:
                return True
            return any(visit(item, str(child_key)) for child_key, item in value.items())
        if isinstance(value, list):
            return any(visit(item, key) for item in value)
        return False

    return _request_has_media(body) or visit(body)


def _request_output_token_limit(body: dict[str, Any], transport: str) -> int | None:
    candidates: list[Any] = [
        body.get("max_tokens"),
        body.get("max_completion_tokens"),
        body.get("max_output_tokens"),
        body.get("maxOutputTokens"),
    ]
    for key in ("generationConfig", "generation_config"):
        nested = body.get(key)
        if isinstance(nested, dict):
            candidates.extend(
                [nested.get("maxOutputTokens"), nested.get("max_output_tokens")]
            )
    for parent, key in (("inferenceConfig", "maxTokens"), ("body", "max_tokens")):
        nested = body.get(parent)
        if isinstance(nested, dict):
            candidates.append(nested.get(key))
    limits = [value for item in candidates if (value := _optional_int(item)) is not None and value > 0]
    limit = min(limits) if limits else None
    if limit is None:
        return None
    count = 1
    if transport == "chat_completions":
        count = _optional_int(body.get("n")) or 1
    elif transport == "gemini_generate_content":
        generation = body.get("generationConfig") or body.get("generation_config") or {}
        if isinstance(generation, dict):
            count = (
                _optional_int(generation.get("candidateCount"))
                or _optional_int(generation.get("candidate_count"))
                or 1
            )
    return limit * count


def _strip_opaque_media(value: Any, key: str = "") -> Any:
    normalized_key = key.replace("-", "_").casefold()
    opaque_payload_keys = {
        "data",
        "b64_json",
        "bytesbase64encoded",
        "inline_data",
        "inlinedata",
        "image_url",
        "audio_url",
        "video_url",
    }
    if normalized_key in opaque_payload_keys and value not in (None, "", [], {}):
        return f"<{normalized_key}>"
    if isinstance(value, dict):
        return {
            child_key: _strip_opaque_media(item, str(child_key))
            for child_key, item in value.items()
        }
    if isinstance(value, list):
        return [_strip_opaque_media(item, key) for item in value]
    return value


def _semantic_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        compact = [item for item in value if item not in (None, "", [], {})]
        if not compact:
            return ""
        if len(compact) == 1 and isinstance(compact[0], str):
            return compact[0]
        value = compact
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _range_status(value: int | None, expected: dict[str, int]) -> str:
    if value is None:
        return "not_available"
    return "pass" if expected["min"] <= value <= expected["max"] else "fail"


def _combined_status(first: str, second: str) -> str:
    return _aggregate_statuses([first, second])


def _aggregate_statuses(statuses: list[str]) -> str:
    if not statuses or all(status == "not_available" for status in statuses):
        return "not_available"
    if "fail" in statuses:
        return "fail"
    if "partial" in statuses or "not_available" in statuses:
        return "partial"
    return "pass"


def _aggregate_validation_statuses(statuses: list[str]) -> str:
    applicable = [status for status in statuses if status != "not_applicable"]
    if not applicable or all(status == "not_available" for status in applicable):
        return "not_applicable" if not applicable else "not_available"
    if "fail" in applicable:
        return "fail"
    if "partial" in applicable or "not_available" in applicable:
        return "partial"
    return "pass"


def _iter_audit_exchanges(results: list[dict[str, Any]]):
    for result in results:
        audit = result.get("token_audit") or {}
        for exchange in audit.get("exchanges") or []:
            if isinstance(exchange, dict):
                yield exchange


def _result_requires_token_audit(result: dict[str, Any]) -> bool:
    status_code = result.get("status_code")
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return 200 <= status_code <= 299
    return result.get("token_audit_required") is True


def _sum_present(values) -> int | None:
    present = [int(value) for value in values if value is not None]
    return sum(present) if present else None


def _sum_if_any(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return (first or 0) + (second or 0)


def _sum_if_all(first: int | None, second: int | None) -> int | None:
    if first is None or second is None:
        return None
    return first + second


def _first_int(value: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        parsed = _optional_int(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _nested_first_int(value: dict[str, Any], *paths: tuple[str, str]) -> int | None:
    for parent, child in paths:
        nested = value.get(parent)
        if isinstance(nested, dict):
            parsed = _optional_int(nested.get(child))
            if parsed is not None:
                return parsed
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _openai_cached_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("input_tokens_details")
    return _optional_int(details.get("cached_tokens")) or 0 if isinstance(details, dict) else 0


def _looks_like_gemini_usage(usage: dict[str, Any]) -> bool:
    return any(
        key in usage
        for key in (
            "promptTokenCount",
            "candidatesTokenCount",
            "thoughtsTokenCount",
            "toolUsePromptTokenCount",
            "totalTokenCount",
        )
    )


def _bounded_float(
    value: Any, default: float, minimum: float, maximum: float
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and minimum <= parsed <= maximum else default


def _bounded_int(
    value: Any, default: int, minimum: int, maximum: int
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default
