"""Independent offline contract for DeepSeek Chat Prefix Completion beta."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Sequence


DEEPSEEK_BETA_PREFIX_URL = "https://api.deepseek.com/beta/chat/completions"
DEEPSEEK_BETA_PREFIX_API_FORM = "deepseek_beta_chat_prefix"
OFFICIAL_SOURCE_ID = "deepseek"
OFFICIAL_FAMILY_ID = "deepseek"
OFFICIAL_MODELS = frozenset({"deepseek-v4-pro", "deepseek-v4-flash"})

_REQUIRED_BODY_KEYS = frozenset({"model", "messages", "max_tokens", "stream"})
_OPTIONAL_BODY_KEYS = frozenset({"stop", "thinking"})
_MESSAGE_KEYS = frozenset({"role", "content"})
_FINAL_MESSAGE_KEYS = frozenset({"role", "content", "prefix"})
_MESSAGE_ROLES = frozenset({"system", "user", "assistant"})
_MAX_STOP_SEQUENCES = 16
_FINISH_REASONS = frozenset(
    {
        "stop",
        "length",
        "content_filter",
        "tool_calls",
        "insufficient_system_resource",
    }
)


@dataclass(frozen=True)
class DeepSeekBetaPrefixRequest:
    method: str
    url: str
    source_id: str
    family_id: str
    api_form: str
    _body_json: bytes = field(repr=False)

    @property
    def body(self) -> dict[str, Any]:
        """Return a fresh copy decoded from the immutable canonical snapshot."""

        return _decode_request_body(self._body_json)


def build_prefix_request(
    *,
    source_id: str,
    family_id: str,
    api_form: str,
    model: str,
    messages: Sequence[dict[str, Any]],
    max_tokens: int = 256,
    stop: Sequence[str] | None = None,
    thinking_enabled: bool | None = None,
) -> DeepSeekBetaPrefixRequest:
    """Build the official beta request without exposing an HTTP sender."""

    _validate_transport_identity(
        method="POST",
        url=DEEPSEEK_BETA_PREFIX_URL,
        source_id=source_id,
        family_id=family_id,
        api_form=api_form,
    )
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ValueError("messages must be a non-empty sequence")
    normalized = copy.deepcopy(list(messages))
    body: dict[str, Any] = {
        "model": model,
        "messages": normalized,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if stop is not None:
        if not isinstance(stop, Sequence) or isinstance(stop, (str, bytes)):
            raise ValueError("stop must be a non-empty sequence of non-empty strings")
        body["stop"] = copy.deepcopy(list(stop))
    if thinking_enabled is not None:
        if type(thinking_enabled) is not bool:
            raise ValueError("thinking_enabled must be boolean")
        body["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
    _validate_body(body)
    return DeepSeekBetaPrefixRequest(
        method="POST",
        url=DEEPSEEK_BETA_PREFIX_URL,
        source_id=source_id,
        family_id=family_id,
        api_form=api_form,
        _body_json=_canonical_body_bytes(body),
    )


def validate_prefix_response(
    request: DeepSeekBetaPrefixRequest,
    *,
    status_code: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate an offline fixture against a freshly revalidated request."""

    body = _validated_request_body(request)
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 100 <= status_code <= 599
    ):
        raise ValueError("status_code must be an HTTP status integer")
    if status_code != 200:
        if 200 <= status_code <= 299:
            raise ValueError("Prefix Completion success response must use HTTP 200")
        error = payload.get("error") if isinstance(payload, dict) else None
        return {
            "valid": False,
            "status_code": status_code,
            "error": error if isinstance(error, dict) else {"message": "http_error"},
        }
    if not isinstance(payload, dict) or payload.get("model") != body["model"]:
        raise ValueError("Prefix response returned-model identity mismatch")
    response_id = payload.get("id")
    if (
        not isinstance(response_id, str)
        or not response_id.strip()
        or response_id != response_id.strip()
    ):
        raise ValueError("Prefix response is missing id")
    if payload.get("object") != "chat.completion":
        raise ValueError("Prefix response object must be chat.completion")
    created = payload.get("created")
    if isinstance(created, bool) or not isinstance(created, int) or created < 0:
        raise ValueError("Prefix response is missing a valid created timestamp")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("Prefix response must contain exactly one choice")
    choice = choices[0]
    choice_index = choice.get("index") if isinstance(choice, dict) else None
    if (
        isinstance(choice_index, bool)
        or not isinstance(choice_index, int)
        or choice_index != 0
    ):
        raise ValueError("Prefix response choice must have index=0")
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("Prefix response is missing the assistant message")
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("Prefix response continuation must be non-empty")
    finish_reason = choice.get("finish_reason")
    if (
        not isinstance(finish_reason, str)
        or finish_reason not in _FINISH_REASONS
    ):
        raise ValueError("Prefix response finish_reason is unsupported")
    usage = payload.get("usage")
    return {
        "valid": True,
        "status_code": status_code,
        "model": payload["model"],
        "continuation": content,
        "finish_reason": finish_reason,
        "usage": copy.deepcopy(usage) if isinstance(usage, dict) else {},
    }


def _validated_request_body(request: DeepSeekBetaPrefixRequest) -> dict[str, Any]:
    if not isinstance(request, DeepSeekBetaPrefixRequest):
        raise ValueError("Prefix response validator requires a prefix request")
    _validate_transport_identity(
        method=request.method,
        url=request.url,
        source_id=request.source_id,
        family_id=request.family_id,
        api_form=request.api_form,
    )
    body = _decode_request_body(request._body_json)
    _validate_body(body)
    return body


def _validate_transport_identity(
    *,
    method: str,
    url: str,
    source_id: str,
    family_id: str,
    api_form: str,
) -> None:
    if method != "POST" or url != DEEPSEEK_BETA_PREFIX_URL:
        raise ValueError("Prefix Completion requires the official beta transport")
    if source_id != OFFICIAL_SOURCE_ID or family_id != OFFICIAL_FAMILY_ID:
        raise ValueError("Prefix Completion is restricted to the official DeepSeek source")
    if api_form != DEEPSEEK_BETA_PREFIX_API_FORM:
        raise ValueError("Prefix Completion must use its independent beta API form")


def _validate_body(body: Any) -> None:
    if not isinstance(body, dict):
        raise ValueError("Prefix request body must be an object")
    keys = set(body)
    if not _REQUIRED_BODY_KEYS.issubset(keys) or not keys.issubset(
        _REQUIRED_BODY_KEYS | _OPTIONAL_BODY_KEYS
    ):
        raise ValueError("Prefix request body does not match the approved schema")
    model = body["model"]
    if not isinstance(model, str) or model not in OFFICIAL_MODELS:
        raise ValueError("Model is not in the official Prefix Completion allowlist")
    max_tokens = body["max_tokens"]
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if body["stream"] is not False:
        raise ValueError("The offline Prefix Completion contract requires stream=false")
    if "thinking" in body and body["thinking"] not in ({"type": "enabled"}, {"type": "disabled"}):
        raise ValueError("thinking must select enabled or disabled")

    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty sequence")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError("Each message must be an object")
        if "reasoning_content" in message:
            raise ValueError(
                "reasoning_content is outside this narrow prefix-only contract"
            )
        is_final = index == len(messages) - 1
        expected_keys = _FINAL_MESSAGE_KEYS if is_final else _MESSAGE_KEYS
        if set(message) != expected_keys:
            raise ValueError("Prefix request message schema was modified")
        role = message["role"]
        if not isinstance(role, str) or role not in _MESSAGE_ROLES:
            raise ValueError("Prefix request message role is unsupported")
        content = message["content"]
        if not isinstance(content, str) or not content:
            raise ValueError("Prefix request message content must be a non-empty string")
        if is_final and (
            message["role"] != "assistant" or message.get("prefix") is not True
        ):
            raise ValueError("The final assistant message must set prefix=true")

    if "stop" in body:
        stop = body["stop"]
        if (
            not isinstance(stop, list)
            or not stop
            or len(stop) > _MAX_STOP_SEQUENCES
            or any(not isinstance(value, str) or not value for value in stop)
        ):
            raise ValueError(
                "stop must contain between 1 and 16 non-empty strings"
            )


def _canonical_body_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_request_body(body_json: Any) -> dict[str, Any]:
    if not isinstance(body_json, bytes):
        raise ValueError("Prefix request body snapshot must be immutable bytes")
    try:
        decoded = json.loads(body_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Prefix request body snapshot is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Prefix request body must be an object")
    _validate_body(decoded)
    if _canonical_body_bytes(decoded) != body_json:
        raise ValueError("Prefix request body snapshot is not canonical")
    return decoded
