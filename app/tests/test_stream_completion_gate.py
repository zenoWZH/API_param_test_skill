from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from lib.client import OpenAICompatibleClient


class StreamResponse:
    status_code = 200
    headers = {}
    encoding = "utf-8"

    def __init__(self, events: list[dict | str]):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self, **_kwargs):
        for event in self.events:
            yield "data: " + (event if isinstance(event, str) else json.dumps(event))


def client_with(events):
    client = OpenAICompatibleClient("https://offline.example/v1", "offline")
    client.session.post = Mock(return_value=StreamResponse(events))
    return client


def chunk(index=0, text="", finish=None):
    return {"choices": [{
        "index": index, "delta": {"content": text}, "finish_reason": finish,
    }]}


@pytest.mark.parametrize("events,error", [
    ([chunk(text="OK", finish="stop")], "stream_terminal_missing"),
    ([chunk(text="OK"), "[DONE]"], "stream_finish_reason_missing"),
    ([chunk(text="OK", finish="stop"), chunk(text="injected"), "[DONE]"], "stream_data_after_finish"),
    ([chunk(text="OK", finish="stop"), "[DONE]", chunk(text="injected")], "stream_data_after_terminal"),
    ([chunk(text="OK", finish="stop"), {"error": {"message": "aborted"}}, "[DONE]"], "stream_error_event"),
])
def test_chat_stream_does_not_accept_partial_or_appended_output(events, error):
    result = client_with(events).chat_completion({"model": "offline", "stream": True})
    assert result.success is False
    assert result.error_type == error


def test_chat_stream_preserves_every_choice_for_completion_audit():
    events = [
        chunk(1, "second"), chunk(0, "first"), chunk(1, finish="length"),
        chunk(0, finish="stop"),
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
        "[DONE]",
    ]
    result = client_with(events).chat_completion({"model": "offline", "stream": True, "n": 2})
    assert [c["message"]["content"] for c in result.response_json["choices"]] == ["first", "second"]
    assert [c["finish_reason"] for c in result.response_json["choices"]] == ["stop", "length"]
    assert result.usage["completion_tokens"] == 4


def test_chat_stream_requires_all_requested_choices():
    result = client_with([chunk(text="OK", finish="stop"), "[DONE]"]).chat_completion(
        {"model": "offline", "stream": True, "n": 2}
    )
    assert result.success is False
    assert result.error_type == "stream_choice_count_mismatch"


def test_valid_chat_terminal_usage_remains_available():
    result = client_with([
        chunk(text="OK", finish="stop"),
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}},
        "[DONE]",
    ]).chat_completion({"model": "offline", "stream": True})
    assert result.success is True
    assert result.text == "OK"
    assert result.usage["total_tokens"] == 11


@pytest.mark.parametrize("events,error", [
    ([{"type": "response.output_text.delta", "delta": "OK"}], "stream_terminal_missing"),
    ([{"type": "response.completed", "response": {"status": "completed", "output": []}},
      {"type": "response.output_text.delta", "delta": "injected"}], "stream_data_after_terminal"),
    ([{"type": "error", "message": "aborted"}], "stream_error_event"),
    ([{"type": "response.completed", "response": {"status": "completed", "output": []}},
      "unexpected trailing text"], "stream_data_after_terminal"),
    ([{"type": "response.completed", "response": {"status": "completed", "error": {"message": "failed"}}}], "request_failed"),
])
def test_responses_requires_terminal_and_rejects_post_terminal_data(events, error):
    result = client_with(events).openai_responses({"model": "offline", "input": "OK", "stream": True})
    assert result.success is False
    assert result.error_type == error


@pytest.mark.parametrize("streamed,terminal,expected_success", [
    ("O", "OK", False),
    ("OK injected", "OK", False),
    ("NO", "OK", False),
    ("OK", "", False),
    ("OK", "OK", True),
    (None, "OK", True),
])
def test_responses_delta_must_agree_with_completed_text(streamed, terminal, expected_success):
    from lib.token_audit import audit_exchange

    events = [] if streamed is None else [
        {"type": "response.output_text.delta", "delta": streamed},
    ]
    events.append({"type": "response.completed", "response": {
        "status": "completed",
        "output": [{"type": "message", "status": "completed",
                    "content": [{"type": "output_text", "text": terminal}]}],
        "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
    }})
    body = {"model": "offline", "input": "Say OK", "stream": True, "max_output_tokens": 256}
    result = client_with(events).openai_responses(body)

    assert result.success is expected_success
    assert result.error_type == (None if expected_success else "stream_output_mismatch")
    assert result.text == (terminal or streamed)
    audit = audit_exchange(body, result, "openai_responses", {}, "initial")
    assert audit["validation_pass"] is expected_success


def test_responses_empty_terminal_output_cannot_reuse_earlier_item():
    events = [
        {"type": "response.output_text.delta", "delta": "OK"},
        {"type": "response.output_item.done", "item": {
            "type": "message", "status": "completed",
            "content": [{"type": "output_text", "text": "OK"}],
        }},
        {"type": "response.completed", "response": {
            "status": "completed", "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
        }},
    ]
    result = client_with(events).openai_responses(
        {"model": "offline", "input": "Say OK", "stream": True}
    )
    assert result.success is False
    assert result.error_type == "stream_output_mismatch"
    assert result.response_json["output"] == []
