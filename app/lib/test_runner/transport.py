"""Credential-scoped HTTP transport for the common workflow dispatcher.

The execution context owns budgets and receipts. This module owns authentication,
wire fidelity, response limits and protocol decoding; it never retries a request.
Importing or constructing it does not load credentials or open a connection.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from lib.anthropic_message_stream import AnthropicMessageStream
from lib.config import get_provider_interface
from lib.credential_security import credential_from_config
from lib.parameter_output_limit import enforce_parameter_test_output_limit
from .cancellation import response_deadline

SAFE_RESPONSE_HEADERS = frozenset({
    "content-type", "retry-after", "request-id", "x-request-id", "openai-processing-ms",
    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests",
    "x-model", "x-model-id", "x-response-model", "x-upstream-model", "x-provider",
    "x-cache", "cf-cache-status", "x-oneapi-cache",
})


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _strict_json(raw: bytes) -> Any:
    def invalid(value: str) -> None:
        raise ValueError("Non-finite response JSON value: " + value)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate response JSON field: " + key)
            value[key] = item
        return value
    return json.loads(raw, parse_constant=invalid, object_pairs_hook=unique)


def _redact_json_wire(raw: bytes, redact: Callable) -> bytes:
    """Decode escaped strings/field names before redacting a JSON capture.

    Retain exact wire bytes when the parsed document needs no redaction. For a
    changed document, retain every non-sensitive field and serialize only the
    protected copy; the receipt separately keeps the original byte hash.
    """
    try:
        payload = _strict_json(raw)
    except (ValueError, UnicodeError):
        return raw
    protected = redact(payload)
    if protected == payload:
        return raw
    return json.dumps(protected, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _redact_sse_wire(raw: bytes, redact: Callable) -> bytes:
    """Protect each legal data JSON event while retaining other SSE fields."""
    text = raw.decode("utf-8")

    def event(lines):
        indexes, values = [], []
        for index, line in enumerate(lines):
            content = line.rstrip("\r\n")
            if content == "data" or content.startswith("data:"):
                value = content[5:] if content.startswith("data:") else ""
                indexes.append(index)
                values.append(value[1:] if value.startswith(" ") else value)
        if not indexes:
            return "".join(lines)
        original = "\n".join(values).encode("utf-8")
        protected = _redact_json_wire(original, redact)
        if protected == original:
            return "".join(lines)
        first = indexes[0]
        newline = lines[first][len(lines[first].rstrip("\r\n")):]
        replacement = "data: " + protected.decode("utf-8") + newline
        return "".join(replacement if index == first else line
                       for index, line in enumerate(lines) if index == first or index not in indexes)

    output, current = [], []
    # SSE recognizes CR/LF, not every Unicode separator handled by splitlines.
    for line in re.split(r"(?<=\n)|(?<=\r)(?!\n)", text):
        if not line:
            continue
        if not line.rstrip("\r\n"):
            output.extend((event(current), line))
            current = []
        else:
            current.append(line)
    output.append(event(current))
    return redact("".join(output)).encode("utf-8")


def _redact_capture(raw: bytes, mime: str, redact: Callable) -> bytes:
    if mime == "text/event-stream":
        try:
            return _redact_sse_wire(raw, redact)
        except UnicodeError:
            pass
    elif mime == "application/json" or mime.endswith("+json"):
        raw = _redact_json_wire(raw, redact)
    # Also protect literal reflections outside JSON data (including error text
    # and SSE comments). Latin-1 round-trips arbitrary binary fixture bytes.
    return redact(raw.decode("latin-1")).encode("latin-1")


def _safe_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("Workflow request headers must be an object")
    forbidden = {"authorization", "proxy-authorization", "x-api-key", "api-key", "x-goog-api-key", "host", "cookie", "content-length"}
    result = {}
    for key, item in value.items():
        if (not isinstance(key, str) or not key or key.lower() in forbidden
                or not isinstance(item, str) or any(c in key + item for c in "\r\n\0")
                or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise ValueError("Workflow headers cannot override credentials or HTTP framing")
        result[key] = item
    return result


class HttpDispatcher:
    """Send only to one configured API origin or an explicit public-fixture URL.

    Requests are JSON objects containing method, path (API) or url (public), body,
    headers and optionally wire_base64/content_type. Actual credentials are never
    part of a request template or receipt.
    """

    def __init__(self, config: dict, provider: str, transport: str, *,
                 public_urls: tuple[str, ...] = (), session_factory: Callable = requests.Session,
                 credential_factory: Callable = credential_from_config,
                 response_byte_limit: int = 8 * 1024 * 1024,
                 public_response_byte_limit: int | None = None,
                 operation_auth: dict[str, str] | None = None):
        if type(response_byte_limit) is not int or response_byte_limit <= 0:
            raise ValueError("Response byte limit must be a positive integer")
        self._config = config
        self.provider, self.transport = provider, transport
        self.public_urls = frozenset(public_urls)
        self._session_factory, self._credential_factory = session_factory, credential_factory
        self.response_byte_limit = response_byte_limit
        self.public_response_byte_limit = response_byte_limit if public_response_byte_limit is None else public_response_byte_limit
        if type(self.public_response_byte_limit) is not int or self.public_response_byte_limit <= 0:
            raise ValueError("Public response byte limit must be a positive integer")
        self.operation_auth = copy.deepcopy(operation_auth or {})
        if any(not isinstance(prefix, str) or not prefix.startswith("/") or prefix.startswith("//")
               or urlsplit(prefix).path != prefix or ".." in prefix.split("/")
               or mode not in {"bearer", "anthropic"} for prefix, mode in self.operation_auth.items()):
            raise ValueError("Operation authentication requires reviewed relative path prefixes")
        self._credential = None

    def _api_target(self, request: dict, context: Any) -> tuple[str, str, str]:
        target = context.target
        execution = target.get("execution_target") or target
        if execution.get("provider_id", execution.get("provider")) != self.provider:
            raise ValueError("Dispatcher provider differs from the frozen target")
        interface = get_provider_interface(self._config, self.transport, self.provider)
        base = str(interface.get("base_url") or "").rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Workflow API requires an exact configured HTTPS origin")
        expected_base = target.get("base_url")
        if expected_base and str(expected_base).rstrip("/") != base:
            raise ValueError("Configured API endpoint differs from the frozen target")
        path = request.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("API operation requires an absolute relative path")
        relative = urlsplit(path)
        if relative.scheme or relative.netloc or relative.fragment or ".." in relative.path.split("/"):
            raise ValueError("API operation cannot change origin or escape its path")
        # Definitions use full /v1 paths while provider bases commonly include /v1.
        prefix = parsed.path.rstrip("/")
        url = (parsed.scheme + "://" + parsed.netloc + path
               if prefix and (relative.path == prefix or relative.path.startswith(prefix + "/"))
               else base + path)
        auth = str(interface.get("auth") or "bearer")
        for operation_prefix in sorted(self.operation_auth, key=len, reverse=True):
            if relative.path == operation_prefix or relative.path.startswith(operation_prefix + "/"):
                auth = self.operation_auth[operation_prefix]
                break
        return url, auth, str(interface.get("anthropic_version") or "")

    def __call__(self, request: dict, *, timeout: float, context: Any) -> dict:
        if not isinstance(request, dict) or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A request object and finite positive timeout are required")
        request = copy.deepcopy(request)
        method = request.get("method", "POST")
        if method not in {"GET", "POST", "DELETE", "PATCH", "PUT", "HEAD"}:
            raise ValueError("Unsupported workflow HTTP method")
        headers = _safe_headers(request.get("headers", {}))
        public = request.get("public", False)
        if type(public) is not bool:
            raise ValueError("public must be boolean")
        if public:
            url = request.get("url")
            if method != "GET" or url not in self.public_urls or urlsplit(url).scheme != "https":
                raise ValueError("Public fixture request is outside the frozen URL allowlist")
            auth_headers = {}
        else:
            url, auth, version = self._api_target(request, context)
            if version:
                headers.setdefault("anthropic-version", version)
        body = request.get("body")
        if "body" in request and "wire_base64" in request:
            raise ValueError("Choose a JSON body or exact wire bytes, not both")
        path = urlsplit(url).path.rstrip("/")
        generation = method == "POST" and (path.endswith(("/messages", "/chat/completions", "/responses", "/completions"))
                                            or path.endswith((":generateContent", ":streamGenerateContent")))
        if (context.target.get("modality") == "image"
                and self.transport in {"chat_completions", "gemini_generate_content"}):
            generation = False  # Image-only protocols have no invented text cap.
        if generation:
            if not isinstance(body, dict):
                raise ValueError("Generation request requires a JSON object with an explicit output allowance")
            limited = copy.deepcopy(body)
            enforce_parameter_test_output_limit(limited, self.transport)
            if canonical_bytes(limited) != canonical_bytes(body):
                raise ValueError("Frozen generation request violates its explicit output allowance")
        wire = None
        if "wire_base64" in request:
            wire = base64.b64decode(request["wire_base64"], validate=True)
        elif "body" in request:
            wire = canonical_bytes(body)
            headers.setdefault("Content-Type", "application/json")
        if "content_type" in request:
            content_type = request["content_type"]
            if not isinstance(content_type, str) or any(c in content_type for c in "\r\n\0"):
                raise ValueError("Invalid content type")
            headers["Content-Type"] = content_type
        maximum = self.public_response_byte_limit if public else self.response_byte_limit
        limit = request.get("response_byte_limit", maximum)
        if type(limit) is not int or not 0 < limit <= maximum:
            raise ValueError("Request response limit exceeds its dispatcher allowance")
        if type(request.get("capture_raw", False)) is not bool:
            raise ValueError("capture_raw must be boolean")
        if not public:
            # Validate endpoint, headers, body and limits before resolving a key.
            if self._credential is None:
                self._credential = self._credential_factory(self._config, self.provider)
            auth_headers = self._credential.auth_headers(url=url, auth_mode=auth)
        row = {"http_status": None, "response": None, "response_complete": False,
               "stream_observed": False, "stream_error": None,
               "request_bytes_sha256": hashlib.sha256(wire or b"").hexdigest(),
               "request_byte_length": len(wire or b""), "method": method, "endpoint": url}
        started = time.monotonic()
        raw = bytearray()
        try:
            # This deadline also bounds a peer that continuously trickles bytes.
            with self._session_factory() as session:
                session.trust_env = False
                session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
                with response_deadline(timeout):
                    with session.request(method, url, headers={**auth_headers, **headers}, data=wire,
                                         timeout=(min(15, timeout), timeout), stream=True, allow_redirects=False) as response:
                        row.update(http_status=response.status_code, content_type=response.headers.get("content-type", ""),
                                   request_id=response.headers.get("request-id") or response.headers.get("x-request-id"))
                        row["response_headers"] = {str(key).lower(): str(value) for key, value in response.headers.items()
                                                   if str(key).lower() in SAFE_RESPONSE_HEADERS}
                        for chunk in response.iter_content(65536):
                            raw.extend(chunk)
                            if len(raw) > limit:
                                raise ValueError("Workflow response exceeds byte allowance")
                            if time.monotonic() - started > timeout:
                                raise TimeoutError("Workflow response deadline exceeded")
                        mime = row["content_type"].lower().split(";", 1)[0].strip()
                        if public:
                            row["raw_base64"] = base64.b64encode(raw).decode()
                        elif mime == "text/event-stream":
                            row["stream_observed"] = True
                            row["stream_events_text"] = bytes(raw).decode("utf-8")
                            if self.transport == "claude_messages":
                                parser = AnthropicMessageStream()
                                for line in bytes(raw).splitlines():
                                    parser.feed_line(line)
                                row["stream_error"] = parser.finish()
                                row["response"] = parser.message
                            else:
                                row["raw_base64"] = base64.b64encode(raw).decode()
                        elif not raw and response.status_code == 204:
                            row["response"] = None
                        elif mime == "application/json" or mime.endswith("+json"):
                            row["response"] = _strict_json(bytes(raw))
                        else:
                            raise ValueError("Unexpected workflow response content type")
                        row["response_complete"] = True
                        if request.get("capture_raw"):
                            row["raw_base64"] = base64.b64encode(raw).decode()
        except (requests.RequestException, ValueError, TimeoutError) as exc:
            # Unknown server-side effects stay unknown; the engine owns creation state.
            row["error_type"] = type(exc).__name__
        row.update(response_bytes_sha256=hashlib.sha256(raw).hexdigest(), response_byte_length=len(raw),
                   elapsed_seconds=round(time.monotonic() - started, 6))
        captured = bytes(raw)
        if self._credential is not None:
            mime = str(row.get("content_type") or "").lower().split(";", 1)[0].strip()
            captured = _redact_capture(captured, mime, self._credential.redact)
            if "stream_events_text" in row:
                row["stream_events_text"] = captured.decode("utf-8")
        if "raw_base64" in row:
            row.update(raw_base64=base64.b64encode(captured).decode(),
                       captured_bytes_sha256=hashlib.sha256(captured).hexdigest(),
                       captured_byte_length=len(captured), raw_redacted=captured != bytes(raw))
        return self._credential.redact(row) if self._credential is not None else row
