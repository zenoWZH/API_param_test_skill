"""Bounded, source-specific requests for the official DeepSeek V4.1 Flash API.

This module builds requests, not evidence.  A case is only a planned probe until
the live runner validates its response.  The coverage ledger deliberately keeps
untested documentation, ignored-field acceptance, and observable behavior apart.
No legacy Flash results or provider aliases are used here.
"""
from __future__ import annotations

import base64
import copy
import json
import re
import struct
import zlib
from pathlib import Path
from typing import Any


MODEL = "deepseek-flash"
PROFILE_ID = "text/deepseek/deepseek/deepseek-v4.1-flash"
ROOT = Path(__file__).resolve().parents[1]
REFERENCE_FILES = (
    "references/deepseek_v4_1_flash_chat_sources_20260917.json",
    "references/deepseek_v4_1_flash_compat_sources_20260917.json",
)
CHAT = "openai_chat_completions"
RESPONSES = "openai_responses"
ANTHROPIC = "anthropic_messages"
PREFIX = "deepseek_beta_chat_prefix"
FIM = "openai_fim_completions_beta"
PATHS = {
    CHAT: "/chat/completions",
    RESPONSES: "/responses",
    ANTHROPIC: "/anthropic/v1/messages",
    PREFIX: "/beta/chat/completions",
    FIM: "/beta/completions",
}

_TEXT_PROMPT = "Reply with exactly the word READY, without punctuation."
_TOOL_PROMPT = (
    "Call add_numbers with a=2 and b=3. Do not calculate the answer yourself. "
    "Use the supplied tool and do not write an explanation."
)
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    "required": ["a", "b"],
    "additionalProperties": False,
}
_JSON_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["BLUE"]}},
    "required": ["answer"],
    "additionalProperties": False,
}
_JSON_PROMPT = 'Return exactly this json object: {"answer":"BLUE"}. No markdown.'
_PREFIX_JSON_CONTINUATION_SYSTEM = (
    "Finish the current assistant message as one complete JSON object with exactly one key, answer. "
    "Preserve its existing opening. The answer value is the concatenation of the uppercase characters "
    "supplied by the user, in their listed order. "
    "Do not add any other fields, markdown, or explanations."
)
_IMAGE_PROMPT = (
    "What is the solid background color of this image? "
    "Reply with exactly one English color word."
)
_FIM_PROMPT = (
    "Complete each country-to-capital pair with only the capital city.\n"
    "Germany -> Berlin\n"
    "Italy -> Rome\n"
    "Japan -> Tokyo\n"
    "France -> "
)
_FIM_SUFFIX = "\nEnd of table."
_PREFIX_STOP = "<END>"


def _red_png_base64() -> str:
    """Deterministic 128x128 RGB test fixture; no files or external image host."""
    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 128 for _ in range(128))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    return base64.b64encode(png).decode("ascii")


def _chat(prompt: str = _TEXT_PROMPT, *, stream: bool = False, thinking: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
        "max_tokens": 2048 if thinking else 512,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if thinking:
        body["reasoning_effort"] = "low"
    return body


def _prefix_json_continuation(prefix: str, *, stream: bool = False) -> dict[str, Any]:
    target = '{"answer":"BLUE"}'
    if not target.startswith(prefix):
        raise ValueError("JSON continuation fixture prefix must be an exact prefix of its target")
    body = _chat(
        "The characters for the answer string are: B, L, U, E. "
        "Use exactly these four characters in this order.",
        stream=stream,
    )
    body["messages"].insert(0, {"role": "system", "content": _PREFIX_JSON_CONTINUATION_SYSTEM})
    if prefix.endswith("B"):
        body["messages"][0]["content"] = (
            "Finish the current assistant message as exactly the target serialized JSON document. "
            "Preserve the characters already written. Use the JSON Unicode escape sequences exactly "
            "as shown; do not double-escape their backslashes or add any other text."
        )
        body["messages"][1]["content"] = (
            r'The target serialized JSON document is {"answer":"B\u004c\u0055\u0045"}. '
            "Complete the assistant message to exactly this document."
        )
    body["messages"].append({"role": "assistant", "content": prefix, "prefix": True})
    body["temperature"] = 0
    body["reasoning_effort"] = "none"
    body["response_format"] = {"type": "text"}
    return body


def _responses(prompt: str = _TEXT_PROMPT, *, stream: bool = False, thinking: bool = False) -> dict[str, Any]:
    return {
        "model": MODEL,
        "instructions": "Follow the user's output format precisely.",
        "input": prompt,
        "stream": stream,
        "max_output_tokens": 2048 if thinking else 512,
        "reasoning": {"effort": "low" if thinking else "none"},
    }


def _anthropic(prompt: str = _TEXT_PROMPT, *, stream: bool = False, thinking: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": MODEL,
        "system": "Follow the user's output format precisely.",
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
        "max_tokens": 2048 if thinking else 512,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if thinking:
        body["output_config"] = {"effort": "low"}
    return body


def _fim(*, stream: bool = False) -> dict[str, Any]:
    return {
        "model": MODEL,
        "prompt": _FIM_PROMPT,
        "stream": stream,
        "max_tokens": 512,
        "temperature": 0,
        "stop": ["\n"],
    }


def _tool(form: str, *, strict: bool = False) -> dict[str, Any]:
    common = {"name": "add_numbers", "description": "Return the sum of the two integer inputs."}
    if form == ANTHROPIC:
        return {**common, "input_schema": copy.deepcopy(_TOOL_SCHEMA)}
    function = {**common, "parameters": copy.deepcopy(_TOOL_SCHEMA)}
    if strict:
        function["strict"] = True
    return {"type": "function", "function": function} if form == CHAT else {"type": "function", **function}


def build_cases() -> list[dict[str, Any]]:
    """Return independent request dictionaries; this function never sends them.

    Smoke flags select the ten positive non-thinking baseline requests (both
    transports on all five forms). The full matrix adds parameter semantics,
    images, real tool-call output, and documented error boundaries. No case
    creates server-side files or executes a generated tool call.
    """
    cases: list[dict[str, Any]] = []

    def add(case_id: str, form: str, body: dict[str, Any], expected: dict[str, Any],
            targets: list[str], *, smoke: bool = False, path: str | None = None,
            acceptance_only: bool = False) -> None:
        cases.append({
            "id": case_id, "api_form": form, "path": path or PATHS[form],
            "body": copy.deepcopy(body), "expected": copy.deepcopy(expected),
            "target_parameters": list(dict.fromkeys(targets)), "smoke": smoke,
            "acceptance_only": acceptance_only,
        })

    text = {"kind": "text", "exact_text": "READY"}
    tool = {"kind": "tool", "tool_name": "add_numbers", "tool_arguments": {"a": 2, "b": 3}}
    image_expected = {"kind": "image", "contains": "red"}
    error = {"kind": "error", "status_codes": [400]}
    inline_image = "data:image/png;base64," + _red_png_base64()

    # Chat: independently exercise transport, output formats, tools and modes.
    body = _chat()
    body.update(temperature=0, response_format={"type": "text"}, user_id="v41_official_matrix")
    add("chat_text", CHAT, body, text,
        ["model", "messages", "max_tokens", "stream", "thinking", "temperature", "response_format", "user_id"], smoke=True)
    body = _chat(stream=True)
    body["stream_options"] = {"include_usage": True}
    add("chat_stream", CHAT, body, text, ["stream", "stream_options", "stream_options.include_usage"], smoke=True)
    body = _chat(thinking=True)
    body["top_p"] = 0.5
    add("chat_thinking_top_p", CHAT, body, {**text, "reasoning_contract": "schema"},
        ["thinking", "reasoning_effort", "top_p"])
    for effort in ("none", "high", "max"):
        body = _chat(thinking=effort != "none")
        body.pop("thinking")  # Exercise the documented effort switch on its own.
        body["reasoning_effort"] = effort
        body["max_tokens"] = 4096 if effort == "max" else 2048 if effort == "high" else 512
        add("chat_effort_" + effort, CHAT, body,
            {**text, **({"reasoning_contract": "schema"} if effort != "none" else {})}, ["reasoning_effort"])
    body = _chat(_JSON_PROMPT)
    body["response_format"] = {"type": "json_object"}
    add("chat_json", CHAT, body, {"kind": "json", "json_contains": {"answer": "BLUE"}, "exact_json": True}, ["response_format"])
    body = _chat(_TOOL_PROMPT)
    body.update(tools=[_tool(CHAT)], tool_choice="required")
    add("chat_tool_required", CHAT, body, tool, ["tools", "tool_choice"])
    body = _chat(_TOOL_PROMPT, stream=True)
    body.update(tools=[_tool(CHAT, strict=True)], tool_choice={"type": "function", "function": {"name": "add_numbers"}})
    add("chat_tool_strict_stream", CHAT, body, tool, ["tools", "tools[].function.strict", "tool_choice", "stream"], path="/beta/chat/completions")
    body = _chat(_TOOL_PROMPT, thinking=True)
    body.update(tools=[_tool(CHAT)], tool_choice="auto")
    add("chat_tool_thinking_auto", CHAT, body, {**tool, "reasoning_contract": "schema"}, ["thinking", "tools", "tool_choice"])
    body = _chat()
    body.update(logprobs=True, top_logprobs=3)
    add("chat_logprobs", CHAT, body, {**text, "require_logprobs": True}, ["logprobs", "top_logprobs"])
    body = _chat("Reply exactly: READY|END|AFTER")
    body["stop"] = "|END|"
    add("chat_stop", CHAT, body, {**text, "forbid_contains": "|END|"}, ["stop"])
    body = _chat()
    body.update(frequency_penalty=0.5, presence_penalty=0.5)
    add("chat_deprecated_penalties", CHAT, body, text, ["frequency_penalty", "presence_penalty"], acceptance_only=True)
    body = _chat()
    body["messages"] = [{"role": "user", "content": [
        {"type": "text", "text": _IMAGE_PROMPT},
        {"type": "image_url", "image_url": {"url": inline_image, "detail": "low"}},
    ]}]
    add("chat_inline_image", CHAT, body, image_expected, ["messages[].content[].image_url", "messages[].content[].image_url.detail"])
    body = _chat()
    body["stream_options"] = {"include_usage": True}
    add("chat_reject_nonstream_options", CHAT, body, {**error, "error_fields": ["stream_options", "stream"]}, ["stream_options"])
    body = _chat(_TOOL_PROMPT, thinking=True)
    body.update(tools=[_tool(CHAT)], tool_choice="required")
    add("chat_reject_thinking_required_tool", CHAT, body, {**error, "error_fields": ["tool_choice", "thinking", "required"]}, ["thinking", "tool_choice"])

    # Responses keeps its own schemas and never inherits Chat-only restrictions.
    body = _responses()
    body.update(temperature=0, user="v41_official_matrix")
    add("responses_text", RESPONSES, body, text,
        ["model", "input", "instructions", "max_output_tokens", "stream", "reasoning.effort", "temperature", "user"], smoke=True)
    body = _responses(stream=True)
    body["input"] = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": _TEXT_PROMPT}]}]
    add("responses_stream", RESPONSES, body, text,
        ["stream", "input[].type", "input[].role", "input[].content"], smoke=True)
    body = _responses(thinking=True)
    body["top_p"] = 0.5
    add("responses_thinking_top_p", RESPONSES, body, {**text, "reasoning_contract": "schema"}, ["reasoning.effort", "top_p"])
    for effort in ("high", "max"):
        prompt = (
            "Calculate 17 * 23 + 41 * 7. Work out the arithmetic carefully, then reply with only the integer result."
            if effort == "max" else _TEXT_PROMPT
        )
        body = _responses(prompt, thinking=True)
        body["reasoning"]["effort"] = effort
        body["max_output_tokens"] = 4096 if effort == "max" else 2048
        expected = {"kind": "text", "exact_text": "678" if effort == "max" else "READY", "reasoning_contract": "schema"}
        add("responses_effort_" + effort, RESPONSES, body, expected, ["reasoning.effort"])
    for format_name in ("json_object", "json_schema"):
        body = _responses(_JSON_PROMPT)
        body["text"] = {"format": {"type": format_name}}
        targets = ["text.format.type"]
        if format_name == "json_schema":
            body["text"]["format"].update(name="fixed_answer", schema=copy.deepcopy(_JSON_SCHEMA))
            targets += ["text.format.name", "text.format.schema"]
        add("responses_" + format_name, RESPONSES, body, {"kind": "json", "json_contains": {"answer": "BLUE"}, "exact_json": True}, targets)
    body = _responses(_TOOL_PROMPT, stream=True)
    body.update(tools=[_tool(RESPONSES)], tool_choice="required")
    add("responses_tool_stream", RESPONSES, body, tool,
        ["tools", "tools[].name", "tools[].description", "tools[].parameters", "tool_choice", "stream"])
    body = _responses(_TOOL_PROMPT, thinking=True)
    body.update(tools=[_tool(RESPONSES)], tool_choice="auto")
    add("responses_tool_thinking_auto", RESPONSES, body, {**tool, "reasoning_contract": "schema"}, ["tools", "tool_choice", "reasoning.effort"])
    body = _responses()
    body["input"] = [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": _IMAGE_PROMPT},
        {"type": "input_image", "image_url": inline_image, "detail": "low"},
    ]}]
    add("responses_inline_image", RESPONSES, body, image_expected,
        ["input[].content[].input_image", "input[].content[].image_url", "input[].content[].detail"])
    body = _responses()
    body["top_logprobs"] = 3
    add("responses_logprobs", RESPONSES, body, {**text, "require_logprobs": True}, ["top_logprobs"])
    body = _responses()
    body.update(store=True, background=False, metadata={"test": "v41"}, parallel_tool_calls=False,
                max_tool_calls=1, stream_options={"include_usage": True})
    body["text"] = {"format": {"type": "text"}, "verbosity": "low"}
    body["reasoning"]["summary"] = "auto"
    add("responses_ignored_fields", RESPONSES, body, text,
        ["store", "background", "metadata", "parallel_tool_calls", "max_tool_calls", "stream_options", "text.verbosity", "reasoning.summary"], acceptance_only=True)
    body = _responses()
    body["tools"] = [{"type": "custom", "name": "unsupported_custom_tool"}]
    add("responses_reject_custom_tool_name", RESPONSES, body,
        {**error, "error_fields": ["custom", "apply_patch", "unsupported_custom_tool"]}, ["tools", "tools[].type=custom"])
    body = _responses()
    body["input"] = [{"role": "user", "content": [{"type": "input_image"}]}]
    add("responses_reject_missing_image_source", RESPONSES, body,
        {**error, "error_fields": ["input_image", "image_url", "file_id"]}, ["input[].content[].image_url", "input[].content[].file_id"])
    body = _responses()
    body["input"] = [{"role": "system", "content": [{"type": "input_image", "image_url": inline_image}]}]
    add("responses_reject_system_image", RESPONSES, body,
        {**error, "error_fields": ["image", "system"]}, ["input[].content[].input_image"])

    # Anthropic uses the native tool, image, effort and streaming shapes.
    body = _anthropic()
    body.update(temperature=0, metadata={"user_id": "v41_official_matrix"})
    add("anthropic_text", ANTHROPIC, body, text,
        ["model", "messages", "system", "max_tokens", "stream", "thinking.type", "temperature", "metadata.user_id"], smoke=True)
    body = _anthropic(stream=True)
    body["messages"][0]["content"] = [{"type": "text", "text": _TEXT_PROMPT}]
    add("anthropic_stream", ANTHROPIC, body, text,
        ["stream", "messages[].content", "messages[].content[].text"], smoke=True)
    body = _anthropic(thinking=True)
    body["top_p"] = 0.5
    add("anthropic_thinking_top_p", ANTHROPIC, body, {**text, "reasoning_contract": "schema"}, ["thinking.type", "output_config.effort", "top_p"])
    for effort in ("high", "max"):
        body = _anthropic(thinking=True)
        body["output_config"]["effort"] = effort
        body["max_tokens"] = 4096 if effort == "max" else 2048
        add("anthropic_effort_" + effort, ANTHROPIC, body, {**text, "reasoning_contract": "schema"}, ["output_config.effort"])
    body = _anthropic(_TOOL_PROMPT)
    body.update(tools=[_tool(ANTHROPIC)], tool_choice={"type": "any"})
    add("anthropic_tool_any", ANTHROPIC, body, tool,
        ["tools[].name", "tools[].input_schema", "tools[].description", "tool_choice.type"])
    body = _anthropic(_TOOL_PROMPT, thinking=True, stream=True)
    body.update(tools=[_tool(ANTHROPIC)], tool_choice={"type": "auto"})
    add("anthropic_tool_thinking_stream", ANTHROPIC, body, {**tool, "reasoning_contract": "schema"},
        ["thinking.type", "tools[].name", "tools[].input_schema", "tool_choice.type", "stream"])
    body = _anthropic()
    body["messages"] = [{"role": "user", "content": [
        {"type": "text", "text": _IMAGE_PROMPT},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _red_png_base64()}},
    ]}]
    add("anthropic_inline_image", ANTHROPIC, body, image_expected,
        ["messages[].content[].type=image", "messages[].content[].source.type=base64"])
    body = _anthropic("Reply exactly: READY|END|AFTER")
    body["stop_sequences"] = ["|END|"]
    add("anthropic_stop", ANTHROPIC, body, {**text, "forbid_contains": "|END|", "finish_reasons": ["end_turn", "stop_sequence"]}, ["stop_sequences"])
    body = _anthropic(thinking=True)
    body["thinking"]["budget_tokens"] = 1024
    body.update(top_k=1, service_tier="auto")
    add("anthropic_ignored_fields", ANTHROPIC, body, {**text, "reasoning_contract": "schema"},
        ["thinking.budget_tokens", "top_k", "service_tier"], acceptance_only=True)
    body = _anthropic()
    body["messages"].append({"role": "assistant", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _red_png_base64()}}]})
    add("anthropic_reject_assistant_image", ANTHROPIC, body,
        {**error, "error_fields": ["image", "assistant"]}, ["messages[].content[].type=image"])

    # Prefix inherits Chat parameters, but its actual probes have a beta path.
    for stream, thinking in ((False, False), (True, False), (False, True)):
        body = _chat(
            'Continue the supplied assistant prefix to complete exactly the compact json object '
            '{"answer":"BLUE"}, then immediately append <END>. Do not add markdown or commentary.',
            stream=stream, thinking=thinking,
        )
        prefix = '{"answer": "'
        body["messages"].append({"role": "assistant", "content": prefix, "prefix": True})
        # Newlines are legal JSON whitespace and may be the first continuation.
        # A dedicated marker must not stop an otherwise valid JSON continuation.
        body["stop"] = [_PREFIX_STOP]
        targets = ["model", "messages", "max_tokens", "stream", "thinking", "stop",
                   "messages[-1].role", "messages[-1].content", "messages[-1].prefix"]
        if not thinking:
            body["temperature"] = 0
            targets.append("temperature")
        expected = {"kind": "prefix", "prefix": prefix, "json_contains": {"answer": "BLUE"}, "exact_json": True, "forbid_contains": _PREFIX_STOP}
        if thinking:
            body["messages"][-1]["reasoning_content"] = "The requested answer is the exact JSON object with the value BLUE."
            targets += ["messages[-1].reasoning_content", "reasoning_effort"]
        if stream:
            body["stream_options"] = {"include_usage": True}
            targets += ["stream_options", "stream_options.include_usage"]
        case_id = "prefix_thinking" if thinking else "prefix_stream" if stream else "prefix_text"
        add(case_id, PREFIX, body, expected, targets, smoke=not thinking)
        cases[-1]["notes"] = (
            "JSON string-boundary fixture validated in reports/deepseek_v41/prefix_open_quote_20260917_01; "
            "the model must generate the complete BLUE value. Historical bare-colon and partial-word failures "
            "remain in earlier reports and dedicated diagnostic cases."
        )
        if thinking:
            cases[-1]["notes"] = (
                "prefilled reasoning accepted; nullable returned reasoning does not prove a new reasoning chain; "
                + cases[-1]["notes"]
            )

    # Keep the original JSON observations above and independently diagnose the
    # official guide's code-fence example, mode switch and absence of stop.
    for default_thinking in (True, False):
        body = _chat(
            "Please write quick sort code in Python. Define only the function quick_sort(arr) "
            "that returns the input values in sorted order. Do not include examples or explanations."
        )
        body["messages"].append({"role": "assistant", "content": "```python\n", "prefix": True})
        body["stop"] = ["```"]
        targets = ["model", "messages", "max_tokens", "stream", "stop",
                   "messages[-1].role", "messages[-1].content", "messages[-1].prefix"]
        if default_thinking:
            body.pop("thinking")
            body["max_tokens"] = 4096
        else:
            body["temperature"] = 0
            targets += ["thinking", "temperature"]
        expected = {
            "kind": "prefix", "prefix": "```python\n",
            "python_function_name": "quick_sort", "python_arguments": ["arr"],
        }
        add("prefix_code_default_thinking" if default_thinking else "prefix_code_nonthinking",
            PREFIX, body, expected, targets)
    body = _prefix_json_continuation('{"answer":')
    add("prefix_json_no_stop", PREFIX, body,
        {"kind": "prefix", "prefix": '{"answer":', "json_contains": {"answer": "BLUE"}, "exact_json": True},
        ["model", "messages", "max_tokens", "stream", "thinking", "temperature", "reasoning_effort", "response_format",
         "messages[-1].role", "messages[-1].content", "messages[-1].prefix"])
    for variant in ("quote", "mode"):
        for stream in (False, True):
            prefix = '{"answer":"B' if variant == "quote" else '{"answer":'
            if variant == "quote":
                body = _prefix_json_continuation(prefix, stream=stream)
            else:
                body = _chat(_JSON_PROMPT, stream=stream)
                body["temperature"] = 0
                body["messages"].append({"role": "assistant", "content": prefix, "prefix": True})
            targets = ["model", "messages", "max_tokens", "stream", "thinking", "temperature",
                       "messages[-1].role", "messages[-1].content", "messages[-1].prefix"]
            if variant == "mode":
                body["response_format"] = {"type": "json_object"}
                targets.append("response_format")
            else:
                targets += ["reasoning_effort", "response_format"]
            expected = (
                {**error, "error_fields": ["response_format", "prefix"]}
                if variant == "mode" else
                {"kind": "prefix", "prefix": prefix, "json_contains": {"answer": "BLUE"}, "exact_json": True}
            )
            add("prefix_json_" + variant + ("_stream" if stream else ""), PREFIX, body,
                expected, targets)
            cases[-1]["notes"] = (
                "Partial-word JSON prefix uses an equivalent JSON Unicode serialization to avoid ambiguous literal word continuation; "
                "the original B prefix bytes and decoded complete BLUE target are preserved. "
                "Standard JSON decoding performs the equivalence; response bytes are never repaired."
                if variant == "quote" else
                "Official live HTTP 400 rejection: response_format json_object should not be used with prefix. "
                "This case validates the incompatible combination, not JSON generation. "
                "Evidence: reports/deepseek_v41/prefix_shapes_20260917_01."
            )
    for stream in (False, True):
        body = _chat(_JSON_PROMPT, stream=stream)
        body["temperature"] = 0
        prefix = '{"answer": "'
        body["messages"].append({"role": "assistant", "content": prefix, "prefix": True})
        add("prefix_json_open_quote" + ("_stream" if stream else ""), PREFIX, body,
            {"kind": "prefix", "prefix": prefix, "json_contains": {"answer": "BLUE"}, "exact_json": True},
            ["model", "messages", "max_tokens", "stream", "thinking", "temperature",
             "messages[-1].role", "messages[-1].content", "messages[-1].prefix"])
        cases[-1]["notes"] = (
            "Diagnostic with an open JSON value quote and no partial word; the model must generate the complete BLUE value. "
            "Previous partial-word prefix observations are retained separately."
        )

    # FIM is non-thinking and has its own 4K ceiling. Do not send Chat fields.
    body = _fim()
    add("fim_text", FIM, body, {"kind": "fim", "contains": "Paris"},
        ["model", "prompt", "max_tokens", "stream", "temperature", "stop"], smoke=True)
    body = _fim(stream=True)
    body.update(suffix=_FIM_SUFFIX, stream_options={"include_usage": True})
    add("fim_suffix_stream", FIM, body, {"kind": "fim", "contains": "Paris", "suffix": body["suffix"]},
        ["suffix", "stream", "stream_options", "stream_options.include_usage"], smoke=True)
    body = _fim()
    body["echo"] = True
    add("fim_echo", FIM, body, {"kind": "fim", "contains": "Paris", "echo_prefix": body["prompt"]}, ["echo"])
    body = _fim()
    body.update(logprobs=3, top_p=0.8)
    add("fim_logprobs_top_p", FIM, body, {"kind": "fim", "contains": "Paris", "require_logprobs": True}, ["logprobs", "top_p"])
    body = _fim()
    body.update(frequency_penalty=0.5, presence_penalty=0.5)
    add("fim_deprecated_penalties", FIM, body, {"kind": "fim", "contains": "Paris"},
        ["frequency_penalty", "presence_penalty"], acceptance_only=True)
    body = _fim()
    body["stream_options"] = {"include_usage": True}
    add("fim_reject_nonstream_options", FIM, body,
        {**error, "error_fields": ["stream_options", "stream"]}, ["stream_options"])
    body = _fim()
    body.update(echo=True, suffix=_FIM_SUFFIX)
    add("fim_reject_echo_suffix", FIM, body,
        {**error, "error_fields": ["echo", "suffix"]}, ["echo", "suffix"])

    for case in cases:
        if case["api_form"] == FIM and case["expected"]["kind"] != "error":
            case["expected"]["reconstructed_pattern"] = (
                re.escape(_FIM_PROMPT) + r"\s*Paris(?:\.|\s)*"
                + (re.escape(case["body"]["suffix"]) if "suffix" in case["body"] else "")
            )

    return cases


def documented_parameters() -> dict[str, dict[str, Any]]:
    """Read the dated documentation snapshot and expand the prefix delta."""
    forms: dict[str, dict[str, Any]] = {}
    for relative_path in REFERENCE_FILES:
        source = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
        for form, interface in source["interfaces"].items():
            forms[form] = copy.deepcopy(interface["parameters"])
    forms[PREFIX] = {**copy.deepcopy(forms[CHAT]), **forms[PREFIX]}
    return forms


def _uncovered_reason(form: str, parameter: str, row: dict[str, Any]) -> tuple[str, str]:
    state = row["state"]
    if state == "unknown":
        return "unknown_not_probed", "Official documentation does not establish support; no undocumented probe or cross-form inference."
    if "vision.request_limits" == parameter:
        return "documented_not_probed", "Only one small inline image is exercised; image-count, size, dimension and token limits are not tested."
    if parameter in ("messages[].content[].file", "input[].content[].file_id", "messages[].content[].source.type=file"):
        return "documented_not_probed", "Uploaded-file lifecycle is outside this bounded matrix; no file is created or deleted."
    if form == PREFIX:
        return "documented_not_probed", "Inherited Chat contract entry; no independent beta-prefix probe for this parameter."
    if state == "unsupported":
        return "documented_not_probed", "Documented unsupported behavior is retained; this matrix does not infer rejection or successful semantics from omission."
    if state == "ignored":
        return "documented_not_probed", "Documented ignored behavior is not established by an unexecuted or unrelated request."
    if parameter.startswith("headers."):
        return "documented_not_probed", "Transport headers are configured by the runner; this matrix does not independently compare header variants."
    if parameter == "messages[].reasoning_content" or "call_id" in parameter or parameter in (
        "input[].output", "messages[].content[].type=tool_use", "messages[].content[].type=tool_result",
        "messages[].content[].type=thinking", "messages[].content[].type=server_tool_use",
        "messages[].content[].type=web_search_tool_result",
    ):
        return "documented_not_probed", "Generating a tool call or reasoning is separate from replaying prior-turn blocks; history replay is not exercised here."
    if parameter == "messages[].content[].source.type=url":
        return "documented_not_probed", "Inline images are exercised; external URL download behavior is not tested."
    return "documented_not_probed", "Recorded from the official source; no dedicated request exercises this parameter in the bounded matrix."


def parameter_coverage() -> list[dict[str, Any]]:
    """One ledger row for every expanded contract entry, with no pass claims.

    ``planned_probe`` means the listed cases must still run. Even after a case
    passes, sampling clamp/no-effect claims and untested enum/range values remain
    documentation facts; request acceptance cannot prove statistical semantics.
    """
    cases = build_cases()
    rows: list[dict[str, Any]] = []
    for form, parameters in documented_parameters().items():
        for parameter, source in parameters.items():
            matching = [case for case in cases if case["api_form"] == form and parameter in case["target_parameters"]]
            if matching:
                acceptance_only = all(case["acceptance_only"] for case in matching)
                status = "planned_acceptance_probe" if acceptance_only else "planned_probe"
                reason = (
                    "Only request acceptance is planned; ignored/no-effect behavior is not empirically proven."
                    if acceptance_only else
                    "Listed cases exercise selected values or a documented negative boundary; not all enum values, ranges or lifecycle behavior."
                )
                if parameter in ("top_p", "temperature"):
                    reason += " Output sampling distributions and clamping/ignored semantics require separate statistical evidence."
            else:
                status, reason = _uncovered_reason(form, parameter, source)
            rows.append({
                "api_form": form,
                "parameter": parameter,
                "documentation_state": source["state"],
                "status": status,
                "case_ids": [case["id"] for case in matching],
                "reason": reason,
                "official_sources": list(source.get("official_sources", [])),
                "live_verified": False,
                "notes": "; ".join(dict.fromkeys(case["notes"] for case in matching if case.get("notes"))),
            })
    return rows
