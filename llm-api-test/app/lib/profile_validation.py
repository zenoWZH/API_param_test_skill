from __future__ import annotations

import json
from typing import Any

from .deepseek_params import (
    extract_claude_tool_uses,
    extract_content,
    extract_native_function_calls,
    extract_openai_responses_function_calls,
    extract_openai_responses_text,
    extract_reasoning_content,
    extract_tool_calls,
)


OPENAI_TOOL_PROFILES = {
    "tool_calls",
    "tool_choice_required",
    "tool_calls_thinking",
    "deepseek_tool_strict",
    "glm_tools",
    "glm_tool_choice_auto",
    "glm_tool_calls",
    "glm_tool_stream",
    "glm_tool_calls_thinking",
    "qwen_tools",
    "qwen_tool_choice_auto",
    "qwen_tool_calls",
    "qwen_parallel_tool_calls",
    "qwen_tool_stream",
    "aliyun_tools",
    "aliyun_tool_choice_auto",
    "aliyun_tool_stream",
    "gemini_tools",
    "gemini_tool_choice_auto",
    "claude_tools",
    "claude_tool_choice_auto",
    "claude_parallel_tool_calls",
    "gpt5_chat_tools",
    "grok_tools",
    "kimi_k3_dynamic_tools",
}

NATIVE_TOOL_PROFILES = {
    "gemini_native_tools",
    "gemini_native_tool_config",
}

CLAUDE_NATIVE_TOOL_PROFILES = {
    "claude_native_tools",
    "claude_native_tool_choice_auto",
}

OPENAI_RESPONSES_TOOL_PROFILES = {
    "openai_responses_tools",
    "grok_responses_tools",
}

# Vertex fingerprint profiles: require usageMetadata.trafficType (absent on AI Studio).
GEMINI_VERTEX_FINGERPRINT_PROFILES = {
    "gemini_vertex_traffic_type",
    "gemini_vertex_labels",
    "gemini_vertex_service_tier_body",
    "gemini_vertex_request_type_header",
    "gemini_vertex_shared_request_type_header",
}

KIMI_K3_REASONING_PROFILES = {
    "kimi_k3_stream",
    "kimi_k3_stream_usage",
    "kimi_k3_reasoning_low",
    "kimi_k3_reasoning_high",
    "kimi_k3_reasoning_max",
    "kimi_k3_preserved_thinking",
    "kimi_k3_prompt_cache_key",
    "kimi_k3_dynamic_tools",
}

DEEPSEEK_REASONING_PROFILES = {
    "thinking_low",
    "thinking_enabled",
    "thinking_max",
    "tool_calls_thinking",
}

GLM_REASONING_PROFILES = {
    "glm_thinking_enabled",
    "glm_reasoning_low",
    "glm_reasoning_medium",
    "glm_reasoning_high",
    "glm_reasoning_xhigh",
    "glm_reasoning_max",
    "glm_tool_calls_thinking",
}

GLM_NON_REASONING_PROFILES = {
    "glm_thinking_disabled",
    "glm_reasoning_none",
    "glm_reasoning_minimal",
}

QWEN_REASONING_PROFILES = {
    "qwen_thinking_enabled",
    "qwen_thinking_budget",
    "qwen_preserve_thinking",
}

GEMINI_NATIVE_THOUGHT_SUMMARY_PROFILES = {
    "gemini_native_thinking_medium",
    "gemini_native_thinking_high",
}

OPENAI_REASONING_CONTEXT_PROFILES = {
    "openai_responses_reasoning_context_all_turns": "all_turns",
    "openai_responses_reasoning_context_current_turn": "current_turn",
}


def validate_profile_response(
    profile: str,
    response_json: dict[str, Any],
    result: Any,
    *,
    request_body: dict[str, Any] | None = None,
    transport: str = "chat_completions",
    tool_validation_mode: str = "auto",
    reference_source: str | None = None,
) -> str | None:
    if not result.success:
        return result.failure_classification or result.error_type or "request_failed"

    json_profiles = {
        "json_output",
        "deepseek_json_output_256",
        "qwen_response_format",
        "aliyun_json_object",
        "gemini_chat_response_mime_type",
        "gemini_native_response_mime_type",
        "gemini_native_response_schema",
        "gemini_native_response_json_schema",
        "gemini_native_response_format",
        "gpt5_chat_json",
        "openai_responses_json",
        "grok_json",
        "grok_responses_json",
    }
    if profile in json_profiles:
        content = extract_content(response_json)
        if profile in {"openai_responses_json", "grok_responses_json"} and not content.strip():
            content = extract_openai_responses_text(response_json)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return "json_parse"
        if not isinstance(parsed, dict):
            return "json_not_object"

    is_tool_probe = (
        profile in OPENAI_TOOL_PROFILES
        or profile in NATIVE_TOOL_PROFILES
        or profile in CLAUDE_NATIVE_TOOL_PROFILES
        or profile in OPENAI_RESPONSES_TOOL_PROFILES
    )
    protocol = _tool_validation_protocol(tool_validation_mode, transport)
    if is_tool_probe and protocol == "openai_compat":
        error = _validate_openai_tool_calls(response_json, request_body or {})
        if error:
            return error

    if is_tool_probe and protocol == "gemini_native":
        error = _validate_native_function_calls(response_json, request_body or {})
        if error:
            return error

    if is_tool_probe and protocol == "claude_native":
        error = _validate_claude_tool_uses(response_json, request_body or {})
        if error:
            return error

    if is_tool_probe and protocol == "openai_responses":
        error = _validate_openai_responses_function_calls(response_json, request_body or {})
        if error:
            return error

    if profile in {
        "stream_with_usage",
        "aliyun_stream_usage",
        "gpt5_chat_stream_usage",
        "openai_responses_stream_usage",
        "grok_stream_usage",
        "grok_responses_stream_usage",
    } and not result.usage:
        return "stream_usage_missing"

    expected_reasoning_context = OPENAI_REASONING_CONTEXT_PROFILES.get(profile)
    if expected_reasoning_context:
        response_reasoning = response_json.get("reasoning") or {}
        actual_context = (
            response_reasoning.get("context")
            if isinstance(response_reasoning, dict)
            else None
        )
        if actual_context != expected_reasoning_context:
            return "reasoning_context_mismatch"

    if profile in {"logprobs", "qwen_logprobs"}:
        choices = response_json.get("choices") or []
        if not choices or choices[0].get("logprobs") is None:
            return "logprobs_missing"

    if profile in KIMI_K3_REASONING_PROFILES:
        if not extract_reasoning_content(response_json).strip():
            return "reasoning_content_missing"

    if (
        reference_source == "deepseek_chat"
        and profile in DEEPSEEK_REASONING_PROFILES
        and not extract_reasoning_content(response_json).strip()
    ):
        return "reasoning_content_missing"

    if (
        reference_source == "deepseek_chat"
        and profile == "thinking_disabled"
        and extract_reasoning_content(response_json).strip()
    ):
        return "reasoning_content_unexpected"

    if (
        reference_source == "glm_openai_compat"
        and profile in GLM_REASONING_PROFILES
        and not extract_reasoning_content(response_json).strip()
    ):
        return "reasoning_content_missing"

    if (
        reference_source == "glm_openai_compat"
        and profile in GLM_NON_REASONING_PROFILES
        and extract_reasoning_content(response_json).strip()
    ):
        return "reasoning_content_unexpected"

    if (
        reference_source == "qwen_openai_compat"
        and profile in QWEN_REASONING_PROFILES
        and not extract_reasoning_content(response_json).strip()
    ):
        return "reasoning_content_missing"

    if (
        reference_source == "qwen_openai_compat"
        and profile == "qwen_thinking_disabled"
        and extract_reasoning_content(response_json).strip()
    ):
        return "reasoning_content_unexpected"

    if profile == "qwen_preserve_thinking":
        content = extract_content(response_json)
        if "215" not in content or "222" not in content:
            return "preserved_thinking_mismatch"

    if profile == "kimi_k3_preserved_thinking":
        content = extract_content(response_json)
        if "215" not in content or "222" not in content:
            return "preserved_thinking_mismatch"

    if profile in {"qwen_n", "gemini_n"}:
        choices = response_json.get("choices") or []
        expected_n = int((request_body or {}).get("n") or 1)
        if len(choices) < expected_n:
            return "n_choices_mismatch"

    if profile == "gemini_chat_candidate_count":
        choices = response_json.get("choices") or []
        generation_config = (request_body or {}).get("generationConfig") or {}
        expected_n = int(generation_config.get("candidateCount") or 1)
        if len(choices) < expected_n:
            return "n_choices_mismatch"

    if profile == "gemini_native_candidate_count":
        candidates = response_json.get("candidates") or []
        generation_config = (request_body or {}).get("generationConfig") or {}
        expected_n = int(generation_config.get("candidateCount") or 1)
        if len(candidates) < expected_n:
            return "n_choices_mismatch"

    if profile in GEMINI_VERTEX_FINGERPRINT_PROFILES:
        error = _validate_gemini_vertex_fingerprint(profile, response_json)
        if error:
            return error

    if (
        profile in GEMINI_NATIVE_THOUGHT_SUMMARY_PROFILES
        and not _has_gemini_native_thought_summary(response_json)
    ):
        return "thought_summary_missing"

    return None


def _has_gemini_native_thought_summary(response_json: dict[str, Any]) -> bool:
    for candidate in response_json.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if (
                isinstance(part, dict)
                and part.get("thought") is True
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            ):
                return True
    return False


def _validate_gemini_vertex_fingerprint(
    profile: str,
    response_json: dict[str, Any],
) -> str | None:
    usage = response_json.get("usageMetadata")
    if not isinstance(usage, dict):
        usage = {}
    traffic_type = usage.get("trafficType")
    if not isinstance(traffic_type, str) or not traffic_type.strip():
        return "vertex_traffic_type_missing"
    # Body serviceTier is an AI Studio field. Vertex responses should keep
    # trafficType and must not echo AI Studio serviceTier.
    if profile == "gemini_vertex_service_tier_body" and usage.get("serviceTier"):
        return "vertex_service_tier_unexpected"
    return None


def validate_tool_followup_response(
    response_json: dict[str, Any],
    result: Any,
    *,
    transport: str,
    tool_validation_mode: str = "auto",
) -> str | None:
    if not result.success:
        return result.failure_classification or result.error_type or "tool_followup_failed"
    protocol = _tool_validation_protocol(tool_validation_mode, transport)
    if protocol == "gemini_native":
        if extract_native_function_calls(response_json):
            return "tool_followup_unresolved_call"
        if not extract_content(response_json).strip():
            return "tool_followup_content_missing"
        return None
    if protocol == "claude_native":
        if extract_claude_tool_uses(response_json):
            return "tool_followup_unresolved_call"
        if not extract_content(response_json).strip():
            return "tool_followup_content_missing"
        return None
    if protocol == "openai_responses":
        if extract_openai_responses_function_calls(response_json):
            return "tool_followup_unresolved_call"
        if not extract_openai_responses_text(response_json).strip():
            return "tool_followup_content_missing"
        return None
    if extract_tool_calls(response_json):
        return "tool_followup_unresolved_call"
    if not extract_content(response_json).strip():
        return "tool_followup_content_missing"
    return None


def _validate_openai_tool_calls(
    response_json: dict[str, Any],
    request_body: dict[str, Any],
) -> str | None:
    calls = extract_tool_calls(response_json)
    if not calls:
        return "tool_calls_missing"
    declared = _declared_openai_tool_names(request_body)
    for call in calls:
        if not isinstance(call, dict):
            return "tool_call_malformed"
        if not isinstance(call.get("id"), str) or not call["id"].strip():
            return "tool_call_id_missing"
        if call.get("type") != "function":
            return "tool_call_malformed"
        function = call.get("function")
        if not isinstance(function, dict):
            return "tool_call_malformed"
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            return "tool_call_name_missing"
        if declared and name not in declared:
            return "tool_call_unknown_function"
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            return "tool_call_arguments_invalid"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return "tool_call_arguments_invalid"
        if not isinstance(parsed, dict):
            return "tool_call_arguments_invalid"
    return None


def _validate_native_function_calls(
    response_json: dict[str, Any],
    request_body: dict[str, Any],
) -> str | None:
    calls = extract_native_function_calls(response_json)
    if not calls:
        return "native_function_call_missing"
    declared = _declared_native_tool_names(request_body)
    for call in calls:
        name = call.get("name")
        if not isinstance(name, str) or not name.strip():
            return "tool_call_name_missing"
        if declared and name not in declared:
            return "tool_call_unknown_function"
        if not isinstance(call.get("args"), dict):
            return "tool_call_arguments_invalid"
    return None


def _validate_claude_tool_uses(
    response_json: dict[str, Any],
    request_body: dict[str, Any],
) -> str | None:
    calls = extract_claude_tool_uses(response_json)
    if not calls:
        return "native_function_call_missing"
    declared = _declared_claude_tool_names(request_body)
    for call in calls:
        if not isinstance(call.get("id"), str) or not call["id"].strip():
            return "tool_call_id_missing"
        name = call.get("name")
        if not isinstance(name, str) or not name.strip():
            return "tool_call_name_missing"
        if declared and name not in declared:
            return "tool_call_unknown_function"
        if not isinstance(call.get("input"), dict):
            return "tool_call_arguments_invalid"
    return None


def _validate_openai_responses_function_calls(
    response_json: dict[str, Any],
    request_body: dict[str, Any],
) -> str | None:
    calls = extract_openai_responses_function_calls(response_json)
    if not calls:
        return "native_function_call_missing"
    declared = _declared_openai_responses_tool_names(request_body)
    for call in calls:
        if not isinstance(call.get("call_id"), str) or not call["call_id"].strip():
            return "tool_call_id_missing"
        name = call.get("name")
        if not isinstance(name, str) or not name.strip():
            return "tool_call_name_missing"
        if declared and name not in declared:
            return "tool_call_unknown_function"
        arguments = call.get("arguments")
        if not isinstance(arguments, str):
            return "tool_call_arguments_invalid"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return "tool_call_arguments_invalid"
        if not isinstance(parsed, dict):
            return "tool_call_arguments_invalid"
    return None


def _declared_openai_tool_names(request_body: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    declarations: list[Any] = list(request_body.get("tools") or [])
    for message in request_body.get("messages") or []:
        if isinstance(message, dict):
            declarations.extend(message.get("tools") or [])
    for tool in declarations:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
        elif isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool.get("type") == "function":
            names.add(tool["name"])
    return names


def _declared_openai_responses_tool_names(request_body: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in request_body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _declared_native_tool_names(request_body: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in request_body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        for declaration in tool.get("functionDeclarations") or []:
            name = declaration.get("name") if isinstance(declaration, dict) else None
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _declared_claude_tool_names(request_body: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in request_body.get("tools") or []:
        name = tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _request_declares_native_tools(request_body: dict[str, Any]) -> bool:
    return bool(_declared_native_tool_names(request_body))


def _tool_validation_protocol(mode: str, transport: str) -> str:
    if mode == "auto":
        if transport == "gemini_generate_content":
            return "gemini_native"
        if transport == "claude_messages":
            return "claude_native"
        if transport == "openai_responses":
            return "openai_responses"
        return "openai_compat"
    if mode not in {"openai_compat", "gemini_native", "claude_native", "openai_responses"}:
        raise ValueError(
            "tool_validation_mode must be auto, openai_compat, gemini_native, "
            "claude_native, or openai_responses"
        )
    return mode
