"""Offline evidence: transport fixtures, semantic rejection and package guards."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from lib.deepseek_v4_1_matrix import build_cases
from lib.deepseek_v4_1_validation import normalize_response, validate_response
from scripts import run_deepseek_v4_1_matrix as runner


def case(name="chat_text"):
    return next(item for item in build_cases() if item["id"] == name)


def chat(content="READY", **updates):
    result = {"id": "chat-test", "object": "chat.completion", "model": "deepseek-flash",
              "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
    result.update(updates)
    return result


def sse(*payloads, done=False):
    return "".join("data: " + json.dumps(item) + "\n\n" for item in payloads) + ("data: [DONE]\n\n" if done else "")


def chat_stream(text="READY", *, done=True, usage=True):
    first = {"object": "chat.completion.chunk", "model": "deepseek-flash", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
    last = {"object": "chat.completion.chunk", "model": "deepseek-flash", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    if usage:
        last["usage"] = chat()["usage"]
    return sse(first, last, done=done)


def check(selected, payload, status=200):
    return validate_response(selected, status, json.dumps(payload))


def test_plain_response_requires_identity_usage_and_semantic_content():
    assert check(case(), chat())["passed"]
    for payload in (chat(model="deepseek-v4-flash-0731"), chat(content="Something unrelated"),
                    chat(usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 13}),
                    chat(usage={"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3})):
        assert not check(case(), payload)["passed"]


def test_output_cap_and_truncation_are_not_success():
    payload = chat(usage={"prompt_tokens": 10, "completion_tokens": 513, "total_tokens": 523})
    assert not check(case(), payload)["passed"]
    payload = chat()
    payload["choices"][0]["finish_reason"] = "length"
    assert not check(case(), payload)["passed"]


def test_image_color_requires_positive_single_word_identification():
    selected = case("chat_inline_image")
    assert check(selected, chat("Red."))["passed"]
    assert not check(selected, chat("not red"))["passed"]


def test_chat_stream_requires_terminal_usage_and_correct_envelope():
    selected = case("chat_stream")
    assert validate_response(selected, 200, chat_stream(), "text/event-stream")["passed"]
    for raw in (chat_stream(done=False), chat_stream(usage=False), chat_stream().replace("chat.completion.chunk", "fake"),
                chat_stream().replace('"assistant"', '"user"')):
        assert not validate_response(selected, 200, raw, "text/event-stream")["passed"]
    assert not validate_response(selected, 200, chat_stream(), "application/json")["passed"]


def test_sse_malformed_frame_and_data_after_done_are_rejected():
    for raw in (chat_stream() + 'data: {"x": 1}\n\n', chat_stream().replace('"READY"', 'NaN'),
                chat_stream().replace('"READY"', '"READY", "content": "READY"')):
        assert not validate_response(case("chat_stream"), 200, raw)["passed"]


def test_chat_tool_stream_reassembles_argument_fragments():
    selected = case("chat_tool_strict_stream")
    base = {"object": "chat.completion.chunk", "model": "deepseek-flash"}
    raw = sse({**base, "choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call1", "type": "function", "function": {"name": "add_numbers", "arguments": '{"a":2,'}}]}}]},
              {**base, "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"b":3}'}}]}, "finish_reason": "tool_calls"}], "usage": chat()["usage"]}, done=True)
    assert validate_response(selected, 200, raw)["passed"]
    assert not validate_response(selected, 200, raw.replace("add_numbers", "other"))["passed"]


def test_tool_json_arguments_must_match_types_and_values():
    selected = case("chat_tool_required")
    payload = chat(None)
    payload["choices"][0].update(finish_reason="tool_calls", message={"role": "assistant", "content": None,
        "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "add_numbers", "arguments": '{"a":2,"b":3}'}}]})
    assert check(selected, payload)["passed"]
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"a":2.0,"b":3e0}'
    assert check(selected, payload)["passed"]  # JSON Schema integer permits zero-fraction numbers.
    for args in ('{"a":2,"b":4}', '{"a":2.5,"b":3}', '{"a":true,"b":3}', '{"a":"2","b":3}',
                 '{"a":2,"b":3,"extra":0}', '{"a":2}', '{"a":2,"a":2,"b":3}'):
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = args
        assert not check(selected, payload)["passed"]


def test_prefix_validates_reconstructed_json_both_response_shapes():
    selected = case("prefix_text")
    for content in ('BLUE"}', selected["expected"]["prefix"] + 'BLUE"}'):
        assert check(selected, chat(content))["passed"]
    assert not check(selected, chat('RED"}'))["passed"]
    assert not check(selected, chat('Some BLUE content'))["passed"]


def test_partial_word_prefix_uses_standard_json_unicode_without_repairing_bad_output():
    selected = case("prefix_json_quote")
    assert selected["body"]["messages"][-1]["content"] == '{"answer":"B'
    result = check(selected, chat(r'\u004c\u0055\u0045"}'))
    assert result["passed"]
    assert result["parsed_json"] == {"answer": "BLUE"}
    for invalid in (r'\u004d\u0055\u0045"}', r'\\u004c\\u0055\\u0045"}', 'LLUE"}', ''):
        assert not check(selected, chat(invalid))["passed"]


def python_prefix_case():
    selected = case("prefix_text")
    selected["body"]["messages"][-1]["content"] = "```python\n"
    selected["expected"] = {"kind": "prefix", "prefix": "```python\n",
                            "python_function_name": "quick_sort", "python_arguments": ["arr"]}
    return selected


def test_prefix_python_structure_accepts_tail_and_full_prefix_without_execution():
    selected = python_prefix_case()
    code = "def quick_sort(arr):\n    if len(arr) < 2:\n        return arr\n    return sorted(arr)\n"
    for text in (code, "```python\n" + code, "```python\n" + code + "```"):
        result = check(selected, chat(text))
        assert result["passed"], result["errors"]
        assert result["python_structure"] == {
            "syntax_valid": True, "function_name": "quick_sort", "arguments": ["arr"],
            "has_return": True, "executed": False, "algorithm_correctness_verified": False}


@pytest.mark.parametrize("code", [
    "def quick_sort(arr)\n    return arr",  # invalid Python syntax
    "def other(arr):\n    return arr",  # wrong function
    "def quick_sort(items):\n    return items",  # wrong argument name
    "def quick_sort(arr, extra):\n    return arr",  # extra argument
    "def quick_sort(arr, *extra):\n    return arr",  # variadic argument
    "def quick_sort(arr):\n    pass",  # no return
    "def outer():\n    def quick_sort(arr):\n        return arr",  # not top-level
    "def quick_sort(arr):\n    def inner():\n        return arr",  # only nested return
    "def quick_sort(arr):\n    return arr\ndef quick_sort(arr):\n    return arr",  # ambiguous duplicate
])
def test_prefix_python_structure_rejects_wrong_syntax_signature_or_return(code):
    result = check(python_prefix_case(), chat(code))
    assert not result["passed"]
    assert any("reconstructed Python" in error for error in result["errors"])


def test_prefix_python_never_executes_generated_side_effects(tmp_path):
    target = tmp_path / "must-not-exist"
    code = f"open({str(target)!r}, 'w').write('executed')\ndef quick_sort(arr):\n    return arr\n"
    result = check(python_prefix_case(), chat(code))
    assert result["passed"]
    assert result["python_structure"]["executed"] is False
    assert not target.exists()


def test_prefix_python_does_not_relax_nonempty_output_or_usage():
    assert not check(python_prefix_case(), chat(""))["passed"]
    payload = chat("def quick_sort(arr):\n    return arr", usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10})
    assert not check(python_prefix_case(), payload)["passed"]


def test_fim_requires_completion_envelope_and_full_reconstruction():
    selected = case("fim_text")
    payload = chat()
    payload["object"] = "text_completion"
    payload["choices"] = [{"index": 0, "text": "Paris", "finish_reason": "stop"}]
    assert check(selected, payload)["passed"]
    payload["choices"][0]["text"] = "not Paris"
    assert not check(selected, payload)["passed"]
    assert not check(selected, chat("Paris"))["passed"]


def responses(*, status="completed", content="READY", reasoning=False):
    output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": content}]}]
    if reasoning:
        output.insert(0, {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "I should answer briefly."}], "summary": []})
    return {"id": "resp1", "object": "response", "model": "deepseek-flash", "status": status, "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}


def responses_events(*, tool=False):
    """Native Responses lifecycle with independently represented delta/done data."""
    terminal = responses()
    if tool:
        item = {"id": "item1", "type": "function_call", "status": "completed",
                "name": "add_numbers", "call_id": "call1", "arguments": '{"a":2,"b":3}'}
    else:
        item = {**terminal["output"][0], "id": "item1", "status": "completed"}
    terminal["output"] = [item]
    start = {**item, "status": "in_progress"}
    start["arguments" if tool else "content"] = "" if tool else []
    events = [{"type": "response.created", "response": {**terminal, "status": "in_progress", "output": []}},
              {"type": "response.output_item.added", "output_index": 0, "item": start}]
    locator = {"output_index": 0, "item_id": "item1"}
    if tool:
        for delta in ('{"a":2,', '"b":3}'):
            events.append({"type": "response.function_call_arguments.delta", **locator, "delta": delta})
        events.append({"type": "response.function_call_arguments.done", **locator, "arguments": item["arguments"]})
    else:
        locator["content_index"] = 0
        part = item["content"][0]
        events.append({"type": "response.content_part.added", **locator, "part": {"type": "output_text", "text": ""}})
        for delta in ("READ", "Y"):
            events.append({"type": "response.output_text.delta", **locator, "delta": delta})
        events.extend([{"type": "response.output_text.done", **locator, "text": "READY"},
                       {"type": "response.content_part.done", **locator, "part": part}])
    events.extend([{"type": "response.output_item.done", "output_index": 0, "item": item},
                   {"type": "response.completed", "response": terminal}])
    # Copies prevent mutating one snapshot from silently altering the others.
    return [dict(copy.deepcopy(event), sequence_number=number) for number, event in enumerate(events)]


def test_responses_uses_native_reasoning_content_and_logprobs():
    assert check(case("responses_thinking_top_p"), responses(reasoning=True))["passed"]
    assert check(case("responses_thinking_top_p"), responses())["passed"]
    payload = responses()
    payload["output"][0]["content"][0]["logprobs"] = [token_probability()]
    assert check(case("responses_logprobs"), payload)["passed"]


def token_probability():
    return {"token": "READY", "logprob": -0.1,
            "top_logprobs": [{"token": "READY", "logprob": -0.1}]}


def anthropic(content=None):
    return {"id": "msg1", "type": "message", "role": "assistant", "model": "deepseek-flash",
            "content": content if content is not None else [{"type": "text", "text": "READY"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 2}}


@pytest.mark.parametrize("value,state", [(None, "empty"), ("", "empty"), ("Reasoning", "present_nonempty")])
def test_chat_nullable_reasoning_is_schema_evidence_without_mode_effect_claim(value, state):
    payload = chat()
    payload["choices"][0]["message"]["reasoning_content"] = value
    result = check(case("chat_thinking_top_p"), payload)
    assert result["passed"], result["errors"]
    assert result["reasoning_state"] == state
    assert result["reasoning_mode_effect_verified"] is False
    assert check(case("chat_thinking_top_p"), chat())["reasoning_state"] == "absent"


def test_explicit_legacy_reasoning_observability_expectation_remains_strict():
    selected = case("chat_thinking_top_p")
    selected["expected"]["require_reasoning"] = True
    assert not check(selected, chat())["passed"]


def test_anthropic_empty_thinking_block_is_distinct_from_absent_or_nonempty():
    selected = case("anthropic_thinking_top_p")
    payload = anthropic([{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "READY"}])
    result = check(selected, payload)
    assert result["passed"] and result["reasoning_state"] == "empty"
    assert check(selected, anthropic())["reasoning_state"] == "absent"
    payload["content"][0]["thinking"] = "Reasoning"
    assert check(selected, payload)["reasoning_state"] == "present_nonempty"
    payload["content"][0]["thinking"] = None  # Anthropic thinking is string, unlike nullable Chat reasoning.
    assert not check(selected, payload)["passed"]


@pytest.mark.parametrize("value", [0, False, [], {}])
def test_reasoning_wrong_types_are_not_silently_coerced(value):
    payload = chat()
    payload["choices"][0]["message"]["reasoning_content"] = value
    assert not check(case("chat_thinking_top_p"), payload)["passed"]
    payload = responses(reasoning=True)
    payload["output"][0]["content"][0]["text"] = value
    assert not check(case("responses_thinking_top_p"), payload)["passed"]


def test_responses_reasoning_arrays_are_optional_but_not_nullable():
    payload = responses(reasoning=True)
    payload["output"][0].pop("content")
    assert check(case("responses_thinking_top_p"), payload)["passed"]
    payload["output"][0]["content"] = None
    assert not check(case("responses_thinking_top_p"), payload)["passed"]


@pytest.mark.parametrize("name,payload", [
    ("chat_text", chat()), ("responses_text", responses()), ("anthropic_text", anthropic()),
])
def test_disabled_thinking_rejects_nonempty_reasoning_and_positive_usage(name, payload):
    payload = copy.deepcopy(payload)
    if name == "chat_text":
        payload["choices"][0]["message"]["reasoning_content"] = "Reasoning"
    elif name == "responses_text":
        payload["output"].insert(0, {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "Reasoning"}]})
    else:
        payload["content"].insert(0, {"type": "thinking", "thinking": "Reasoning"})
    assert not check(case(name), payload)["passed"]
    payload = {"chat_text": chat, "responses_text": responses, "anthropic_text": anthropic}[name]()
    details = "completion_tokens_details" if name == "chat_text" else "output_tokens_details"
    payload["usage"][details] = {"reasoning_tokens": 1}
    assert not check(case(name), payload)["passed"]
    payload["usage"][details]["reasoning_tokens"] = 0
    assert check(case(name), payload)["passed"]


@pytest.mark.parametrize("value", [-1, True, 1.5, 3, "0"])
def test_reasoning_usage_requires_nonnegative_integral_count_within_output(value):
    payload = responses(reasoning=True)
    payload["usage"]["output_tokens_details"] = {"reasoning_tokens": value}
    assert not check(case("responses_thinking_top_p"), payload)["passed"]


@pytest.mark.parametrize("requested,returned", [("minimal", "low"), ("medium", "high"), ("xhigh", "high"), ("high", "xhigh")])
def test_responses_effort_echo_uses_documented_aliases(requested, returned):
    selected = case("responses_thinking_top_p")
    selected["body"]["reasoning"]["effort"] = requested
    payload = responses()
    payload["reasoning"] = {"effort": returned}
    assert check(selected, payload)["passed"]


def test_responses_effort_echo_optional_but_cannot_contradict_request():
    selected = case("responses_effort_high")
    payload = responses()
    assert check(selected, payload)["passed"]
    for config in (None, {}, {"effort": None}):
        payload["reasoning"] = config
        assert check(selected, payload)["passed"]
    for config in ({"effort": "max"}, {"effort": "ultra"}, {"effort": 0}, []):
        payload["reasoning"] = config
        assert not check(selected, payload)["passed"]


@pytest.mark.parametrize("text", ["NOT READY", "ALREADY", "READY|AFTER", "ready"])
def test_fixed_text_is_an_exact_answer_not_a_substring(text):
    assert not check(case("chat_text"), chat(text))["passed"]
    assert check(case("chat_text"), chat(" READY\n"))["passed"]


def test_numeric_text_answer_rejects_superset():
    assert not check(case("responses_effort_max"), responses(content="1678"))["passed"]
    assert check(case("responses_effort_max"), responses(content="678"))["passed"]


def test_json_exact_keys_and_requested_schema_are_independent_checks():
    selected = case("responses_json_schema")
    payload = responses(content='{"answer":"BLUE","extra":true}')
    assert not check(selected, payload)["passed"]
    selected["expected"].pop("exact_json")
    result = check(selected, payload)
    assert not result["passed"]
    assert any("additionalProperties" in error for error in result["errors"])
    selected = case("chat_json")
    assert not check(selected, chat('{"answer":"BLUE","extra":true}'))["passed"]


@pytest.mark.parametrize("mutation", ["role", "content", "flag"])
def test_prefix_expected_bytes_and_final_assistant_must_match_request(mutation):
    selected = case("prefix_text")
    selected["body"]["messages"][-1][{"role": "role", "content": "content", "flag": "prefix"}[mutation]] = {
        "role": "user", "content": '{"wrong": "', "flag": False}[mutation]
    assert not check(selected, chat('BLUE"}'))["passed"]


def test_partial_word_prefix_never_rewrites_the_actual_completion():
    selected = case("prefix_json_quote")
    assert check(selected, chat('LUE"}'))["passed"]
    result = check(selected, chat('LLUE"}'))
    assert not result["passed"]
    assert result["reconstructed_text"] == '{"answer":"BLLUE"}'


@pytest.mark.parametrize("value", [{"content": None}, {"content": []}, {"content": [{}]},
                                   {"content": [{"token": "READY", "logprob": True}]},
                                   {"content": [{"token": "READY", "logprob": 0.1}]}])
def test_chat_logprobs_require_real_numeric_token_records(value):
    payload = chat()
    payload["choices"][0]["logprobs"] = value
    assert not check(case("chat_logprobs"), payload)["passed"]


def test_native_logprobs_shapes_are_checked_for_all_supported_forms():
    payload = chat()
    payload["choices"][0]["logprobs"] = {"content": [token_probability()]}
    assert check(case("chat_logprobs"), payload)["passed"]
    payload = responses()
    payload["output"][0]["content"][0]["logprobs"] = [{}]
    assert not check(case("responses_logprobs"), payload)["passed"]
    payload = chat()
    payload["object"] = "text_completion"
    payload["choices"] = [{"index": 0, "text": "Paris", "finish_reason": "stop", "logprobs": {
        "tokens": ["Paris"], "token_logprobs": [-0.1], "top_logprobs": [{"Paris": -0.1}]}}]
    assert check(case("fim_logprobs_top_p"), payload)["passed"]
    payload["choices"][0]["logprobs"]["token_logprobs"] = [None]
    assert not check(case("fim_logprobs_top_p"), payload)["passed"]


def test_nonstream_envelope_and_roles_must_match_api_form():
    payload = chat(object="text_completion")
    assert not check(case("chat_text"), payload)["passed"]
    payload = responses()
    payload["output"][0]["role"] = "user"
    assert not check(case("responses_text"), payload)["passed"]
    payload = anthropic()
    payload["type"] = "completion"
    assert not check(case("anthropic_text"), payload)["passed"]


def test_responses_stream_rejects_incomplete_and_missing_terminal():
    selected = case("responses_stream")
    assert validate_response(selected, 200, sse(*responses_events()))["passed"]
    for payload in ({"type": "response.incomplete", "response": responses(status="incomplete")},
                    {"type": "response.created", "response": responses(status="in_progress")},
                    {"type": "response.completed", "response": responses()}):
        assert not validate_response(selected, 200, sse(payload))["passed"]


def test_responses_terminal_rejects_later_semantic_data():
    raw = sse(*responses_events(),
              {"type": "response.output_text.delta", "delta": "AFTER"})
    assert not validate_response(case("responses_stream"), 200, raw)["passed"]


@pytest.mark.parametrize("tool", [False, True])
def test_responses_stream_reconstructs_text_and_tool_arguments(tool):
    selected = case("responses_tool_stream" if tool else "responses_stream")
    assert validate_response(selected, 200, sse(*responses_events(tool=tool)))["passed"]


@pytest.mark.parametrize("tool", [False, True])
@pytest.mark.parametrize("stage", ["delta", "done", "output_item.done", "completed"])
def test_responses_stream_rejects_contradictory_incremental_and_completed_content(tool, stage):
    events = responses_events(tool=tool)
    kind = "function_call_arguments" if tool else "output_text"
    if stage in {"delta", "done"}:
        event = next(event for event in events if event["type"] == "response." + kind + "." + stage)
        event["delta" if stage == "delta" else "arguments" if tool else "text"] = "WRONG"
    else:
        event = next(event for event in events if event["type"] == "response." + stage)
        item = event["item"] if stage == "output_item.done" else event["response"]["output"][0]
        if tool:
            item["arguments"] = '{"a":2,"b":4}'
        else:
            item["content"][0]["text"] = "WRONG"
    selected = case("responses_tool_stream" if tool else "responses_stream")
    result = validate_response(selected, 200, sse(*events))
    assert not result["passed"]
    assert "stream" in " ".join(result["errors"]).lower()


@pytest.mark.parametrize("tool", [False, True])
@pytest.mark.parametrize("field,value", [
    ("output_index", 1), ("output_index", -1), ("output_index", True),
    ("item_id", "wrong-item"), ("delta", None), ("sequence_number", 0),
])
def test_responses_stream_rejects_bad_delta_identity_index_type_or_order(tool, field, value):
    events = responses_events(tool=tool)
    delta = next(event for event in events if event["type"].endswith(".delta"))
    delta[field] = value
    selected = case("responses_tool_stream" if tool else "responses_stream")
    assert not validate_response(selected, 200, sse(*events))["passed"]


@pytest.mark.parametrize("content_index", [-1, 1, True, "0"])
def test_responses_stream_rejects_bad_content_index(content_index):
    events = responses_events()
    next(event for event in events if event["type"].endswith(".delta"))["content_index"] = content_index
    assert not validate_response(case("responses_stream"), 200, sse(*events))["passed"]


@pytest.mark.parametrize("tool", [False, True])
@pytest.mark.parametrize("mutation", ["missing_delta", "missing_done", "missing_item_done", "duplicate_done", "late_delta"])
def test_responses_stream_rejects_missing_duplicate_or_late_content_events(tool, mutation):
    events = responses_events(tool=tool)
    kind = "function_call_arguments" if tool else "output_text"
    delta_index = next(i for i, event in enumerate(events) if event["type"] == "response." + kind + ".delta")
    done_index = next(i for i, event in enumerate(events) if event["type"] == "response." + kind + ".done")
    if mutation == "missing_delta":
        events.pop(delta_index)
    elif mutation == "missing_done":
        events.pop(done_index)
    elif mutation == "missing_item_done":
        events.pop(-2)
    elif mutation == "duplicate_done":
        events.insert(done_index + 1, copy.deepcopy(events[done_index]))
    else:
        events.insert(done_index + 1, copy.deepcopy(events[delta_index]))
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
    selected = case("responses_tool_stream" if tool else "responses_stream")
    assert not validate_response(selected, 200, sse(*events))["passed"]


def test_responses_stream_checks_response_identity_and_event_name():
    events = responses_events()
    events[0]["response"]["model"] = "another-model"
    assert not validate_response(case("responses_stream"), 200, sse(*events))["passed"]
    raw = "event: response.wrong\n" + sse(*responses_events())
    assert not validate_response(case("responses_stream"), 200, raw)["passed"]


def test_responses_stream_does_not_require_unrelated_optional_metadata():
    events = responses_events()
    for event in events:
        event.pop("sequence_number")
    next(event for event in events if event["type"] == "response.content_part.added")["part"].pop("text")
    events[-1]["response"]["output"][0]["content"][0]["annotations"] = []
    assert validate_response(case("responses_stream"), 200, sse(*events))["passed"]


def test_chat_terminal_rejects_later_reasoning_or_tool_data():
    base = {"object": "chat.completion.chunk", "model": "deepseek-flash"}
    for delta in ({"reasoning_content": "late"}, {"tool_calls": [{"function": {"arguments": "late"}}]}):
        raw = chat_stream(done=False) + sse({**base, "choices": [{"index": 0, "delta": delta}]}, done=True)
        assert not validate_response(case("chat_stream"), 200, raw)["passed"]


def test_chat_stream_preserves_missing_versus_nullable_reasoning_without_coercing_types():
    assert normalize_response("openai_chat_completions", chat_stream(), stream=True)["choices"][0]["message"].get("reasoning_content", "ABSENT") == "ABSENT"
    raw = chat_stream().replace('"content": "READY"', '"content": "READY", "reasoning_content": null')
    assert validate_response(case("chat_stream"), 200, raw)["reasoning_state"] == "empty"
    raw = raw.replace('"reasoning_content": null', '"reasoning_content": []')
    assert not validate_response(case("chat_stream"), 200, raw)["passed"]


def test_anthropic_stream_requires_stop_and_merges_usage():
    start = {"type": "message_start", "message": {"id": "msg1", "type": "message", "role": "assistant", "model": "deepseek-flash", "usage": {"input_tokens": 10, "output_tokens": 0}}}
    records = [start, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "READY"}},
               {"type": "content_block_stop", "index": 0},
               {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
               {"type": "message_stop"}]
    assert validate_response(case("anthropic_stream"), 200, sse(*records))["passed"]
    assert not validate_response(case("anthropic_stream"), 200, sse(*records[:-1]))["passed"]
    assert not validate_response(case("anthropic_stream"), 200, sse(*(record for record in records if record["type"] != "content_block_stop")))["passed"]
    late = records[:4] + [records[2]] + records[4:]
    assert not validate_response(case("anthropic_stream"), 200, sse(*late))["passed"]
    assert not validate_response(case("anthropic_stream"), 200, sse(*records[1:]))["passed"]


def test_error_status_alone_does_not_prove_parameter_rejection():
    selected = case("chat_reject_nonstream_options")
    assert check(selected, {"error": {"type": "invalid_request_error", "message": "stream_options requires stream=true"}}, 400)["passed"]
    assert not check(selected, {"error": {"message": "insufficient balance"}}, 400)["passed"]
    assert not check(selected, {"error": {"message": "stream_options invalid"}}, 500)["passed"]
    assert not check(selected, chat(), 200)["passed"]


def test_plan_is_network_free_and_detects_source_drift(tmp_path):
    with patch.object(runner, "load_config", side_effect=AssertionError("plan loaded credentials")):
        package = runner.prepare(tmp_path, "smoke")
    assert len(package["cases"]) == 10
    assert (tmp_path / "manifest.json").is_file()
    assert runner.prepare(tmp_path, "smoke") == package
    with patch.object(runner, "source_hashes", return_value={"modified": "a"}):
        with pytest.raises(ValueError, match="drift"):
            runner.prepare(tmp_path, "smoke")


def test_case_token_floor_and_exact_beta_exception():
    selected = case()
    for value in (255, True, None):
        selected["body"]["max_tokens"] = value
        with pytest.raises(ValueError, match="256"):
            runner.validate_case(selected)
    runner.validate_case(case("chat_tool_strict_stream"))
    selected = case()
    selected["path"] = "/beta/chat/completions"
    with pytest.raises(ValueError, match="route"):
        runner.validate_case(selected)


@pytest.mark.parametrize("url", ["http://api.deepseek.com", "https://api.deepseek.com.evil.test", "https://other.test", "https://user@api.deepseek.com", "https://api.deepseek.com/unapproved", "https://api.deepseek.com?key=secret"])
def test_provider_must_be_exact_official_https_origin(url):
    with pytest.raises(ValueError, match="official"):
        runner.official_provider({"providers": {runner.PROVIDER: {"base_url": url}}})


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.status_code = 200
        self.headers = headers or {"content-type": "application/json", "set-cookie": "private"}
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size):
        yield self.raw


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET":
            return FakeResponse({"data": [{"id": "deepseek-flash"}]})
        return FakeResponse(chat())


def test_execution_discovers_model_saves_evidence_and_never_repeats(tmp_path):
    package = runner.prepare(tmp_path, "smoke", ["chat_text"])
    config = {"providers": {runner.PROVIDER: {"base_url": runner.ORIGIN}}}
    session = FakeSession()
    with patch.object(runner, "get_api_key", return_value="test-secret-never-persist"):
        result = runner.execute(tmp_path, package, config=config, session=session)
        assert result["all_pass"] and result["executed"] and result["terminal"]
        assert [item[0] for item in session.calls] == ["GET", "POST"]
        with pytest.raises(FileExistsError):
            runner.execute(tmp_path, package, config=config, session=session)
    assert len(session.calls) == 2
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert "test-secret-never-persist" not in text
            assert "set-cookie" not in text
    response = tmp_path / "responses/chat_text.json"
    assert result["results"][0]["response_sha256"] == runner.sha256(response.read_bytes())


def test_execute_source_drift_prevents_credentials_and_network(tmp_path):
    package = runner.prepare(tmp_path, "smoke", ["chat_text"])
    with patch.object(runner, "source_hashes", return_value={}), patch.object(runner, "load_config", side_effect=AssertionError):
        with pytest.raises(ValueError, match="drift"):
            runner.execute(tmp_path, package)
    assert not (tmp_path / "execution_started.json").exists()


def test_snapshot_tampering_prevents_execution_before_credentials(tmp_path):
    package = runner.prepare(tmp_path, "smoke", ["chat_text"])
    (tmp_path / "source_snapshot" / runner.SOURCE_FILES[0]).write_text("modified")
    with patch.object(runner, "load_config", side_effect=AssertionError):
        with pytest.raises(ValueError, match="snapshot"):
            runner.execute(tmp_path, package)
    assert not (tmp_path / "execution_started.json").exists()
