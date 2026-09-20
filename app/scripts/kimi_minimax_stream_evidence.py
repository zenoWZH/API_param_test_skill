"""Pure source-specific Chat/SSE observations; no network, credentials or dispatch.

Kimi documents DONE and usage in its final content chunk. MiniMax documents
chunked output and usage=true but no exact wire terminal or usage-only layout.
MiniMax SDK examples handle cumulative content; incremental and monotone-prefix
reconstruction are therefore distinguished for the fixed sample, never silently
treated as a universal transport rule. Reasoning text and native IDs stay out of
the returned observations.
"""
from __future__ import annotations

import hashlib
import json
import re

MAX_BYTES = 2 * 1024 * 1024
MODELS = {"moonshot": {"kimi-k3", "kimi-k2.7-code", "kimi-k2.6"},
          "minimax": {"MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"}}


class Invalid(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise Invalid(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def strict_json(value):
    def unique(pairs):
        out = {}
        for k, v in pairs:
            require(k not in out, "duplicate_json_key")
            out[k] = v
        return out
    def nonfinite(_value):
        raise Invalid("nonfinite_json_number")
    return json.loads(value, object_pairs_hook=unique, parse_constant=nonfinite)


def integer(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _base(source):
    return {"source_id": source, "pass": False, "native_envelope_verified": False,
            "stream_envelope_verified": False, "semantic_match": False, "usage_verified": False,
            "native_usage": None, "cache_count_reported": False, "reasoning_count_reported": False,
            "reasoning_content_observed": False, "reasoning_details_observed": False,
            "native_response_ids_retained": False, "reasoning_text_retained": False,
            "full_parameter_matrix_verified": False, "independent_token_exactness_verified": False,
            "thinking_switch_effect_verified": False, "cache_effect_verified": False,
            "usage_option_effect_verified": False, "diagnostics": []}


def _args(source, model, cap, expected):
    require(source in MODELS and model in MODELS[source], "unexpected_source_model")
    require(type(cap) is int and 256 <= cap <= 8192, "invalid_output_cap")
    require(type(expected) is dict and set(expected) == {"kind", "value"}
            and expected["kind"] in ("text", "json"), "invalid_expectation")


def _identity(value, source, model, *, streaming):
    require(type(value) is dict and not value.get("error"), "native_error_or_invalid_object")
    require(type(value.get("id")) is str and bool(value["id"]) and value.get("model") == model,
            "native_identity_mismatch")
    require(integer(value.get("created")), "invalid_created_timestamp")
    require(value.get("object") == ("chat.completion.chunk" if streaming else "chat.completion"),
            "invalid_native_object")
    native_status = value.get("base_resp")
    if source == "minimax" and (not streaming or native_status is not None):
        require(type(native_status) is dict and type(native_status.get("status_code")) is int
                and native_status["status_code"] == 0, "minimax_native_error_or_missing_status")
    return value["id"], value["model"], value["created"]


def _usage(value, cap, *, positive=True):
    require(type(value) is dict, "usage_missing_or_invalid")
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    require(all(integer(value.get(k)) for k in fields), "usage_counter_missing_or_invalid")
    p, c, t = (value[k] for k in fields)
    require(t == p + c and c <= cap and (not positive or p > 0 and c > 0), "usage_arithmetic_or_budget_mismatch")
    public = {k: value[k] for k in fields}
    cache, reasoning = [], None
    if "cached_tokens" in value:
        require(integer(value["cached_tokens"]) and value["cached_tokens"] <= p, "invalid_cached_count")
        cache.append(value["cached_tokens"])
        public["cached_tokens"] = value["cached_tokens"]
    for parent, name, maximum in (("prompt_tokens_details", "cached_tokens", p),
                                  ("completion_tokens_details", "reasoning_tokens", c)):
        if parent in value:
            require(type(value[parent]) is dict, "invalid_usage_detail_object")
            if name in value[parent]:
                count = value[parent][name]
                require(integer(count) and count <= maximum, "invalid_usage_detail_count")
                public[parent] = {name: count}
                if name == "cached_tokens": cache.append(count)
                else: reasoning = count
    require(len(set(cache)) <= 1, "conflicting_cache_counts")
    return {"native_usage": public, "usage_verified": True,
            "cache_count_reported": bool(cache), "reasoning_count_reported": reasoning is not None}


def _reasoning(message):
    content, details = message.get("reasoning_content"), message.get("reasoning_details")
    require(content is None or type(content) is str, "invalid_reasoning_content")
    require(details is None or type(details) is list, "invalid_reasoning_details")
    for item in details or []:
        require(type(item) is dict, "invalid_reasoning_detail")
        if "text" in item:
            require(type(item["text"]) is str, "invalid_reasoning_detail_text")
    return bool(content), bool(details)


def _text(message, *, delta):
    require(type(message) is dict, "invalid_message_or_delta")
    if "role" in message or not delta:
        require(message.get("role") == "assistant", "invalid_assistant_role")
    require(not message.get("tool_calls") and not message.get("function_call")
            and not message.get("refusal") and not message.get("audio_content")
            and not message.get("audio"), "unexpected_tool_refusal_or_media")
    text = message.get("content")
    require(type(text) is str or delta and text is None, "invalid_text_content")
    return text or "", _reasoning(message)


def _semantic(text, expected):
    if expected["kind"] == "text":
        return type(expected["value"]) is str and text == expected["value"]
    try:
        return canonical(strict_json(text)) == canonical(expected["value"])
    except (ValueError, TypeError, RecursionError):
        return False


def observe_message(payload, source, model, cap, expected):
    result = _base(source)
    try:
        _args(source, model, cap, expected)
        _identity(payload, source, model, streaming=False)
        choices = payload.get("choices")
        require(type(choices) is list and len(choices) == 1 and type(choices[0]) is dict, "invalid_choice_count")
        row = choices[0]
        require(type(row.get("index")) is int and row["index"] == 0, "invalid_choice_index")
        require(row.get("finish_reason") == "stop", "generation_not_complete")
        text, reasoning = _text(row.get("message"), delta=False)
        result.update(_usage(payload.get("usage"), cap))
        result.update(native_envelope_verified=True, returned_model=model, semantic_match=_semantic(text, expected),
                      reasoning_content_observed=reasoning[0], reasoning_details_observed=reasoning[1],
                      content_sha256=sha(text.encode()), parsed_message_sha256=sha(canonical(payload)),
                      native_finish_reason="stop", minimax_native_status_reported="base_resp" in payload)
        result["pass"] = result["semantic_match"]
        if not result["pass"]: result["diagnostics"].append("semantic_mismatch")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        result["diagnostics"].append(str(exc) if isinstance(exc, Invalid) else "invalid_native_payload")
    return result


def parse_frames(raw):
    """Full parsed events for P1M raw reports only, not permanent public facts."""
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "invalid_stream_bytes")
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    require(text.endswith("\n\n"), "partial_sse_frame")
    frames = []
    for block in re.split(r"\n\n+", text):
        if not block: continue
        event, data = None, []
        for line in block.split("\n"):
            if line.startswith(":"): continue
            name, sep, value = line.partition(":")
            require(bool(sep) and name in ("data", "event", "id", "retry"), "invalid_sse_field")
            value = value[1:] if value.startswith(" ") else value
            if name == "data": data.append(value)
            elif name == "event":
                require(event is None and bool(value), "invalid_sse_event")
                event = value
            elif name == "id": require("\x00" not in value, "invalid_sse_id")
            else: require(value.isascii() and value.isdigit(), "invalid_sse_retry")
        if not data:
            require(event is None, "event_without_data")
            continue
        joined = "\n".join(data)
        if joined == "[DONE]":
            frames.append({"event": event, "done": True})
        else:
            payload = strict_json(joined)
            require(type(payload) is dict, "invalid_sse_json_shape")
            frames.append({"event": event, "data": payload})
    require(bool(frames), "empty_sse_stream")
    return frames


def observe_stream(raw, source, model, cap, expected, *, transport_complete=False):
    result = _base(source)
    result.update(done_observed=False, native_finish_observed=False, usage_distribution={"absent": 0, "null": 0, "object": 0})
    try:
        _args(source, model, cap, expected)
        require(transport_complete is True, "http_response_not_complete")
        frames = parse_frames(raw)
        identity, terminal, done, role = None, False, False, False
        text_parts, usage_at_or_after_stop, native_statuses = [], [], 0
        for frame in frames:
            require(not done, "data_after_done")
            require(frame["event"] in (None, "message"), "unexpected_named_sse_event")
            if frame.get("done"):
                require(terminal, "done_before_native_stop")
                done = True
                continue
            value = frame["data"]
            current = _identity(value, source, model, streaming=True)
            require(identity is None or identity == current, "stream_identity_changed")
            identity = current
            native_statuses += "base_resp" in value
            choices = value.get("choices")
            require(type(choices) is list and len(choices) <= 1, "invalid_stream_choice_count")
            if choices:
                choice = choices[0]
                require(type(choice) is dict and type(choice.get("index")) is int and choice["index"] == 0,
                        "invalid_stream_choice_index")
                fragment, reasoning = _text(choice.get("delta"), delta=True)
                require(not terminal, "choice_after_native_stop")
                if fragment: text_parts.append(fragment)
                role |= choice["delta"].get("role") == "assistant"
                result["reasoning_content_observed"] |= reasoning[0]
                result["reasoning_details_observed"] |= reasoning[1]
                finish = choice.get("finish_reason")
                require(finish in (None, "stop"), "stream_generation_not_complete")
                terminal = finish == "stop"
            else:
                require(terminal and type(value.get("usage")) is dict, "unanchored_usage_only_chunk")
            usage_state = "absent" if "usage" not in value else "null" if value["usage"] is None else "object"
            result["usage_distribution"][usage_state] += 1
            if usage_state == "object":
                _usage(value["usage"], cap, positive=False)
                if terminal: usage_at_or_after_stop.append(value["usage"])
        require(terminal and role and text_parts, "missing_native_terminal_role_or_text")
        require(source != "moonshot" or done, "kimi_documented_done_missing")
        result.update(done_observed=done, native_finish_observed=True, native_finish_reason="stop",
                      terminal_kind="native_stop_and_done" if done else "native_stop_and_completed_http_eof",
                      documented_done_requirement=source == "moonshot", stream_envelope_verified=True,
                      native_envelope_verified=True, returned_model=model, native_frame_count=len(frames),
                      minimax_native_status_frame_count=native_statuses, raw_response_sha256=sha(raw),
                      native_usage_anchor_count=len(usage_at_or_after_stop))
        if usage_at_or_after_stop:
            require(all(canonical(u) == canonical(usage_at_or_after_stop[-1]) for u in usage_at_or_after_stop),
                    "conflicting_terminal_usage")
            result.update(_usage(usage_at_or_after_stop[-1], cap))
        incremental = "".join(text_parts)
        incremental_match = _semantic(incremental, expected)
        cumulative, cumulative_valid = "", source == "minimax"
        for fragment in text_parts:
            if not fragment.startswith(cumulative): cumulative_valid = False
            cumulative = fragment
        cumulative_match = cumulative_valid and _semantic(cumulative, expected)
        result["semantic_match"] = incremental_match or cumulative_match
        mode = ("equivalent_single_text_fragment" if len(text_parts) == 1 and result["semantic_match"]
                else "incremental_delta" if incremental_match else "cumulative_prefix_chain" if cumulative_match else "unmatched")
        result.update(content_reconstruction_mode=mode, arbitrary_stream_reconstruction_proven=False,
                      content_fragment_count=len(text_parts), incremental_candidate_sha256=sha(incremental.encode()),
                      cumulative_candidate_sha256=sha(cumulative.encode()) if cumulative_valid else None)
        result["pass"] = result["semantic_match"]
        if not result["pass"]: result["diagnostics"].append("semantic_mismatch")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        result["diagnostics"].append(str(exc) if isinstance(exc, Invalid) else "invalid_native_stream")
    return result


def attributed_type_rejection(payload, source, status, field):
    if type(payload) is not dict or field not in ("stream", "stream_options.include_usage"):
        return False
    error = payload.get("error")
    in_band = False
    if type(error) is not dict and source == "minimax":
        native = payload.get("base_resp")
        if type(native) is dict and type(native.get("status_code")) is int and native["status_code"] != 0:
            error, in_band = {"message": native.get("status_msg")}, True
    if type(error) is not dict or type(status) is not int or not (status in (400, 422) or status == 200 and in_band):
        return False
    message = error.get("message")
    if type(message) is not str or not re.search(r"bool|type|integer|invalid|valid boolean", message, re.I):
        return False
    if re.search(r"quota|balance|billing|credential|api.?key|authentication|rate.?limit|permission", message, re.I):
        return False
    param = error.get("param", error.get("field"))
    if param is not None:
        return param == field
    tokens = {field, field.rsplit(".", 1)[-1]}
    return any(re.search(r"(?<![\w.])" + re.escape(token) + r"(?![\w.])", message, re.I) for token in tokens)
