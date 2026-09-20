"""Native Messages stream failures cannot become successful parameter probes."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from lib.anthropic_message_stream import AnthropicMessageStream
from lib.client import OpenAICompatibleClient
from lib.profile_validation import validate_profile_response


MODEL = "claude-sonnet-4-5-20250929"


def events():
    return [
        {"type": "message_start", "message": {"id": "msg_offline", "type": "message", "role": "assistant",
            "model": MODEL, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 17, "output_tokens": 1, "cache_read_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"color":"blue","count":2}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ]


def wire(rows):
    return "".join("event: " + row["type"] + "\ndata: " + json.dumps(row) + "\n\n" for row in rows)


def parse(raw):
    accumulator = AnthropicMessageStream()
    for line in raw.splitlines():
        accumulator.feed_line(line)
    accumulator.finish()
    return accumulator


class Response:
    status_code = 200
    headers = {}

    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self, **_kwargs):
        return iter(self.raw.encode().splitlines())


def client_result(raw, *, url="https://api.anthropic.com/v1/messages"):
    body = {"model": MODEL, "max_tokens": 1024, "stream": True,
            "messages": [{"role": "user", "content": "Return the fixed JSON."}]}
    sent = []

    def post(endpoint, **kwargs):
        sent.append((endpoint, copy.deepcopy(kwargs)))
        return Response(raw)

    client = object.__new__(OpenAICompatibleClient)
    client.timeout_sec = 1
    client.session = SimpleNamespace(post=post)
    client._transport_url = lambda _transport: url
    client._auth_headers = lambda *_args: {}
    result = client._claude_messages_stream(body)
    assert len(sent) == 1 and sent[0][0] == url
    assert sent[0][1]["json"] == body
    assert sent[0][1]["allow_redirects"] is False and sent[0][1]["stream"] is True
    return result, body


def test_complete_wire_keeps_native_envelope_usage_and_framing():
    result, body = client_result(wire(events()))
    assert result.success and result.error_type is None
    assert result.response_json["id"] == "msg_offline"
    assert result.response_json["type"] == "message"
    assert result.response_json["role"] == "assistant"
    assert result.usage == {"input_tokens": 17, "output_tokens": 12, "cache_read_input_tokens": 0}
    assert "cache_creation_input_tokens" not in result.usage
    assert result.text == '{"color":"blue","count":2}'
    assert result.ttft_ms is not None
    assert "event: message_start\ndata:" in result.raw_text
    assert parse(result.raw_text).error_type is None
    assert validate_profile_response("claude_native_stream", result.response_json, result,
        request_body=body, transport="claude_messages", reference_source="claude_native_messages") is None


@pytest.mark.parametrize("rows,diagnostic", [
    ([], "stream_missing_message_stop"),
    (events()[:-1], "stream_missing_message_stop"),
    (events()[1:], "stream_event_before_message_start"),
    (events()[:3] + events()[4:], "stream_message_delta_before_content_stop"),
    (events()[:1] + events()[:1] + events()[1:], "stream_duplicate_message_start"),
    (events()[:2] + [events()[1]] + events()[2:], "stream_content_start_out_of_order"),
    (events()[:4] + [events()[3]] + events()[4:], "stream_content_stop_out_of_order"),
    (events()[:1] + [events()[2]] + events()[1:], "stream_delta_outside_open_block"),
    (events()[:4] + [events()[-1]], "stream_message_stop_before_delta"),
    (events() + [events()[2]], "stream_event_after_message_stop"),
    (events() + [{"type": "error", "error": {"type": "overloaded_error"}}], "stream_error_event"),
    (events()[:3] + [{"type": "error", "error": {"type": "overloaded_error"}}], "stream_error_event"),
])
def test_incomplete_or_error_wire_is_failed_in_client_and_profile_validator(rows, diagnostic):
    result, body = client_result(wire(rows))
    assert result.success is False and result.error_type == diagnostic
    assert validate_profile_response("claude_native_stream", result.response_json, result,
        request_body=body, transport="claude_messages", reference_source="claude_native_messages") == diagnostic


@pytest.mark.parametrize("invalid", [None, {}, {"output_tokens": None}, {"output_tokens": True},
    {"output_tokens": -1}, {"output_tokens": 0}, {"output_tokens": "12"}, {"output_tokens": 1.5}])
def test_missing_invalid_or_decreasing_terminal_usage_does_not_become_zero(invalid):
    rows = events()
    rows[-2]["usage"] = invalid
    result, _ = client_result(wire(rows))
    assert not result.success
    assert result.error_type in {"stream_usage_missing", "stream_usage_invalid", "stream_usage_decreased"}
    assert "cache_creation_input_tokens" not in result.usage


def test_cumulative_usage_updates_preserve_null_or_missing_native_counters():
    rows = events()
    rows.insert(-1, {"type": "message_delta", "delta": {}, "usage": {
        "output_tokens": 15, "input_tokens": None, "cache_read_input_tokens": None}})
    parsed = parse(wire(rows))
    assert parsed.error_type is None
    assert parsed.message["usage"] == {"input_tokens": 17, "output_tokens": 15, "cache_read_input_tokens": 0}


@pytest.mark.parametrize("field", ["id", "role", "type", "model"])
def test_start_requires_native_identity_fields(field):
    rows = events()
    rows[0]["message"].pop(field)
    assert parse(wire(rows)).error_type in {"stream_invalid_message_start", "stream_identity_missing"}


@pytest.mark.parametrize("bad", ["data: [1]\n\n", "data: {bad}\n\n",
    'data: {"type":"ping","type":"ping"}\n\n',
    'event: wrong\ndata: {"type":"ping"}\n\n',
    'data: {"type":"ping","x":NaN}\n\n'])
def test_malformed_native_frames_fail(bad):
    assert parse(bad + wire(events())).error_type is not None


def test_incomplete_last_frame_is_not_dispatched_at_eof():
    result, _ = client_result(wire(events()).rstrip("\n"))
    assert not result.success and result.error_type == "stream_unterminated_sse_frame"


def test_multiline_json_comments_ping_and_future_event_do_not_replace_terminals():
    raw = wire(events())
    raw = ': comment\n\nevent: ping\ndata: {"type":"ping"}\n\n' + raw
    raw = raw.replace('data: {"type": "content_block_stop", "index": 0}',
                      'data: {"type": "content_block_stop",\ndata: "index": 0}')
    raw = raw.replace('event: message_stop', 'data: {"type":"future_extension"}\n\nevent: message_stop')
    assert parse(raw).error_type is None
    assert parse('data: {"type":"future_extension"}\n\n').error_type == "stream_missing_message_stop"


def test_thinking_signature_and_tool_json_are_reconstructed_by_content_index():
    rows = events()[:1] + [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Reason."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "opaque-test"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tool_test", "name": "lookup", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ]
    result, _ = client_result(wire(rows))
    assert result.success and result.reasoning_content == "Reason."
    assert result.response_json["content"][0]["signature"] == "opaque-test"
    assert result.tool_calls[0]["input"] == {"city": "Paris"}
    assert "partial_json" not in result.tool_calls[0]


@pytest.mark.parametrize("url,returned", [
    ("https://api.anthropic.com/v1/messages", MODEL),
    ("https://api.deepseek.com/anthropic/v1/messages", "deepseek-v4-pro"),
    ("https://bedrock-mantle.us-east-1.api.aws/anthropic/v1/messages", "claude-sonnet-5"),
])
def test_native_protocol_does_not_relabel_compatible_source_identity(url, returned):
    rows = events()
    rows[0]["message"]["model"] = returned
    rows[0]["message"]["usage"]["source_extension"] = {"sample": 3}
    result, _ = client_result(wire(rows), url=url)
    assert result.success and result.response_json["model"] == returned
    assert result.usage["source_extension"] == {"sample": 3}


def test_complete_refusal_preserves_terminal_details_without_claiming_semantics():
    rows = events()
    rows[-2]["delta"] = {"stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "safety"}}
    parsed = parse(wire(rows))
    assert parsed.error_type is None
    assert parsed.message["stop_details"] == {"type": "refusal", "category": "safety"}


def test_invalid_utf8_is_a_failure():
    parsed = AnthropicMessageStream()
    parsed.feed_line(b'data: {"type":"ping","x":"\xff"}')
    assert parsed.finish() == "stream_json_parse"


@pytest.mark.parametrize("text", ["", "   "])
def test_plain_stream_probe_requires_visible_reply_even_with_valid_terminals(text):
    rows = events()
    rows[2]["delta"]["text"] = text
    result, body = client_result(wire(rows))
    assert result.success
    assert validate_profile_response("claude_native_stream", result.response_json, result,
        request_body=body, transport="claude_messages", reference_source="claude_native_messages") == "anthropic_content_missing"


@pytest.mark.parametrize("body", [None, {"stream": False}, {"stream": 1}])
def test_plain_stream_probe_cannot_use_a_nonstream_or_missing_request(body):
    result, _ = client_result(wire(events()))
    assert validate_profile_response("claude_native_stream", result.response_json, result,
        request_body=body, transport="claude_messages", reference_source="claude_native_messages") == "anthropic_stream_request_mismatch"
