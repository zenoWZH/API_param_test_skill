"""Pure Anthropic Messages SSE framing and native message accumulation.

This validates the wire protocol, not model capabilities or prompt semantics.
Compatible endpoints keep their returned identity and native usage fields.
"""
from __future__ import annotations

import copy
import json
from typing import Any


class _Invalid(ValueError):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise _Invalid(code)


def _json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "stream_duplicate_json_key")
            result[key] = value
        return result

    def constant(_value):
        raise _Invalid("stream_nonfinite_json_number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


class AnthropicMessageStream:
    """Consume complete SSE lines without turning EOF into message_stop."""

    def __init__(self) -> None:
        self.message: dict[str, Any] = {}
        self.error_type: str | None = None
        self.state = "before"
        self.active: int | None = None
        self._name: str | None = None
        self._data: list[str] = []
        self._frame_size = 0
        self._partial_json: dict[int, str] = {}
        self.output_observed = False

    @property
    def text(self) -> str:
        return "".join(block.get("text", "") for block in self.message.get("content", [])
                       if block.get("type") == "text")

    @property
    def reasoning(self) -> str:
        return "".join(block.get("thinking", "") for block in self.message.get("content", [])
                       if block.get("type") == "thinking")

    def feed_line(self, line: str | bytes) -> None:
        if self.error_type:
            return
        try:
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            _require(type(line) is str, "stream_invalid_line")
            if not line:
                self._dispatch_frame()
                return
            if line.startswith(":"):
                return
            key, separator, value = line.partition(":")
            if not separator:
                value = ""
            if value.startswith(" "):
                value = value[1:]
            if key == "event":
                self._name = value
            elif key == "data":
                self._frame_size += len(value)
                _require(self._frame_size <= 2 * 1024 * 1024, "stream_event_too_large")
                self._data.append(value)
        except (_Invalid, UnicodeError, TypeError, ValueError, RecursionError,
                KeyError, IndexError, AttributeError) as exc:
            self.error_type = str(exc) if isinstance(exc, _Invalid) else "stream_json_parse"

    def _dispatch_frame(self) -> None:
        name, data = self._name, self._data
        self._name, self._data, self._frame_size = None, [], 0
        if not data:
            return
        event = _json("\n".join(data))
        _require(type(event) is dict and type(event.get("type")) is str, "stream_invalid_event")
        _require(name is None or name == event["type"], "stream_event_type_mismatch")
        self._event(event)

    def _usage(self, value: Any, *, initial: bool = False) -> None:
        _require(type(value) is dict, "stream_usage_missing")
        required = ("input_tokens", "output_tokens") if initial else ("output_tokens",)
        for key in required:
            _require(type(value.get(key)) is int and value[key] >= 0, "stream_usage_invalid")
        usage = self.message.setdefault("usage", {})
        previous = usage.get("output_tokens")
        if type(previous) is int:
            _require(value["output_tokens"] >= previous, "stream_usage_decreased")
        for key, item in value.items():
            if key in {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"}:
                _require(item is None or type(item) is int and item >= 0, "stream_usage_invalid")
            # Native message_delta may omit/null unchanged counters. Neither
            # absence nor null means zero, and cumulative output is not summed.
            if item is not None or key not in usage:
                usage[key] = copy.deepcopy(item)

    def _event(self, event: dict[str, Any]) -> None:
        typ = event["type"]
        if typ == "error":
            raise _Invalid("stream_error_event")
        _require(self.state != "stopped", "stream_event_after_message_stop")
        if typ == "ping":
            return
        if typ == "message_start":
            _require(self.state == "before", "stream_duplicate_message_start")
            message = event.get("message")
            _require(type(message) is dict and message.get("type") == "message"
                     and message.get("role") == "assistant", "stream_invalid_message_start")
            _require(all(type(message.get(k)) is str and message[k].strip() for k in ("id", "model")),
                     "stream_identity_missing")
            _require(message.get("content") == [] and message.get("stop_reason") is None,
                     "stream_invalid_message_start")
            self.message = copy.deepcopy(message)
            self.message["usage"] = {}
            self._usage(message.get("usage"), initial=True)
            self.state = "content"
            return
        if typ not in {"content_block_start", "content_block_delta", "content_block_stop",
                       "message_delta", "message_stop"}:
            # Anthropic permits future event types. They cannot supply a
            # required start, content transition, usage update or terminal.
            return
        _require(self.state != "before", "stream_event_before_message_start")
        if typ.startswith("content_block_"):
            _require(self.state == "content", "stream_content_after_message_delta")
            index = event.get("index")
            _require(type(index) is int and index >= 0, "stream_invalid_content_index")
            blocks = self.message["content"]
            if typ == "content_block_start":
                _require(self.active is None and index == len(blocks), "stream_content_start_out_of_order")
                block = event.get("content_block")
                _require(type(block) is dict and type(block.get("type")) is str, "stream_invalid_content_block")
                for kind, field in (("text", "text"), ("thinking", "thinking")):
                    if block["type"] == kind:
                        _require(type(block.get(field)) is str, "stream_invalid_content_block")
                if block["type"] in {"tool_use", "server_tool_use"}:
                    _require(type(block.get("input")) is dict, "stream_invalid_tool_input")
                if "citations" in block:
                    _require(type(block["citations"]) is list, "stream_invalid_citation_delta")
                blocks.append(copy.deepcopy(block))
                self.active = index
                self.output_observed |= bool(block.get("text")) or block["type"] in {"tool_use", "server_tool_use"}
            elif typ == "content_block_delta":
                _require(self.active == index, "stream_delta_outside_open_block")
                block, delta = blocks[index], event.get("delta")
                _require(type(delta) is dict, "stream_invalid_content_delta")
                delta_type = delta.get("type")
                if delta_type in {"text_delta", "thinking_delta", "signature_delta"}:
                    field = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature"}[delta_type]
                    expected = "text" if field == "text" else "thinking"
                    _require(block["type"] == expected and type(delta.get(field)) is str, "stream_invalid_content_delta")
                    block[field] = block.get(field, "") + delta[field]
                    self.output_observed |= field == "text" and bool(delta[field])
                elif delta_type == "input_json_delta":
                    _require(block["type"] in {"tool_use", "server_tool_use"} and type(delta.get("partial_json")) is str,
                             "stream_invalid_tool_delta")
                    self._partial_json[index] = self._partial_json.get(index, "") + delta["partial_json"]
                elif delta_type == "citations_delta":
                    _require(block["type"] == "text" and type(delta.get("citation")) is dict, "stream_invalid_citation_delta")
                    block.setdefault("citations", []).append(copy.deepcopy(delta["citation"]))
                else:
                    raise _Invalid("stream_unknown_content_delta")
            else:
                _require(self.active == index, "stream_content_stop_out_of_order")
                if index in self._partial_json:
                    value = _json(self._partial_json.pop(index))
                    _require(type(value) is dict, "stream_invalid_tool_input")
                    blocks[index]["input"] = value
                self.active = None
            return
        if typ == "message_delta":
            _require(self.state in {"content", "delta"} and self.active is None,
                     "stream_message_delta_before_content_stop")
            delta = event.get("delta")
            _require(type(delta) is dict, "stream_invalid_message_delta")
            for key in ("stop_reason", "stop_sequence", "stop_details", "container"):
                if delta.get(key) is not None:
                    self.message[key] = copy.deepcopy(delta[key])
            self._usage(event.get("usage"))
            self.state = "delta"
        else:
            _require(self.state == "delta" and self.active is None, "stream_message_stop_before_delta")
            _require(type(self.message.get("stop_reason")) is str and bool(self.message["stop_reason"]),
                     "stream_finish_reason_missing")
            self.state = "stopped"

    def finish(self) -> str | None:
        if not self.error_type:
            if self._data or self._name is not None:
                self.error_type = "stream_unterminated_sse_frame"
            elif self.state != "stopped":
                self.error_type = "stream_missing_message_stop"
        return self.error_type
