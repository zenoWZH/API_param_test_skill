from __future__ import annotations

from typing import Any


DEEPSEEK_OFFICIAL_SOURCE = "https://api-docs.deepseek.com/api/create-chat-completion"
GLM_OFFICIAL_SOURCE = "OpenAI-compatible GLM Chat Completions provider documentation"
GEMINI_OFFICIAL_SOURCE = "https://ai.google.dev/gemini-api/docs/openai"
CLAUDE_OFFICIAL_SOURCE = "https://platform.claude.com/docs/en/api/messages"


DEEPSEEK_PARAM_ROWS: list[dict[str, str]] = [
    {"parameter": "messages", "official": "required; system/user/assistant/tool messages", "local": "supported", "coverage": "all profiles"},
    {"parameter": "model", "official": "deepseek-v4-flash | deepseek-v4-pro", "local": "supported", "coverage": "provider default or per-profile override"},
    {"parameter": "thinking", "official": "enabled | disabled; default enabled", "local": "supported", "coverage": "throughput disabled; compatibility enabled/disabled"},
    {"parameter": "reasoning_effort", "official": "low | high | max; model-specific aliases normalized", "local": "supported", "coverage": "thinking_low / thinking_enabled / thinking_max"},
    {"parameter": "max_tokens", "official": "nullable integer", "local": "supported", "coverage": "default and profile override"},
    {"parameter": "response_format", "official": "text | json_object", "local": "supported", "coverage": "json_output profile"},
    {"parameter": "stop", "official": "string or up to 16 strings", "local": "supported", "coverage": "stop_sequences profile"},
    {"parameter": "stream", "official": "boolean SSE stream", "local": "supported", "coverage": "basic_stream / stream_with_usage"},
    {"parameter": "stream_options.include_usage", "official": "usage chunk before data: [DONE]", "local": "supported", "coverage": "stream_with_usage profile"},
    {"parameter": "temperature", "official": "0..2", "local": "supported", "coverage": "sampling_non_thinking profile"},
    {"parameter": "top_p", "official": "0..1", "local": "supported", "coverage": "sampling_non_thinking profile"},
    {"parameter": "tools", "official": "function tools, max 128", "local": "supported", "coverage": "tool_calls fixture"},
    {"parameter": "tool_choice", "official": "none | auto | required | named function", "local": "supported", "coverage": "tool_calls uses required"},
    {"parameter": "logprobs", "official": "boolean", "local": "supported/requested", "coverage": "logprobs profile"},
    {"parameter": "top_logprobs", "official": "0..20; requires logprobs=true", "local": "supported/requested", "coverage": "logprobs profile"},
    {"parameter": "user_id", "official": "cache/isolation identifier", "local": "supported", "coverage": "cache and long_context profiles"},
    {"parameter": "frequency_penalty", "official": "deprecated; no effect", "local": "filtered by default", "coverage": "allow_deprecated=true can send for probing"},
    {"parameter": "presence_penalty", "official": "deprecated; no effect", "local": "filtered by default", "coverage": "allow_deprecated=true can send for probing"},
    {"parameter": "assistant.prefix", "official": "beta; requires beta base_url", "local": "not profiled", "coverage": "out of current scope"},
]


GLM_PARAM_ROWS: list[dict[str, str]] = [
    {"parameter": "messages", "official": "required chat messages", "local": "supported", "coverage": "all profiles"},
    {"parameter": "model", "official": "glm model name, e.g. glm-5.2", "local": "supported", "coverage": "provider default or frontend override"},
    {"parameter": "stream", "official": "boolean SSE stream", "local": "supported", "coverage": "basic_stream"},
    {"parameter": "thinking.type", "official": "enabled | disabled; enabled by default", "local": "supported", "coverage": "glm_thinking_enabled / glm_thinking_disabled"},
    {"parameter": "thinking.clear_thinking", "official": "false preserves reasoning across turns", "local": "supported", "coverage": "glm_clear_thinking / glm_tool_calls_thinking"},
    {"parameter": "reasoning_effort", "official": "none | minimal | low | medium | high | xhigh | max", "local": "supported", "coverage": "glm_reasoning_*"},
    {"parameter": "do_sample", "official": "boolean sampling switch", "local": "supported", "coverage": "glm_do_sample"},
    {"parameter": "temperature", "official": "default 1.0; tune instead of top_p when possible", "local": "supported", "coverage": "glm_temperature"},
    {"parameter": "top_p", "official": "default 0.95; tune instead of temperature when possible", "local": "supported", "coverage": "glm_top_p"},
    {"parameter": "max_tokens", "official": "GLM-5.2 supports up to 131072 output tokens", "local": "supported", "coverage": "glm_max_tokens"},
    {"parameter": "tool_stream", "official": "stream tool-call argument construction", "local": "supported", "coverage": "glm_tool_stream"},
    {"parameter": "tools", "official": "OpenAI-compatible function tools", "local": "supported", "coverage": "glm_tools / glm_tool_stream / glm_tool_calls_thinking"},
    {"parameter": "tool_choice", "official": "OpenAI-compatible tool choice", "local": "supported", "coverage": "glm_tool_choice_auto / glm_tool_stream / glm_tool_calls_thinking"},
    {"parameter": "stop", "official": "string or string array", "local": "supported", "coverage": "stop_sequences"},
    {"parameter": "response_format", "official": "json_object structured output", "local": "supported", "coverage": "json_output"},
    {"parameter": "request_id", "official": "6-64 character request identifier", "local": "supported", "coverage": "glm_request_id"},
    {"parameter": "user_id", "official": "6-128 character end-user identifier", "local": "supported", "coverage": "glm_user_id"},
    {"parameter": "response.reasoning_content", "official": "thinking content returned separately from content", "local": "validated", "coverage": "GLM thinking/reasoning profiles"},
]


GEMINI_PARAM_ROWS: list[dict[str, str]] = [
    {"parameter": "messages", "official": "required chat messages", "local": "supported", "coverage": "all profiles"},
    {"parameter": "model", "official": "gemini model name, e.g. gemini-2.5-flash", "local": "supported", "coverage": "provider default or frontend override"},
    {"parameter": "reasoning_effort", "official": "low | medium | high; maps to thinking budget", "local": "supported", "coverage": "thinking_enabled"},
    {"parameter": "max_tokens", "official": "integer output budget", "local": "supported", "coverage": "default and profile override"},
    {"parameter": "response_format", "official": "json_object / json_schema structured output", "local": "supported", "coverage": "json_output profile"},
    {"parameter": "stop", "official": "string or string array", "local": "supported", "coverage": "stop_sequences profile"},
    {"parameter": "stream", "official": "boolean SSE stream", "local": "supported", "coverage": "basic_stream / stream_with_usage"},
    {"parameter": "stream_options.include_usage", "official": "OpenAI-compatible usage stream option", "local": "supported", "coverage": "stream_with_usage profile"},
    {"parameter": "temperature", "official": "sampling temperature", "local": "supported", "coverage": "sampling_non_thinking profile"},
    {"parameter": "top_p", "official": "nucleus sampling", "local": "supported", "coverage": "sampling_non_thinking profile"},
    {"parameter": "n", "official": "candidateCount; multiple completions", "local": "supported", "coverage": "qwen_n profile"},
    {"parameter": "presence_penalty", "official": "presence penalty", "local": "supported", "coverage": "qwen_presence_penalty profile"},
    {"parameter": "seed", "official": "deterministic sampling seed", "local": "supported", "coverage": "qwen_seed profile"},
    {"parameter": "tools", "official": "OpenAI-compatible function tools", "local": "supported", "coverage": "tool_calls profile"},
    {"parameter": "tool_choice", "official": "OpenAI-compatible tool choice", "local": "supported", "coverage": "tool_calls profile"},
    {"parameter": "logprobs", "official": "provider/model-dependent", "local": "supported/requested", "coverage": "logprobs profile"},
    {"parameter": "top_logprobs", "official": "provider/model-dependent", "local": "supported/requested", "coverage": "logprobs profile"},
    {"parameter": "thinking", "official": "not part of Gemini OpenAI-compatible profile; use reasoning_effort", "local": "expected_unsupported", "coverage": "skipped for gemini family"},
    {"parameter": "user_id", "official": "not part of Gemini OpenAI-compatible profile", "local": "expected_unsupported", "coverage": "skipped for gemini family"},
]


CLAUDE_PARAM_ROWS: list[dict[str, str]] = [
    {"parameter": "messages", "official": "required; user/assistant messages", "local": "supported", "coverage": "claude_native_* profiles"},
    {"parameter": "model", "official": "Claude model id, e.g. claude-sonnet-4-6", "local": "supported", "coverage": "provider default or frontend override"},
    {"parameter": "system", "official": "top-level system prompt", "local": "supported", "coverage": "claude_native_system"},
    {"parameter": "max_tokens", "official": "required integer output budget", "local": "supported", "coverage": "claude_native_max_tokens"},
    {"parameter": "stream", "official": "boolean SSE stream", "local": "supported", "coverage": "claude_native_stream"},
    {"parameter": "stop_sequences", "official": "custom stop strings", "local": "supported", "coverage": "claude_native_stop_sequences"},
    {"parameter": "temperature", "official": "0..1", "local": "supported", "coverage": "claude_native_temperature"},
    {"parameter": "top_p", "official": "nucleus sampling; do not combine with temperature for Claude", "local": "supported", "coverage": "claude_native_top_p"},
    {"parameter": "tools", "official": "native tools with input_schema", "local": "supported", "coverage": "claude_native_tools"},
    {"parameter": "tool_choice", "official": "auto | any | tool | none", "local": "supported", "coverage": "claude_native_tool_choice_auto"},
    {"parameter": "thinking", "official": "enabled+budget_tokens | adaptive | disabled (model-dependent)", "local": "supported", "coverage": "claude_native_thinking_* profiles"},
    {"parameter": "metadata", "official": "request metadata object", "local": "supported", "coverage": "claude_native_metadata"},
    {"parameter": "max_completion_tokens", "official": "OpenAI-compatible alias, not native Messages", "local": "expected_unsupported", "coverage": "claude_openai_compat only"},
    {"parameter": "parallel_tool_calls", "official": "OpenAI-compatible field, not native Messages", "local": "expected_unsupported", "coverage": "claude_openai_compat only"},
    {"parameter": "response_format", "official": "not part of Claude Messages", "local": "expected_unsupported", "coverage": "skipped for claude native family"},
    {"parameter": "logprobs", "official": "not part of Claude Messages", "local": "expected_unsupported", "coverage": "skipped for claude native family"},
    {"parameter": "user_id", "official": "use metadata.user_id instead", "local": "expected_unsupported", "coverage": "claude_native_metadata"},
    {"parameter": "reasoning_effort", "official": "OpenAI-compatible field, not native Messages", "local": "expected_unsupported", "coverage": "skipped for claude native family"},
]


def param_rows_for_family(family: str, supported_only: bool = False) -> list[dict[str, str]]:
    if family == "deepseek":
        rows = DEEPSEEK_PARAM_ROWS
    elif family == "glm":
        rows = GLM_PARAM_ROWS
    elif family == "gemini":
        rows = GEMINI_PARAM_ROWS
    elif family == "claude":
        rows = CLAUDE_PARAM_ROWS
    else:
        from .reference_specs import (
            default_reference_source_for_family,
            reference_param_rows,
        )

        rows = reference_param_rows(default_reference_source_for_family(family))
    if supported_only:
        return [row for row in rows if row.get("local") != "expected_unsupported"]
    return rows


def official_source_for_family(family: str) -> str:
    if family == "deepseek":
        return DEEPSEEK_OFFICIAL_SOURCE
    if family == "glm":
        return GLM_OFFICIAL_SOURCE
    if family == "gemini":
        return GEMINI_OFFICIAL_SOURCE
    if family == "claude":
        return CLAUDE_OFFICIAL_SOURCE
    from .reference_specs import default_reference_source_for_family, get_reference_source

    source = get_reference_source(default_reference_source_for_family(family))
    official = source.get("official_sources") or []
    return str(official[0]) if official else str(source.get("label") or family)


def param_spec_payload(family: str, supported_only: bool = False) -> dict[str, Any]:
    return {
        "family": family,
        "official_source": official_source_for_family(family),
        "supported_only": supported_only,
        "comparison": param_rows_for_family(family, supported_only=supported_only),
    }
