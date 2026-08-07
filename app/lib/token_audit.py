from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import Any, TypedDict

from .token_counter import count_semantic_tokens


DEFAULT_RELATIVE_TOLERANCE = 0.50
DEFAULT_INPUT_ABSOLUTE_TOLERANCE = 16
DEFAULT_OUTPUT_ABSOLUTE_TOLERANCE = 8


class TokenUsage(TypedDict, total=False):
    transport: str
    input_tokens: int | None
    input_primary_tokens: int | None
    answer_tokens: int | None
    thinking_tokens: int | None
    image_tokens: int | None
    image_token_scope: str | None
    output_tokens: int | None
    total_tokens: int | None
    provider_total_tokens: int | None
    cache_tokens: int
    input_source: str | None
    output_source: str | None
    thinking_source: str | None
    details_advisory: dict[str, int]
    errors: list[str]
    raw_usage: dict[str, Any]


def normalize_usage(usage: dict[str, Any] | None, transport: str | None) -> TokenUsage:
    """Normalize provider usage without double-counting reasoning tokens.

    Chat-completions transports intentionally trust prompt_tokens and
    completion_tokens only. input_tokens/output_tokens and details fields are
    retained as advisory diagnostics because compatible gateways do not apply
    those fields consistently.
    """

    raw = usage if isinstance(usage, dict) else {}
    mode = str(transport or "generic")
    errors: list[str] = []

    if mode == "gemini_interactions":
        input_tokens = _first_int(raw, "total_input_tokens")
        answer_tokens = _first_int(raw, "total_output_tokens")
        thinking_tokens = _first_int(raw, "total_thought_tokens") or 0
        output_tokens = (
            (answer_tokens or 0) + thinking_tokens
            if answer_tokens is not None or thinking_tokens
            else None
        )
        provider_total = _first_int(raw, "total_tokens")
        calculated_total = _sum_if_any(input_tokens, output_tokens)
        cache_tokens = _first_int(raw, "total_cached_tokens") or 0
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
            input_source="usage.total_input_tokens" if input_tokens is not None else None,
            output_source="usage.total_output_tokens + total_thought_tokens"
            if output_tokens is not None
            else None,
            thinking_source="usage.total_thought_tokens"
            if _first_int(raw, "total_thought_tokens") is not None
            else None,
            details_advisory={},
            errors=errors,
        ), raw)

    if mode == "gemini_generate_content" or _looks_like_gemini_usage(raw):
        input_tokens = _first_int(raw, "promptTokenCount", "prompt_token_count")
        answer_tokens = _first_int(raw, "candidatesTokenCount", "candidates_token_count")
        thinking_tokens = _first_int(raw, "thoughtsTokenCount", "thoughts_token_count")
        thinking_tokens = thinking_tokens if thinking_tokens is not None else 0
        output_tokens = (
            (answer_tokens or 0) + thinking_tokens
            if answer_tokens is not None or thinking_tokens
            else None
        )
        provider_total = _first_int(raw, "totalTokenCount", "total_token_count", "total_tokens")
        calculated_total = _sum_if_any(input_tokens, output_tokens)
        cache_tokens = _first_int(raw, "cachedContentTokenCount", "cached_content_token_count") or 0
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
            input_source="usageMetadata.promptTokenCount" if input_tokens is not None else None,
            output_source="usageMetadata.candidatesTokenCount + thoughtsTokenCount"
            if output_tokens is not None
            else None,
            thinking_source="usageMetadata.thoughtsTokenCount"
            if _first_int(raw, "thoughtsTokenCount", "thoughts_token_count") is not None
            else None,
            details_advisory={},
            errors=errors,
        ), raw)

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
        calculated_total = _sum_if_any(input_tokens, output_tokens)
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
        calculated_total = _sum_if_any(input_tokens, output_tokens)
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

    if mode == "chat_completions":
        input_tokens = prompt_tokens
        output_tokens = completion_tokens
        calculated_total = (
            prompt_tokens + completion_tokens
            if prompt_tokens is not None and completion_tokens is not None
            else None
        )
        provider_total = _first_int(raw, "total_tokens")
        return _with_raw_usage(_normalized_payload(
            transport=mode,
            input_tokens=input_tokens,
            input_primary_tokens=input_tokens,
            answer_tokens=None,
            thinking_tokens=None,
            output_tokens=output_tokens,
            calculated_total=calculated_total,
            provider_total=provider_total,
            cache_tokens=_openai_cached_tokens(raw),
            input_source="usage.prompt_tokens" if input_tokens is not None else None,
            output_source="usage.completion_tokens" if output_tokens is not None else None,
            thinking_source=None,
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
            # Some OpenAI-compatible gateways expose advisory totals that do
            # not use the prompt/completion pair as their accounting basis.
            # Preserve normalization compatibility here; the versioned
            # usage_arithmetic audit below still validates the raw total
            # strictly and gates confirmed contradictions.
            validate_provider_total=False,
        ), raw)

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
    calculated_total = _sum_if_any(input_tokens, output_tokens)
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
) -> dict[str, Any]:
    settings = _audit_settings(config)
    usage = normalize_usage(getattr(result, "usage", None), transport)
    if not settings["enabled"]:
        unavailable = {
            "status": "not_available",
            "note": "token accuracy audit is disabled",
        }
        return {
            "schema_version": 2,
            "exchange": exchange,
            "status": "not_available",
            "input": dict(unavailable),
            "output": dict(unavailable),
            "reported": _reported_usage(usage),
            "independent_count": {},
            "usage_arithmetic": dict(unavailable),
            "input_accuracy": dict(unavailable),
            "output_accuracy": dict(unavailable),
            "evidence_level": "unavailable",
            "usage_accounting": usage,
            "settings": settings,
        }
    input_semantic = _input_semantic_payload(request_body or {}, transport)
    output_semantic = _output_semantic_payload(getattr(result, "response_json", None) or {}, transport)

    input_estimate = estimate_token_count(input_semantic)
    input_expected = token_range(
        input_estimate,
        settings["relative_tolerance"],
        settings["input_absolute_tolerance"],
    )
    cache_or_hidden_input = bool(
        usage.get("cache_tokens")
        or _request_has_external_context(request_body or {})
    )
    input_compared = usage.get("input_primary_tokens")
    if not usage.get("input_source"):
        input_status = "not_available"
        input_note = "authoritative input usage is unavailable"
    else:
        input_status = _range_status(input_compared, input_expected)
        input_note = None
        if input_status == "fail" and input_compared is not None and input_compared > input_expected["max"]:
            if cache_or_hidden_input:
                input_status = "partial"
                input_note = "reported input includes cached or server-side context that is not locally visible"
        elif input_status == "pass" and cache_or_hidden_input:
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
    )
    hidden_thinking = thinking_detected and thinking_visibility in {"none", "hidden", "summary"}
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
        input_text=_semantic_text(input_semantic),
        output_text=_semantic_text(
            [output_semantic.get("answer"), output_semantic.get("reasoning")]
        ),
    )
    if isinstance(independent_input_count, dict):
        independent["input"] = {
            "tokens": _optional_int(independent_input_count.get("tokens")),
            "evidence_level": str(
                independent_input_count.get("evidence_level") or "exact"
            ),
            "note": independent_input_count.get("note"),
        }
        independent["source"] = (
            independent_input_count.get("source") or independent.get("source")
        )
        independent["kind"] = (
            independent_input_count.get("kind") or independent.get("kind")
        )
    usage_arithmetic = _usage_arithmetic(usage)
    input_accuracy = _accuracy_check(
        usage.get("input_tokens"), independent.get("input") or {}, "input"
    )
    if hidden_thinking:
        output_accuracy = {
            "status": "not_available",
            "reported_tokens": usage.get("output_tokens"),
            "independent_tokens": (independent.get("output") or {}).get("tokens"),
            "delta": None,
            "evidence_level": (independent.get("output") or {}).get(
                "evidence_level", "unavailable"
            ),
            "note": "hidden or summarized thinking prevents exact visible-output comparison",
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
    return {
        "schema_version": 2,
        "exchange": exchange,
        "status": status,
        "input": input_payload,
        "output": output_payload,
        "reported": _reported_usage(usage),
        "independent_count": independent,
        "usage_arithmetic": usage_arithmetic,
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
) -> dict[str, Any]:
    result = SimpleNamespace(usage=usage, response_json=response_json)
    audit = audit_exchange(
        request_body,
        result,
        transport,
        config,
        "initial",
        provider=provider,
        model=model,
    )
    output_accuracy = audit.get("output_accuracy") or {}
    output_accuracy.update(
        {
            "status": "not_available",
            "delta": None,
            "note": "image tokens cannot be derived from decoded pixels without a published model formula",
        }
    )
    audit["output_accuracy"] = output_accuracy
    audit["status"] = _aggregate_statuses(
        [
            str((audit.get("usage_arithmetic") or {}).get("status") or "not_available"),
            str((audit.get("input_accuracy") or {}).get("status") or "not_available"),
            "not_available",
        ]
    )
    return combine_exchange_audits([audit])


def combine_exchange_audits(exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = [str(item.get("status") or "not_available") for item in exchanges]
    return {
        "status": _aggregate_statuses(statuses),
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
    accounting = [item.get("usage_accounting") or {} for item in exchanges]
    input_tokens = _sum_present(item.get("input_tokens") for item in accounting)
    answer_tokens = _sum_present(item.get("answer_tokens") for item in accounting)
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
    status = _aggregate_statuses(
        [str(item.get("status") or "not_available") for item in exchanges]
    )
    mismatch_count = status_counts["fail"] + arithmetic_failures
    return {
        "status": status,
        "pass": mismatch_count == 0,
        "exchange_count": len(exchanges),
        "total_dimensions": total_dimensions,
        "eligible_dimensions": eligible,
        "passed_dimensions": status_counts["pass"],
        "failed_dimensions": status_counts["fail"],
        "partial_dimensions": status_counts["partial"],
        "not_available_dimensions": status_counts["not_available"],
        "coverage": eligible / total_dimensions if total_dimensions else 0.0,
        "pass_rate": status_counts["pass"] / eligible if eligible else None,
        "mismatch_count": mismatch_count,
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
        "input_source": input_source,
        "output_source": output_source,
        "thinking_source": thinking_source,
        "details_advisory": details_advisory,
        "errors": errors,
    }


def _with_raw_usage(payload: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    direct_cached = _first_int(raw, "cached_tokens", "cache_tokens")
    if direct_cached is not None and not payload.get("cache_tokens"):
        payload["cache_tokens"] = direct_cached
    image_tokens = _first_int(raw, "image_tokens")
    image_scope = "unknown" if image_tokens is not None else None
    if image_tokens is None:
        image_tokens = _nested_first_int(
            raw,
            ("completion_tokens_details", "image_tokens"),
            ("output_tokens_details", "image_tokens"),
        )
        if image_tokens is not None:
            image_scope = "output"
    if image_tokens is None:
        image_tokens = _nested_first_int(
            raw,
            ("input_tokens_details", "image_tokens"),
        )
        if image_tokens is not None:
            image_scope = "input"
    payload["image_tokens"] = image_tokens
    payload["image_token_scope"] = image_scope
    payload["raw_usage"] = raw
    return payload


def _reported_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": usage.get("input_tokens"),
        "input_primary_tokens": usage.get("input_primary_tokens"),
        "answer_tokens": usage.get("answer_tokens"),
        "thinking_tokens": usage.get("thinking_tokens"),
        "image_tokens": usage.get("image_tokens"),
        "image_token_scope": usage.get("image_token_scope"),
        "output_tokens": usage.get("output_tokens"),
        "cached_tokens": usage.get("cache_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "provider_total_tokens": usage.get("provider_total_tokens"),
    }


def _usage_arithmetic(usage: dict[str, Any]) -> dict[str, Any]:
    errors = [str(item) for item in usage.get("errors") or []]
    raw = usage.get("raw_usage") if isinstance(usage.get("raw_usage"), dict) else {}
    for path, value in _token_scalars(raw):
        if value < 0:
            errors.append(f"{path} must be non-negative")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("provider_total_tokens")
    if input_tokens is not None and output_tokens is not None and total_tokens is not None:
        calculated = int(input_tokens) + int(output_tokens)
        if calculated != int(total_tokens):
            # xAI Chat Completions reports reasoning_tokens outside completion_tokens,
            # so total = prompt + completion + reasoning. Treat that as explained when
            # the advisory/detail reasoning field closes the gap exactly.
            advisory = usage.get("details_advisory") if isinstance(usage.get("details_advisory"), dict) else {}
            reasoning_extra = _first_int(
                {
                    "thinking_tokens": usage.get("thinking_tokens"),
                    "advisory_reasoning_tokens": advisory.get("reasoning_tokens"),
                    "completion_reasoning_tokens": _nested_first_int(
                        raw, ("completion_tokens_details", "reasoning_tokens")
                    ),
                    "output_reasoning_tokens": _nested_first_int(
                        raw, ("output_tokens_details", "reasoning_tokens")
                    ),
                },
                "thinking_tokens",
                "advisory_reasoning_tokens",
                "completion_reasoning_tokens",
                "output_reasoning_tokens",
            )
            explained = (
                reasoning_extra is not None
                and calculated + int(reasoning_extra) == int(total_tokens)
            )
            if not explained:
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
    image_tokens = usage.get("image_tokens")
    image_scope = usage.get("image_token_scope")
    image_parent_tokens = input_tokens if image_scope == "input" else output_tokens
    if (
        image_tokens is not None
        and image_parent_tokens is not None
        and image_scope in {"input", "output"}
        and int(image_tokens) > int(image_parent_tokens)
    ):
        errors.append(f"image tokens exceed {image_scope} tokens")
    has_usage = any(
        usage.get(key) is not None
        for key in ("input_tokens", "output_tokens", "provider_total_tokens", "image_tokens")
    )
    return {
        "status": "fail" if errors else "pass" if has_usage else "not_available",
        "errors": list(dict.fromkeys(errors)),
        "calculated_total_tokens": _sum_if_any(input_tokens, output_tokens),
        "provider_total_tokens": total_tokens,
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
    if "exact" in levels:
        return "exact"
    if "provider_count" in levels:
        return "provider_count"
    if "estimate" in levels:
        return "estimate"
    return "unavailable"


def _token_scalars(value: Any, prefix: str = "usage"):
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if "token" in str(key).lower() and not isinstance(item, (dict, list, bool)):
                try:
                    yield path, int(item)
                except (TypeError, ValueError):
                    pass
            yield from _token_scalars(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _token_scalars(item, f"{prefix}[{index}]")


def _audit_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = ((config.get("test_cases") or {}).get("token_accuracy") or {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "relative_tolerance": _bounded_float(
            raw.get("relative_tolerance"), DEFAULT_RELATIVE_TOLERANCE, 0.0
        ),
        "input_absolute_tolerance": _bounded_int(
            raw.get("input_absolute_tolerance"), DEFAULT_INPUT_ABSOLUTE_TOLERANCE, 0
        ),
        "output_absolute_tolerance": _bounded_int(
            raw.get("output_absolute_tolerance"), DEFAULT_OUTPUT_ABSOLUTE_TOLERANCE, 0
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
            for key in ("input", "tools", "response_format")
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

    if transport == "openai_responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "reasoning":
                reasoning.append(item.get("summary") or item.get("content") or item)
                visibility = "summary"
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
            if block_type in {"thinking", "redacted_thinking"}:
                reasoning.append(block.get("thinking") or block.get("data") or block)
                visibility = "summary"
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
        "answer": _semantic_text(answers),
        "reasoning": _semantic_text(reasoning),
        "thinking_visibility": visibility,
    }


def _thinking_requested(body: dict[str, Any]) -> bool:
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        if str(thinking.get("type") or "").lower() not in {"", "disabled", "none"}:
            return True
    if body.get("enable_thinking") is True:
        return True
    if body.get("reasoning_effort") not in (None, "", "none", "minimal"):
        return True
    for container_key in ("extra_body", "generationConfig"):
        container = body.get(container_key)
        if isinstance(container, dict) and _contains_enabled_thinking(container):
            return True
    return False


def _contains_enabled_thinking(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
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


def _request_has_external_context(body: dict[str, Any]) -> bool:
    if body.get("cachedContent") or body.get("cached_content"):
        return True
    if body.get("enable_search") is True or body.get("enable_code_interpreter") is True:
        return True
    extra = body.get("extra_body")
    return isinstance(extra, dict) and bool(extra.get("cached_content"))


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


def _iter_audit_exchanges(results: list[dict[str, Any]]):
    for result in results:
        audit = result.get("token_audit") or {}
        for exchange in audit.get("exchanges") or []:
            if isinstance(exchange, dict):
                yield exchange


def _sum_present(values) -> int | None:
    present = [int(value) for value in values if value is not None]
    return sum(present) if present else None


def _sum_if_any(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return (first or 0) + (second or 0)


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
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _openai_cached_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("input_tokens_details")
    return _optional_int(details.get("cached_tokens")) or 0 if isinstance(details, dict) else 0


def _looks_like_gemini_usage(usage: dict[str, Any]) -> bool:
    return any(
        key in usage
        for key in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount")
    )


def _bounded_float(value: Any, default: float, minimum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _bounded_int(value: Any, default: int, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default
