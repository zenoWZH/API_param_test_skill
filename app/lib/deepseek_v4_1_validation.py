"""Envelope and semantic checks for the source-scoped V4.1 Flash matrix.

Acceptance establishes the exact case's response semantics, not that ignored
sampling controls had an effect. No response content is evaluated as code.
"""
from __future__ import annotations

import ast
import json
import math
import re
from typing import Any

MODEL_IDS = frozenset({"deepseek-flash"})


def _string(value: Any, field: str, *, nullable: bool = False) -> str:
    if value is None and nullable:
        return ""
    if not isinstance(value, str):
        raise ValueError(field + " must be a string" + (" or null" if nullable else ""))
    return value


def _objects(value: Any, field: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(field + " must be an array of objects")
    return value


def strict_json(value: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = item
        return result

    def constant(value):
        raise ValueError("non-finite JSON number: " + value)

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def parse_sse(raw: str) -> tuple[list[dict], bool]:
    """Parse complete SSE records; never silently skip malformed JSON frames."""
    frames, done = [], False
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    for record in normalized.split("\n\n"):
        if not record.strip():
            continue
        event, data = "message", []
        for line in record.split("\n"):
            if line.startswith(":") or not line:
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
        if not data:
            continue
        value = "\n".join(data)
        if value == "[DONE]":
            if done:
                raise ValueError("duplicate SSE [DONE]")
            done = True
            continue
        if done:
            raise ValueError("SSE data after [DONE]")
        payload = strict_json(value)
        if not isinstance(payload, dict):
            raise ValueError("SSE payload must be an object")
        frames.append({"event": event, "data": payload})
    if not frames:
        raise ValueError("no JSON SSE frames")
    return frames, done


def _chat_stream(frames: list[dict], done: bool, fim: bool) -> dict:
    result = {"model": None, "usage": None, "choices": [{"index": 0, "finish_reason": None}]}
    content, reasoning, tools, logprobs = [], [], {}, []
    terminal, reasoning_seen, reasoning_nonnull = False, False, False
    for frame in frames:
        item = frame["data"]
        if "error" in item:
            raise ValueError("error in SSE stream: " + json.dumps(item["error"], ensure_ascii=False))
        if item.get("object") not in ({"text_completion", "text_completion.chunk"} if fim else {"chat.completion.chunk"}):
            raise ValueError("unexpected completion stream object")
        if not isinstance(item.get("choices"), list):
            raise ValueError("stream chunk lacks choices list")
        if item.get("model"):
            if result["model"] and result["model"] != item["model"]:
                raise ValueError("model identity changed inside stream")
            result["model"] = item["model"]
        if item.get("usage") is not None:
            result["usage"] = item["usage"]
        for choice in _objects(item["choices"], "stream choices"):
            if choice.get("index", 0) != 0:
                raise ValueError("unexpected extra choice in stream")
            delta = choice if fim else choice.get("delta", {})
            if not isinstance(delta, dict) or (not fim and delta.get("role", "assistant") != "assistant"):
                raise ValueError("invalid assistant stream delta")
            text = _string(delta.get("text" if fim else "content"), "stream text", nullable=True)
            if terminal:
                raise ValueError("choice after terminal choice")
            if text:
                content.append(text)
            if "reasoning_content" in delta:
                reasoning_seen = True
                reasoning_nonnull |= delta["reasoning_content"] is not None
                reasoning.append(_string(delta["reasoning_content"], "reasoning_content", nullable=True))
            for call in _objects(delta.get("tool_calls", []), "stream tool_calls"):
                index = call.get("index", 0)
                current = tools.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                for field in ("id", "type"):
                    if call.get(field):
                        current[field] = call[field]
                function = call.get("function", {})
                if not isinstance(function, dict):
                    raise ValueError("stream tool function must be an object")
                for field in ("name", "arguments"):
                    if function.get(field):
                        current["function"][field] += _string(function[field], "tool function " + field)
            if choice.get("logprobs"):
                logprobs.append(choice["logprobs"])
            if choice.get("finish_reason") is not None:
                terminal = True
                result["choices"][0]["finish_reason"] = choice["finish_reason"]
    choice = result["choices"][0]
    if fim:
        choice["text"] = "".join(content)
    else:
        choice["message"] = {"role": "assistant", "content": "".join(content), "tool_calls": list(tools.values())}
        if reasoning_seen:
            choice["message"]["reasoning_content"] = "".join(reasoning) if reasoning_nonnull else None
    choice["logprobs"] = logprobs
    result["_stream_terminal"] = bool(done and terminal)
    return result


def _responses_stream(frames: list[dict]) -> dict:
    """Reconcile streamed text/tool arguments with every completed snapshot.

    A correct final response cannot repair corrupt or missing deltas already
    delivered to a streaming client. The matrix uses message and function-call
    output; other item types still retain their added/done/terminal identity.
    """
    terminal = None
    items: dict[int, dict] = {}
    response_identity: dict[str, str] = {}
    last_sequence = -1

    def index(value: dict, field: str) -> int:
        found = value.get(field)
        if type(found) is not int or found < 0:
            raise ValueError("Responses " + field + " must be a nonnegative integer")
        return found

    def item_state(value: dict) -> dict:
        state = items.get(index(value, "output_index"))
        if state is None or state["done"] is not None:
            raise ValueError("Responses event outside an active output item")
        if "item_id" in value and value["item_id"] != state["initial"].get("id"):
            raise ValueError("Responses stream item_id differs from its output index")
        return state

    def part_state(value: dict) -> dict:
        state = item_state(value)
        if value.get("item_id") != state["initial"].get("id"):
            raise ValueError("Responses content event lacks its matching item_id")
        part = state["parts"].get(index(value, "content_index"))
        if part is None or part["done"] is not None:
            raise ValueError("Responses event outside an active content part")
        return part

    def check_item(state: dict, completed: dict) -> None:
        initial = state["initial"]
        identity_fields = ("id", "type", "role") if initial["type"] == "message" else ("id", "type", "name", "call_id")
        if any(key in initial and key in completed and completed[key] != initial[key] for key in identity_fields):
            raise ValueError("Responses output item identity changed inside stream")
        if initial["type"] == "message":
            content = _objects(completed.get("content"), "Responses completed message content")
            if set(state["parts"]) != set(range(len(content))):
                raise ValueError("Responses completed content differs from streamed content indexes")
            for part_index, value in enumerate(content):
                part = state["parts"][part_index]
                if (part["done"] is None or value.get("type") != part["done"].get("type")
                        or value.get("text") != part["done"].get("text")):
                    raise ValueError("Responses completed content differs from streamed content part")
        elif initial["type"] == "function_call":
            if not state["arguments_done"] or completed.get("arguments") != state["arguments"]:
                raise ValueError("Responses completed tool arguments differ from streamed arguments")

    for frame in frames:
        if terminal is not None:
            raise ValueError("Responses event after terminal response")
        value = frame["data"]
        event = value.get("type", frame["event"])
        if frame["event"] not in {"message", event}:
            raise ValueError("Responses SSE event name differs from payload type")
        if "sequence_number" in value:
            sequence = index(value, "sequence_number")
            if sequence <= last_sequence:
                raise ValueError("Responses stream sequence_number is not increasing")
            last_sequence = sequence
        if event in {"error", "response.error"}:
            raise ValueError("error in Responses stream: " + json.dumps(value, ensure_ascii=False))
        if event in {"response.created", "response.in_progress", "response.completed", "response.incomplete", "response.failed"}:
            response = value.get("response")
            if not isinstance(response, dict):
                raise ValueError("Responses lifecycle event requires a response object")
            for field in ("id", "model"):
                identity = _string(response.get(field), "Responses response " + field)
                if not identity or field in response_identity and response_identity[field] != identity:
                    raise ValueError("Responses response identity changed inside stream")
                response_identity[field] = identity
        if event == "response.output_item.added":
            output_index = index(value, "output_index")
            initial = value.get("item")
            if (output_index in items or not isinstance(initial, dict)
                    or not isinstance(initial.get("id"), str) or not initial["id"]
                    or not isinstance(initial.get("type"), str)):
                raise ValueError("invalid or duplicate Responses output item start")
            if any(state["initial"]["id"] == initial["id"] for state in items.values()):
                raise ValueError("duplicate Responses output item id")
            items[output_index] = {"initial": initial, "parts": {}, "done": None,
                                   "arguments": (_string(initial.get("arguments", ""), "initial tool arguments")
                                                 if initial["type"] == "function_call" else ""),
                                   "arguments_done": False}
        elif event == "response.content_part.added":
            state = item_state(value)
            content_index = index(value, "content_index")
            part = value.get("part")
            if (state["initial"]["type"] != "message" or value.get("item_id") != state["initial"]["id"]
                    or content_index in state["parts"] or not isinstance(part, dict)):
                raise ValueError("invalid or duplicate Responses content part start")
            state["parts"][content_index] = {
                "initial": part, "done": None, "text_done": False,
                "text": _string(part.get("text", ""), "initial output text") if part.get("type") == "output_text" else "",
            }
        elif event in {"response.output_text.delta", "response.output_text.done"}:
            part = part_state(value)
            if part["initial"].get("type") != "output_text" or part["text_done"]:
                raise ValueError("Responses output text outside an active text stream")
            if event.endswith(".delta"):
                part["text"] += _string(value.get("delta"), "Responses output text delta")
            else:
                if _string(value.get("text"), "Responses done text") != part["text"]:
                    raise ValueError("Responses done text differs from streamed text deltas")
                part["text_done"] = True
        elif event == "response.content_part.done":
            part = part_state(value)
            completed = value.get("part")
            if not isinstance(completed, dict) or completed.get("type") != part["initial"].get("type"):
                raise ValueError("Responses completed content part has a different type")
            if completed.get("type") == "output_text" and (not part["text_done"] or completed.get("text") != part["text"]):
                raise ValueError("Responses content part differs from streamed text")
            part["done"] = completed
        elif event in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            state = item_state(value)
            if (state["initial"]["type"] != "function_call" or value.get("item_id") != state["initial"]["id"]
                    or state["arguments_done"]):
                raise ValueError("Responses tool arguments outside an active function call")
            if event.endswith(".delta"):
                state["arguments"] += _string(value.get("delta"), "Responses tool argument delta")
            else:
                if _string(value.get("arguments"), "Responses done arguments") != state["arguments"]:
                    raise ValueError("Responses done arguments differ from streamed argument deltas")
                state["arguments_done"] = True
        elif event == "response.output_item.done":
            state = item_state(value)
            completed = value.get("item")
            if not isinstance(completed, dict):
                raise ValueError("Responses output item done requires an item object")
            check_item(state, completed)
            state["done"] = completed
        if event in {"response.completed", "response.incomplete", "response.failed"}:
            terminal = dict(value["response"])
            terminal["_terminal_event"] = event
    if terminal is None:
        raise ValueError("Responses stream lacks terminal response")
    output = _objects(terminal.get("output"), "Responses terminal output")
    if not items or set(items) != set(range(len(output))):
        raise ValueError("Responses terminal output lacks matching streamed output items")
    for output_index, completed in enumerate(output):
        state = items[output_index]
        if state["done"] is None:
            raise ValueError("Responses terminal output differs from completed streamed output item")
        for field in ("id", "type", "role", "name", "call_id"):
            if field in state["done"] and field in completed and state["done"][field] != completed[field]:
                raise ValueError("Responses terminal output identity differs from streamed output item")
        check_item(state, completed)
    terminal["_stream_terminal"] = terminal["_terminal_event"] == "response.completed"
    return terminal


def _anthropic_stream(frames: list[dict]) -> dict:
    result, blocks, usage, open_blocks = {}, {}, {}, set()
    started, stopped, message_delta = False, False, False
    for frame in frames:
        value = frame["data"]
        event = value.get("type", frame["event"])
        if stopped:
            raise ValueError("Anthropic event after message_stop")
        if event == "error":
            raise ValueError("error in Anthropic stream: " + json.dumps(value, ensure_ascii=False))
        if event == "message_start":
            if started:
                raise ValueError("duplicate message_start")
            started = True
            result = dict(value.get("message") or {})
            usage.update(result.get("usage") or {})
        elif event == "content_block_start":
            if not started or message_delta:
                raise ValueError("content block outside active message")
            index = value.get("index")
            if type(index) is not int or index < 0 or index in blocks:
                raise ValueError("duplicate content_block_start")
            blocks[index] = dict(value.get("content_block") or {})
            open_blocks.add(index)
        elif event == "content_block_delta":
            index = value.get("index")
            if index not in open_blocks:
                raise ValueError("content delta without block start")
            block, delta = blocks[index], value.get("delta") or {}
            kind = delta.get("type")
            for delta_type, field in (("text_delta", "text"), ("thinking_delta", "thinking"),
                                      ("signature_delta", "signature")):
                if kind == delta_type:
                    if block.get("type") != ("text" if field == "text" else "thinking"):
                        raise ValueError("content delta type differs from block type")
                    block[field] = _string(block.get(field, ""), field) + _string(delta.get(field), field)
            if kind == "input_json_delta":
                if block.get("type") != "tool_use":
                    raise ValueError("tool input delta differs from block type")
                block["_partial_json"] = block.get("_partial_json", "") + _string(delta.get("partial_json"), "partial_json")
        elif event == "content_block_stop":
            index = value.get("index")
            if index not in open_blocks:
                raise ValueError("content block stop without open block")
            open_blocks.remove(index)
        elif event == "message_delta":
            if not started or open_blocks:
                raise ValueError("message delta before content blocks are closed")
            message_delta = True
            result.update(value.get("delta") or {})
            usage.update(value.get("usage") or {})
        elif event == "message_stop":
            if not started or not message_delta or open_blocks:
                raise ValueError("message stop before message or blocks complete")
            stopped = True
    for block in blocks.values():
        if "_partial_json" in block:
            block["input"] = strict_json(block.pop("_partial_json"))
    result["content"] = [blocks[index] for index in sorted(blocks)]
    result["usage"] = usage
    result["_stream_terminal"] = bool(started and message_delta and stopped)
    return result


def normalize_response(api_form: str, raw: str, *, stream: bool) -> dict:
    if not stream:
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError("response must be a JSON object")
        return value
    frames, done = parse_sse(raw)
    if api_form == "openai_responses":
        return _responses_stream(frames)
    if api_form == "anthropic_messages":
        return _anthropic_stream(frames)
    return _chat_stream(frames, done, api_form == "openai_fim_completions_beta")


def _contains_dict(actual: Any, expected: dict) -> bool:
    return isinstance(actual, dict) and all(key in actual and (
        _contains_dict(actual[key], value) if isinstance(value, dict)
        else type(actual[key]) is type(value) and actual[key] == value
    ) for key, value in expected.items())


def _python_function_structure(text: str, name: str, arguments: list[str]) -> dict:
    """Inspect a prefix completion's AST without compiling or executing it.

    The opening Markdown fence comes from the frozen assistant prefix. A stop
    sequence may remove its closing fence, so either complete or open fencing
    is accepted. This proves syntax/signature/return structure, not correctness
    of the generated sorting algorithm.
    """
    code = text.strip()
    if code.startswith("```python\n"):
        code = code[len("```python\n"):]
        if code.rstrip().endswith("```"):
            code = code.rstrip()[:-3].rstrip()
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise ValueError("invalid reconstructed Python syntax at line " + str(exc.lineno) + ": " + exc.msg) from None
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(functions) != 1:
        raise ValueError("reconstructed Python must define exactly one top-level " + name + " function")
    function = functions[0]
    actual_arguments = [item.arg for item in (*function.args.posonlyargs, *function.args.args)]
    if (actual_arguments != arguments or function.args.vararg is not None or function.args.kwarg is not None
            or function.args.kwonlyargs):
        raise ValueError("reconstructed Python function arguments differ from the frozen signature")

    def own_returns(node: ast.AST) -> bool:
        if isinstance(node, ast.Return):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return False
        return any(own_returns(child) for child in ast.iter_child_nodes(node))

    if not any(own_returns(node) for node in function.body):
        raise ValueError("reconstructed Python function has no return statement in its own scope")
    return {"syntax_valid": True, "function_name": name, "arguments": actual_arguments,
            "has_return": True, "executed": False, "algorithm_correctness_verified": False}


def _usage_errors(api_form: str, usage: Any, cap: int | None) -> list[str]:
    if not isinstance(usage, dict):
        return ["missing usage object"]
    first, second = (("input_tokens", "output_tokens") if api_form in {"openai_responses", "anthropic_messages"}
                     else ("prompt_tokens", "completion_tokens"))
    errors = []
    for key in (first, second):
        if type(usage.get(key)) is not int or usage[key] <= 0:
            errors.append("usage." + key + " must be a positive integer")
    if errors:
        return errors
    if cap is not None and usage[second] > cap:
        errors.append("output usage exceeds requested output limit")
    if api_form != "anthropic_messages":
        if type(usage.get("total_tokens")) is not int or usage["total_tokens"] != usage[first] + usage[second]:
            errors.append("usage.total_tokens must equal input plus output")
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        if key in usage and (type(usage[key]) is not int or not 0 <= usage[key] <= usage[first]):
            errors.append("invalid usage." + key)
    if all(key in usage for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")):
        if usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"] != usage[first]:
            errors.append("cache hit plus miss does not equal prompt tokens")
    return errors


def _reasoning_tokens(usage: dict) -> list[int]:
    counts = []
    for key in ("completion_tokens_details", "output_tokens_details"):
        details = usage.get(key)
        if details is None:
            continue
        if not isinstance(details, dict):
            raise ValueError("usage." + key + " must be an object")
        if "reasoning_tokens" in details:
            value = details["reasoning_tokens"]
            output = usage.get("completion_tokens", usage.get("output_tokens"))
            if type(value) is not int or value < 0 or type(output) is not int or value > output:
                raise ValueError("invalid usage reasoning_tokens")
            counts.append(value)
    return counts


def _canonical_effort(effort: Any) -> Any:
    if not isinstance(effort, str):
        return effort
    return {"minimal": "low", "medium": "high", "xhigh": "high"}.get(effort, effort)


def _thinking_disabled(form: str, body: dict) -> bool:
    if form == "openai_fim_completions_beta":
        return True
    if form == "openai_responses":
        return (body.get("reasoning") or {}).get("effort") == "none"
    return ((body.get("thinking") or {}).get("type") == "disabled"
            or body.get("reasoning_effort") == "none")


def _logprobs_evidence(form: str, value: Any, *, stream: bool, alternatives: int) -> int:
    """Check actual sampled-token records, including each API's native shape."""
    def probability(item: Any) -> bool:
        return type(item) in (int, float) and math.isfinite(item) and item <= 0

    def token_record(item: Any) -> None:
        if (not isinstance(item, dict) or not isinstance(item.get("token"), str)
                or not item["token"] or not probability(item.get("logprob"))):
            raise ValueError("invalid sampled token/logprob record")
        octets = item.get("bytes")
        if octets is not None and (not isinstance(octets, list)
                or any(type(octet) is not int or not 0 <= octet <= 255 for octet in octets)):
            raise ValueError("invalid logprob token bytes")

    if form == "openai_fim_completions_beta":
        groups = value if stream else [value]
        count = 0
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("FIM logprobs must be an object")
            tokens, probabilities = group.get("tokens"), group.get("token_logprobs")
            if (not isinstance(tokens, list) or not tokens or not isinstance(probabilities, list)
                    or len(tokens) != len(probabilities) or any(not isinstance(t, str) or not t for t in tokens)
                    or any(not probability(p) for p in probabilities)):
                raise ValueError("FIM logprobs lack valid sampled-token probabilities")
            top = group.get("top_logprobs")
            if alternatives and (not isinstance(top, list) or len(top) != len(tokens)
                    or any(not isinstance(row, dict) or not row
                           or any(not isinstance(k, str) or not probability(p) for k, p in row.items()) for row in top)):
                raise ValueError("invalid FIM top_logprobs")
            count += len(tokens)
    else:
        if form == "openai_responses":
            records = value
        else:
            groups = value if stream else [value]
            records = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("content"), list):
                    raise ValueError("Chat logprobs lack content token records")
                records.extend(group["content"])
        if not isinstance(records, list) or not records:
            raise ValueError("logprobs lack sampled-token records")
        for record in records:
            token_record(record)
            if alternatives:
                top = record.get("top_logprobs")
                if not isinstance(top, list) or not top:
                    raise ValueError("missing top_logprobs token records")
                for alternative in top:
                    token_record(alternative)
        count = len(records)
    if not count:
        raise ValueError("logprobs lack sampled-token records")
    return count


def _validate_fixed_json_schema(parsed: Any, schema: dict) -> None:
    """Check the bounded schema used by this matrix; never ignore unknown rules."""
    allowed = {"type", "properties", "required", "additionalProperties", "enum"}
    if set(schema) - allowed:
        raise ValueError("unsupported matrix JSON Schema rule")
    kind = schema.get("type")
    types = {"object": dict, "string": str, "boolean": bool, "array": list}
    # JSON Schema integers include finite numeric values with no fraction,
    # regardless of whether JSON spelled them as 2, 2.0 or 2e0.
    matches = (type(parsed) is int or type(parsed) is float and math.isfinite(parsed) and parsed.is_integer()) if kind == "integer" else (
        kind in types and type(parsed) is types[kind])
    if not matches:
        raise ValueError("output violates JSON Schema type")
    if "enum" in schema and not any(type(parsed) is type(item) and parsed == item for item in schema["enum"]):
        raise ValueError("output violates JSON Schema enum")
    if kind == "object":
        properties = schema.get("properties", {})
        if any(key not in parsed for key in schema.get("required", [])):
            raise ValueError("output lacks a required JSON Schema property")
        if schema.get("additionalProperties") is False and set(parsed) - set(properties):
            raise ValueError("output violates JSON Schema additionalProperties")
        for key in parsed.keys() & properties.keys():
            _validate_fixed_json_schema(parsed[key], properties[key])


def validate_response(case: dict, status_code: int, raw: str, content_type: str = "") -> dict:
    """Return all actionable diagnostics without treating HTTP 200 as success."""
    expected, body, form = case["expected"], case["body"], case["api_form"]
    evidence: dict[str, Any] = {"passed": False, "errors": [], "status_code": status_code}
    errors = evidence["errors"]
    if expected["kind"] == "error":
        if status_code not in expected.get("status_codes", [400]):
            errors.append("unexpected status for rejection case")
        try:
            payload = strict_json(raw)
            error = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(error, dict):
                errors.append("missing structured error envelope")
            else:
                fields = expected.get("error_fields", [])
                text = str(error.get("param") or "") + " " + str(error.get("message") or "")
                attributed = [field for field in fields if re.search(r"(?<![A-Za-z0-9_])" + re.escape(field) + r"(?![A-Za-z0-9_])", text, re.I)]
                evidence.update(error=error, attributed_fields=attributed)
                if not fields or not attributed:
                    errors.append("error is not attributed to the tested parameter")
        except (ValueError, TypeError) as exc:
            errors.append("invalid rejection JSON: " + str(exc))
        evidence["passed"] = not errors
        return evidence
    if status_code != 200:
        errors.append("expected HTTP 200")
        evidence["response_excerpt"] = raw[:2000]
        return evidence
    stream = body.get("stream") is True
    if content_type and stream and "text/event-stream" not in content_type.lower():
        errors.append("stream response has wrong Content-Type")
    try:
        payload = normalize_response(form, raw, stream=stream)
        evidence["normalized_response"] = payload
        if payload.get("error"):
            errors.append("response contains error")
        model = payload.get("model")
        evidence["returned_model"] = model
        if model not in MODEL_IDS:
            errors.append("returned model is not an evidenced V4.1 Flash identity: " + str(model))
        if stream and payload.get("_stream_terminal") is not True:
            errors.append("stream lacks successful terminal event")
        if form == "anthropic_messages":
            if payload.get("type") != "message" or payload.get("role") != "assistant":
                errors.append("Anthropic assistant message envelope required")
        elif not stream or form == "openai_responses":
            wanted_object = ("response" if form == "openai_responses" else
                             "text_completion" if form == "openai_fim_completions_beta" else "chat.completion")
            if payload.get("object") != wanted_object:
                errors.append("unexpected response object: " + str(payload.get("object")))
        cap = body.get("max_tokens", body.get("max_output_tokens"))
        errors.extend(_usage_errors(form, payload.get("usage"), cap))
        text, reasoning, calls, logprobs, reasoning_seen = "", "", [], None, False
        if form == "openai_responses":
            finish = payload.get("status")
            if finish != "completed":
                errors.append("Responses status is not completed")
            for item in _objects(payload.get("output"), "Responses output"):
                if item.get("type") == "message":
                    if item.get("role") != "assistant":
                        errors.append("Responses message role must be assistant")
                    for part in _objects(item.get("content"), "Responses message content"):
                        if part.get("type") == "output_text":
                            text += _string(part.get("text"), "output_text.text")
                            if "logprobs" in part and part["logprobs"] is not None:
                                if not isinstance(part["logprobs"], list):
                                    raise ValueError("Responses logprobs must be an array")
                                logprobs = (logprobs or []) + part["logprobs"]
                elif item.get("type") == "function_call":
                    calls.append({"name": item.get("name"), "arguments": item.get("arguments"), "id": item.get("call_id")})
                elif item.get("type") == "reasoning":
                    reasoning_seen = True
                    for key in ("content", "summary"):
                        for part in _objects(item.get(key, []), "reasoning." + key):
                            if part.get("type") != ("reasoning_text" if key == "content" else "summary_text"):
                                raise ValueError("unexpected reasoning content part type")
                            reasoning += _string(part.get("text"), "reasoning text")
            returned_reasoning = payload.get("reasoning")
            if returned_reasoning is not None:
                if not isinstance(returned_reasoning, dict):
                    raise ValueError("Responses reasoning configuration must be an object or null")
                echoed_effort = returned_reasoning.get("effort")
                if echoed_effort is not None:
                    if _canonical_effort(echoed_effort) not in {"none", "low", "high", "max"}:
                        raise ValueError("invalid returned reasoning effort")
                    evidence["returned_reasoning_effort"] = echoed_effort
                    requested_effort = (body.get("reasoning") or {}).get("effort")
                    if requested_effort is not None and _canonical_effort(echoed_effort) != _canonical_effort(requested_effort):
                        errors.append("returned reasoning effort differs from request")
        elif form == "anthropic_messages":
            finish = payload.get("stop_reason")
            for part in _objects(payload.get("content"), "Anthropic content"):
                if part.get("type") == "text":
                    text += _string(part.get("text"), "text block text")
                elif part.get("type") == "thinking":
                    reasoning_seen = True
                    reasoning += _string(part.get("thinking"), "thinking block thinking")
                elif part.get("type") == "tool_use":
                    calls.append({"name": part.get("name"), "arguments": part.get("input"), "id": part.get("id")})
        else:
            choices = payload.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("exactly one response choice required")
            choice = choices[0]
            finish, logprobs = choice.get("finish_reason"), choice.get("logprobs")
            if form == "openai_fim_completions_beta":
                text = _string(choice.get("text"), "FIM choice text")
                if "message" in choice:
                    errors.append("FIM response unexpectedly uses chat message envelope")
            else:
                message = choice.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise ValueError("assistant message required")
                text = _string(message.get("content"), "message content", nullable=True)
                reasoning_seen = "reasoning_content" in message
                reasoning = _string(message.get("reasoning_content"), "reasoning_content", nullable=True)
                for call in _objects(message.get("tool_calls", []), "message tool_calls"):
                    if call.get("type") != "function" or not isinstance(call.get("function"), dict):
                        raise ValueError("function tool call envelope required")
                    calls.append({**call["function"], "id": call.get("id")})
        expected_finish = expected.get("finish_reasons")
        if expected_finish is None:
            expected_finish = (["completed"] if form == "openai_responses" else
                               ["tool_use"] if form == "anthropic_messages" and expected["kind"] == "tool" else
                               ["end_turn", "stop_sequence"] if form == "anthropic_messages" else
                               ["tool_calls"] if expected["kind"] == "tool" else ["stop"])
        if finish not in expected_finish:
            errors.append("unexpected completion reason: " + str(finish))
        reasoning_counts = _reasoning_tokens(payload.get("usage") or {})
        evidence.update(text=text, reasoning_present=bool(reasoning.strip()), finish_reason=finish, tool_calls=calls,
                        usage=payload.get("usage"), reasoning_state=("present_nonempty" if reasoning.strip() else
                            "empty" if reasoning_seen else "absent"), reasoning_tokens=reasoning_counts,
                        reasoning_mode_effect_verified=False)
        if _thinking_disabled(form, body) and (reasoning.strip() or any(reasoning_counts)):
            errors.append("reasoning returned while thinking is disabled")
        if expected["kind"] != "tool" and (not isinstance(text, str) or not text.strip()):
            errors.append("missing nonempty generated text")
        if expected.get("require_reasoning") and not reasoning.strip():
            errors.append("missing requested reasoning content")
        if expected.get("require_logprobs"):
            alternatives = body.get("logprobs", 0) if form == "openai_fim_completions_beta" else body.get("top_logprobs", 0)
            evidence["logprob_token_count"] = _logprobs_evidence(form, logprobs, stream=stream, alternatives=alternatives)
        if expected["kind"] == "tool":
            if len(calls) != 1:
                errors.append("expected exactly one tool call")
            for call in calls:
                if not isinstance(call.get("id"), str) or not call["id"]:
                    errors.append("tool call missing id")
                if call.get("name") != expected.get("tool_name"):
                    errors.append("tool name mismatch")
                args = call.get("arguments")
                args = strict_json(args) if isinstance(args, str) else args
                if not isinstance(args, dict) or args != expected.get("tool_arguments"):
                    errors.append("tool arguments mismatch")
                schemas = []
                for supplied in body.get("tools", []):
                    function = supplied.get("function", {}) if form not in {"anthropic_messages", "openai_responses"} else supplied
                    if function.get("name") == expected.get("tool_name"):
                        schemas.append(function.get("input_schema") if form == "anthropic_messages" else function.get("parameters"))
                if len(schemas) != 1 or not isinstance(schemas[0], dict):
                    raise ValueError("expected tool lacks one request argument schema")
                _validate_fixed_json_schema(args, schemas[0])
        elif calls:
            errors.append("unexpected tool call in text response")
        if expected.get("contains") and expected["contains"].casefold() not in text.casefold():
            errors.append("expected text marker absent")
        if "exact_text" in expected and text.strip() != expected["exact_text"]:
            errors.append("text differs from exact expected answer")
        if expected["kind"] == "image" and expected.get("contains"):
            if text.strip().strip('"\'`*.,!?;:。！').casefold() != expected["contains"].casefold():
                errors.append("image color answer is not the expected single color word")
        if expected.get("forbid_contains") and expected["forbid_contains"].casefold() in text.casefold():
            errors.append("forbidden stop marker present")
        if expected.get("echo_prefix") and not text.startswith(expected["echo_prefix"]):
            errors.append("FIM echo omitted prompt prefix")
        reconstructed = text
        if expected["kind"] == "prefix":
            prefix = expected.get("prefix", "")
            last = body.get("messages", [{}])[-1]
            if (not prefix or last.get("prefix") is not True or last.get("role") != "assistant"
                    or last.get("content") != prefix):
                errors.append("prefix case lacks explicit final assistant prefix")
            reconstructed = text if text.startswith(prefix) else prefix + text
            evidence["prefix_response_mode"] = "full_prefix_and_continuation" if text.startswith(prefix) else "continuation_only"
            evidence["reconstructed_text"] = reconstructed
            if expected.get("python_function_name"):
                evidence["python_structure"] = _python_function_structure(
                    reconstructed, expected["python_function_name"], expected.get("python_arguments", []))
        if expected["kind"] == "fim":
            evidence["reconstructed_text"] = (("" if expected.get("echo_prefix") else body.get("prompt", "")) + text + expected.get("suffix", body.get("suffix", "")))
            pattern = expected.get("reconstructed_pattern")
            if not pattern or not re.fullmatch(pattern, evidence["reconstructed_text"]):
                errors.append("FIM reconstruction does not match the frozen complete-text structure")
        if expected["kind"] == "json" or "json_contains" in expected:
            parsed = strict_json(reconstructed)
            evidence["parsed_json"] = parsed
            if not _contains_dict(parsed, expected.get("json_contains", {})):
                errors.append("JSON content mismatch")
            if expected.get("exact_json") and parsed != expected.get("json_contains"):
                errors.append("JSON differs from exact expected object")
            json_format = (body.get("text") or {}).get("format") or {}
            if json_format.get("type") == "json_schema":
                _validate_fixed_json_schema(parsed, json_format["schema"])
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        errors.append("invalid response structure or semantics: " + str(exc))
    evidence["passed"] = not errors
    return evidence
