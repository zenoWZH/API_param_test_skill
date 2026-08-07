from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from .config import get_api_key, get_provider_config, get_timeout_sec
from .credential_security import (
    ProviderCredential,
    redact_secrets,
    validate_profile_request_headers,
)
from .deepseek_params import (
    extract_claude_tool_uses,
    extract_content,
    extract_finish_reason,
    extract_openai_responses_function_calls,
    extract_openai_responses_text,
    extract_reasoning_content,
    extract_tool_calls,
    extract_usage,
)
from .metrics import classify_failure


@dataclass
class ChatResult:
    success: bool
    status_code: int | None
    latency_ms: float
    timestamp: float
    response_json: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    ttft_ms: float | None = None
    response_length: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cache_headers: dict[str, str] = field(default_factory=dict)
    error_type: str | None = None
    failure_classification: str | None = None
    raw_text: str = ""

    def __post_init__(self) -> None:
        self.response_json = redact_secrets(self.response_json)
        self.text = redact_secrets(self.text)
        self.reasoning_content = redact_secrets(self.reasoning_content)
        self.tool_calls = redact_secrets(self.tool_calls)
        self.usage = redact_secrets(self.usage)
        self.headers = redact_secrets(self.headers)
        self.cache_headers = redact_secrets(self.cache_headers)
        self.raw_text = redact_secrets(self.raw_text)


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_sec: int | None = None,
        provider: str | None = None,
        provider_label: str | None = None,
        api_interfaces: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec if timeout_sec is not None else get_timeout_sec()
        self.provider = provider or "unknown"
        self.provider_label = provider_label or self.provider
        self.api_interfaces = api_interfaces or {
            "chat_completions": {
                "base_url": self.base_url,
                "path": "/chat/completions",
                "auth": "bearer",
            },
            "claude_messages": {
                "base_url": self.base_url,
                "path": "/messages",
                "auth": "anthropic",
            },
            "gemini_generate_content": {
                "base_url": self.base_url[:-len("/openai")]
                if self.base_url.endswith("/openai")
                else self.base_url,
                "path": "/models/{model}:generateContent",
                "auth": "google_api_key",
            },
            "openai_responses": {
                "base_url": self.base_url,
                "path": "/responses",
                "auth": "bearer",
            },
        }
        self._credential = ProviderCredential.create(
            provider=self.provider,
            secret=api_key,
            base_urls=[
                self.base_url,
                *[
                    str(interface.get("base_url") or self.base_url)
                    for interface in self.api_interfaces.values()
                    if isinstance(interface, dict)
                ],
            ],
        )
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    @classmethod
    def from_config(cls, config: dict[str, Any], provider: str | None = None) -> "OpenAICompatibleClient":
        provider_cfg = get_provider_config(config, provider)
        return cls(
            base_url=str(provider_cfg.get("base_url", "https://yibuapi.com/v1")),
            api_key=get_api_key(config, provider_cfg["name"]),
            timeout_sec=get_timeout_sec(config),
            provider=str(provider_cfg["name"]),
            provider_label=str(provider_cfg.get("label") or provider_cfg["name"]),
            api_interfaces=_normalized_interfaces(provider_cfg),
        )

    def list_models(self) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        try:
            url = self._models_url()
            response = self.session.get(
                url,
                headers=self._auth_headers("chat_completions", url),
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            payload = _safe_json(response)
            success = 200 <= response.status_code <= 299
            return ChatResult(
                success=success,
                status_code=response.status_code,
                latency_ms=latency_ms,
                timestamp=timestamp,
                response_json=payload,
                raw_text=response.text,
                response_length=len(response.content or b""),
                headers=_headers(response),
                cache_headers=_cache_headers(response),
                error_type=None if success else "http_error",
                failure_classification=classify_failure(response.status_code),
            )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def chat_completion(self, body: dict[str, Any]) -> ChatResult:
        if body.get("stream"):
            return self._chat_completion_stream(body)
        return self._chat_completion_json(body)

    def count_tokens(
        self,
        transport: str,
        model: str,
        body: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Call an explicitly configured provider token-count interface.

        The interface is opt-in because OpenAI-compatible gateways do not share
        one count-token wire contract. Supported configuration keys are path,
        auth, transports, request_wrapper, and response_field.
        """
        interface = self.api_interfaces.get("token_count")
        if not isinstance(interface, dict):
            return None
        supported = interface.get("transports")
        if isinstance(supported, list) and transport not in {
            str(item) for item in supported
        }:
            return None
        url = self._transport_url("token_count", model)
        wrapper = interface.get("request_wrapper")
        payload = {str(wrapper): body} if wrapper else body
        try:
            response = self.session.post(
                url,
                json=payload,
                headers=self._auth_headers("token_count", url),
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
        except requests.RequestException:
            return None
        if not 200 <= response.status_code <= 299:
            return None
        parsed = _safe_json(response)
        field = str(interface.get("response_field") or "totalTokens")
        value: Any = parsed
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        try:
            tokens = int(value)
        except (TypeError, ValueError):
            return None
        if tokens < 0:
            return None
        return {
            "tokens": tokens,
            "evidence_level": "exact",
            "kind": "provider_count",
            "source": f"token_count:{field}",
            "note": "counted by the provider's separately configured token-count interface",
        }

    def gemini_generate_content(
        self,
        model: str,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        try:
            url = self._gemini_native_url(model)
            request_headers = self._auth_headers("gemini_generate_content", url)
            request_headers.update(validate_profile_request_headers(headers))
            response = self.session.post(
                url,
                json=body,
                headers=request_headers,
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            payload = _safe_json(response)
            candidates = payload.get("candidates") or []
            first_candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
            finish_reason = first_candidate.get("finishReason")
            usage = payload.get("usageMetadata") or {}
            status_success = 200 <= response.status_code <= 299
            failure = classify_failure(response.status_code, str(finish_reason) if finish_reason else None)
            success = status_success and failure is None
            return ChatResult(
                success=success,
                status_code=response.status_code,
                latency_ms=latency_ms,
                timestamp=timestamp,
                response_json=payload,
                text=_gemini_native_text(payload),
                finish_reason=str(finish_reason) if finish_reason else None,
                usage=usage if isinstance(usage, dict) else {},
                response_length=len(response.content or b""),
                headers=_headers(response),
                cache_headers=_cache_headers(response),
                error_type=None if status_success else "http_error",
                failure_classification=failure,
                raw_text=response.text,
            )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def claude_messages(self, body: dict[str, Any]) -> ChatResult:
        if body.get("stream"):
            return self._claude_messages_stream(body)
        return self._claude_messages_json(body)

    def openai_responses(self, body: dict[str, Any]) -> ChatResult:
        if body.get("stream"):
            return self._openai_responses_stream(body)
        return self._openai_responses_json(body)

    def _openai_responses_json(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        try:
            url = self._transport_url("openai_responses")
            response = self.session.post(
                url,
                json=body,
                headers=self._auth_headers("openai_responses", url),
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            payload = _safe_json(response)
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            status = str(payload.get("status") or "")
            status_success = 200 <= response.status_code <= 299
            failure = classify_failure(response.status_code, status or None)
            if status_success and status and status not in {"completed", "incomplete"}:
                # Treat explicit failed/cancelled Responses statuses as failures.
                if status in {"failed", "cancelled", "incomplete"} and payload.get("error"):
                    failure = failure or "request_failed"
            success = status_success and failure is None
            return ChatResult(
                success=success,
                status_code=response.status_code,
                latency_ms=latency_ms,
                timestamp=timestamp,
                response_json=payload,
                text=extract_openai_responses_text(payload),
                tool_calls=extract_openai_responses_function_calls(payload),
                finish_reason=status or None,
                usage=usage,
                response_length=len(response.content or b""),
                headers=_headers(response),
                cache_headers=_cache_headers(response),
                error_type=None if status_success else "http_error",
                failure_classification=failure,
                raw_text=response.text,
            )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def _openai_responses_stream(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        content_parts: list[str] = []
        output_items: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        status: str | None = None
        response_id: str | None = None
        model_name: str | None = None
        raw_lines: list[str] = []
        pending_json = ""
        ttft_ms: float | None = None
        error_type: str | None = None

        try:
            url = self._transport_url("openai_responses")
            with self.session.post(
                url,
                json=body,
                headers=self._auth_headers("openai_responses", url),
                timeout=self.timeout_sec,
                stream=True,
                allow_redirects=False,
            ) as response:
                response.encoding = "utf-8"
                for raw_line in response.iter_lines(decode_unicode=True):
                    line = _sse_payload_line(raw_line)
                    if line is None:
                        continue
                    raw_lines.append(line)
                    if pending_json:
                        pending_json += line
                    elif line.lstrip().startswith("{"):
                        pending_json = line
                    else:
                        continue
                    try:
                        event = json.loads(pending_json)
                        pending_json = ""
                    except json.JSONDecodeError:
                        if len(pending_json) > 65536:
                            error_type = "stream_json_parse"
                            pending_json = ""
                        continue

                    event_type = str(event.get("type") or "")
                    if event_type == "response.output_text.delta" and event.get("delta"):
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000
                        content_parts.append(str(event["delta"]))
                    elif event_type == "response.output_item.done":
                        item = event.get("item")
                        if isinstance(item, dict):
                            output_items.append(item)
                            if item.get("type") == "function_call" and ttft_ms is None:
                                ttft_ms = (time.perf_counter() - started) * 1000
                    elif event_type == "response.completed":
                        completed = event.get("response") or {}
                        if isinstance(completed, dict):
                            response_id = completed.get("id") or response_id
                            model_name = completed.get("model") or model_name
                            status = str(completed.get("status") or status or "completed")
                            if isinstance(completed.get("usage"), dict):
                                usage = completed["usage"]
                            if isinstance(completed.get("output"), list) and completed["output"]:
                                output_items = [
                                    item for item in completed["output"] if isinstance(item, dict)
                                ]
                    elif event_type == "response.failed":
                        failed = event.get("response") or {}
                        if isinstance(failed, dict):
                            status = str(failed.get("status") or "failed")
                            if isinstance(failed.get("error"), dict):
                                error_type = error_type or "request_failed"

                latency_ms = (time.perf_counter() - started) * 1000
                if pending_json and status is None:
                    error_type = error_type or "stream_json_parse"
                status_success = 200 <= response.status_code <= 299
                failure = classify_failure(response.status_code, status, error_type)
                success = status_success and failure is None
                payload = {
                    "id": response_id,
                    "object": "response",
                    "model": model_name,
                    "status": status,
                    "output": output_items,
                    "usage": usage,
                }
                text = "".join(content_parts) or extract_openai_responses_text(payload)
                return ChatResult(
                    success=success,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    timestamp=timestamp,
                    response_json=payload,
                    text=text,
                    tool_calls=extract_openai_responses_function_calls(payload),
                    finish_reason=status,
                    usage=usage,
                    ttft_ms=ttft_ms,
                    response_length=len("\n".join(raw_lines).encode("utf-8")),
                    headers=_headers(response),
                    cache_headers=_cache_headers(response),
                    error_type=error_type if error_type else (None if status_success else "http_error"),
                    failure_classification=failure,
                    raw_text="\n".join(raw_lines),
                )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def _chat_completion_json(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        try:
            url = self._url("/chat/completions")
            response = self.session.post(
                url,
                json=body,
                headers=self._auth_headers("chat_completions", url),
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            payload = _safe_json(response)
            finish_reason = extract_finish_reason(payload)
            usage = extract_usage(payload)
            status_success = 200 <= response.status_code <= 299
            failure = classify_failure(response.status_code, finish_reason)
            success = status_success and failure is None
            return ChatResult(
                success=success,
                status_code=response.status_code,
                latency_ms=latency_ms,
                timestamp=timestamp,
                response_json=payload,
                text=extract_content(payload),
                reasoning_content=extract_reasoning_content(payload),
                tool_calls=extract_tool_calls(payload),
                finish_reason=finish_reason,
                usage=usage,
                response_length=len(response.content or b""),
                headers=_headers(response),
                cache_headers=_cache_headers(response),
                error_type=None if status_success else "http_error",
                failure_classification=failure,
                raw_text=response.text,
            )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def _chat_completion_stream(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        final_usage: dict[str, Any] = {}
        finish_reason: str | None = None
        model_name: str | None = None
        system_fingerprint: str | None = None
        raw_lines: list[str] = []
        pending_json = ""
        ttft_ms: float | None = None
        error_type: str | None = None

        try:
            url = self._url("/chat/completions")
            with self.session.post(
                url,
                json=body,
                headers=self._auth_headers("chat_completions", url),
                timeout=self.timeout_sec,
                stream=True,
                allow_redirects=False,
            ) as response:
                response.encoding = "utf-8"
                for raw_line in response.iter_lines(decode_unicode=True):
                    line = _sse_payload_line(raw_line)
                    if line is None:
                        continue
                    raw_lines.append(line)
                    if line == "[DONE]":
                        break
                    if pending_json:
                        pending_json += line
                    elif line.lstrip().startswith("{"):
                        pending_json = line
                    else:
                        continue
                    try:
                        chunk = json.loads(pending_json)
                        pending_json = ""
                    except json.JSONDecodeError:
                        if len(pending_json) > 65536:
                            error_type = "stream_json_parse"
                            pending_json = ""
                        continue

                    if chunk.get("usage"):
                        final_usage = chunk["usage"]
                    if chunk.get("model"):
                        model_name = str(chunk["model"])
                    if chunk.get("system_fingerprint"):
                        system_fingerprint = str(chunk["system_fingerprint"])

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000
                        content_parts.append(delta["content"])
                    if delta.get("reasoning_content"):
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000
                        reasoning_parts.append(delta["reasoning_content"])
                    if delta.get("tool_calls"):
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000
                        if _is_openai_tool_stream_body(body):
                            _merge_openai_stream_tool_calls(tool_calls, delta["tool_calls"])
                        else:
                            tool_calls.extend(delta["tool_calls"])

                latency_ms = (time.perf_counter() - started) * 1000
                if pending_json and finish_reason is None:
                    error_type = error_type or "stream_json_parse"
                status_success = 200 <= response.status_code <= 299
                failure = classify_failure(response.status_code, finish_reason, error_type)
                success = status_success and failure is None
                text = "".join(content_parts)
                reasoning = "".join(reasoning_parts)
                response_json = {
                    "model": model_name,
                    "system_fingerprint": system_fingerprint,
                    "choices": [
                        {
                            "message": {
                                "content": text,
                                "reasoning_content": reasoning,
                                "tool_calls": tool_calls,
                            },
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": final_usage,
                }
                return ChatResult(
                    success=success,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    timestamp=timestamp,
                    response_json=response_json,
                    text=text,
                    reasoning_content=reasoning,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    usage=final_usage,
                    ttft_ms=ttft_ms,
                    response_length=len("\n".join(raw_lines).encode("utf-8")),
                    headers=_headers(response),
                    cache_headers=_cache_headers(response),
                    error_type=error_type if error_type else (None if status_success else "http_error"),
                    failure_classification=failure,
                    raw_text="\n".join(raw_lines),
                )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def _claude_messages_json(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        try:
            url = self._transport_url("claude_messages")
            response = self.session.post(
                url,
                json=body,
                headers=self._auth_headers("claude_messages", url),
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            payload = _safe_json(response)
            finish_reason = extract_finish_reason(payload)
            usage = extract_usage(payload)
            status_success = 200 <= response.status_code <= 299
            failure = classify_failure(response.status_code, finish_reason)
            success = status_success and failure is None
            return ChatResult(
                success=success,
                status_code=response.status_code,
                latency_ms=latency_ms,
                timestamp=timestamp,
                response_json=payload,
                text=extract_content(payload),
                tool_calls=extract_claude_tool_uses(payload),
                finish_reason=finish_reason,
                usage=usage,
                response_length=len(response.content or b""),
                headers=_headers(response),
                cache_headers=_cache_headers(response),
                error_type=None if status_success else "http_error",
                failure_classification=failure,
                raw_text=response.text,
            )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def _claude_messages_stream(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        content_parts: list[str] = []
        content_blocks: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        model_name: str | None = None
        raw_lines: list[str] = []
        pending_json = ""
        ttft_ms: float | None = None
        error_type: str | None = None

        try:
            url = self._transport_url("claude_messages")
            with self.session.post(
                url,
                json=body,
                headers=self._auth_headers("claude_messages", url),
                timeout=self.timeout_sec,
                stream=True,
                allow_redirects=False,
            ) as response:
                response.encoding = "utf-8"
                for raw_line in response.iter_lines(decode_unicode=True):
                    line = _sse_payload_line(raw_line)
                    if line is None:
                        continue
                    raw_lines.append(line)
                    if pending_json:
                        pending_json += line
                    elif line.lstrip().startswith("{"):
                        pending_json = line
                    else:
                        continue
                    try:
                        event = json.loads(pending_json)
                        pending_json = ""
                    except json.JSONDecodeError:
                        if len(pending_json) > 65536:
                            error_type = "stream_json_parse"
                            pending_json = ""
                        continue

                    event_type = event.get("type")
                    if event_type == "message_start":
                        message = event.get("message") or {}
                        if message.get("model"):
                            model_name = str(message["model"])
                        if isinstance(message.get("usage"), dict):
                            usage.update(message["usage"])
                    elif event_type == "content_block_start":
                        block = event.get("content_block")
                        if isinstance(block, dict):
                            content_blocks.append(block)
                            if block.get("type") == "tool_use" and ttft_ms is None:
                                ttft_ms = (time.perf_counter() - started) * 1000
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            if ttft_ms is None:
                                ttft_ms = (time.perf_counter() - started) * 1000
                            text = str(delta["text"])
                            content_parts.append(text)
                            if content_blocks and content_blocks[-1].get("type") == "text":
                                content_blocks[-1]["text"] = str(content_blocks[-1].get("text") or "") + text
                        elif delta.get("type") == "input_json_delta" and content_blocks:
                            content_blocks[-1]["partial_json"] = (
                                str(content_blocks[-1].get("partial_json") or "")
                                + str(delta.get("partial_json") or "")
                            )
                    elif event_type == "message_delta":
                        delta = event.get("delta") or {}
                        stop_reason = delta.get("stop_reason") or stop_reason
                        if isinstance(event.get("usage"), dict):
                            usage.update(event["usage"])
                    elif event_type == "message_stop":
                        break

                latency_ms = (time.perf_counter() - started) * 1000
                if pending_json and stop_reason is None:
                    error_type = error_type or "stream_json_parse"
                status_success = 200 <= response.status_code <= 299
                failure = classify_failure(response.status_code, stop_reason, error_type)
                success = status_success and failure is None
                payload = {
                    "model": model_name,
                    "content": content_blocks,
                    "stop_reason": stop_reason,
                    "usage": usage,
                }
                return ChatResult(
                    success=success,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    timestamp=timestamp,
                    response_json=payload,
                    text="".join(content_parts),
                    tool_calls=extract_claude_tool_uses(payload),
                    finish_reason=stop_reason,
                    usage=usage,
                    ttft_ms=ttft_ms,
                    response_length=len("\n".join(raw_lines).encode("utf-8")),
                    headers=_headers(response),
                    cache_headers=_cache_headers(response),
                    error_type=error_type if error_type else (None if status_success else "http_error"),
                    failure_classification=failure,
                    raw_text="\n".join(raw_lines),
                )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def _url(self, endpoint: str) -> str:
        if endpoint == "/chat/completions":
            return self._transport_url("chat_completions")
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    def _models_url(self) -> str:
        interface = self.api_interfaces.get("chat_completions") or {}
        base_url = str(interface.get("base_url") or self.base_url).rstrip("/")
        return f"{base_url}/models"

    def _transport_url(self, transport: str, model: str | None = None) -> str:
        interface = self.api_interfaces.get(transport)
        if not isinstance(interface, dict):
            raise ValueError(f"Provider {self.provider!r} has no {transport} interface.")
        base_url = str(interface.get("base_url") or self.base_url).rstrip("/")
        path = str(interface.get("path") or "")
        if model is not None:
            path = path.format(model=quote(model, safe=""))
        return f"{base_url}/{path.lstrip('/')}"

    def _gemini_native_url(self, model: str) -> str:
        return self._transport_url("gemini_generate_content", model)

    def _auth_headers(self, transport: str, url: str) -> dict[str, str]:
        interface = self.api_interfaces.get(transport) or {}
        auth = str(interface.get("auth") or "bearer")
        return self._credential.auth_headers(url=url, auth_mode=auth)


def _normalized_interfaces(provider_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    provider_base = str(provider_cfg.get("base_url") or "").rstrip("/")
    for transport, raw in (provider_cfg.get("api_interfaces") or {}).items():
        if not isinstance(raw, dict):
            continue
        interface = dict(raw)
        interface["base_url"] = str(
            interface.get("base_url") or provider_base
        ).rstrip("/")
        result[str(transport)] = interface
    return result


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {"data": payload}
    except ValueError:
        return {}


def _gemini_native_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))


def _headers(response: requests.Response) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in response.headers.items()}


def _cache_headers(response: requests.Response) -> dict[str, str]:
    lower = _headers(response)
    names = ("x-cache", "cf-cache-status", "x-oneapi-cache")
    return {name: lower[name] for name in names if name in lower}


def _is_openai_tool_stream_body(body: dict[str, Any]) -> bool:
    """OpenAI-compat tool_stream deltas must be merged by index across families."""
    return body.get("tool_stream") is True


# Backward-compatible alias used by older tests/imports.
_is_qwen_tool_stream_body = _is_openai_tool_stream_body


def _merge_openai_stream_tool_calls(
    tool_calls: list[dict[str, Any]],
    deltas: list[Any],
) -> None:
    """Merge OpenAI-compatible streaming tool_call deltas by index.

    tool_stream (Qwen/GLM/etc.) emits argument fragments across chunks for the
    same index; concatenate function.arguments and keep the first non-empty
    id/type/name.
    """
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        try:
            index = int(delta.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        while len(tool_calls) <= index:
            tool_calls.append(
                {
                    "id": "",
                    "type": "function",
                    "index": len(tool_calls),
                    "function": {"name": "", "arguments": ""},
                }
            )
        target = tool_calls[index]
        target["index"] = index
        delta_id = delta.get("id")
        if isinstance(delta_id, str) and delta_id and not target.get("id"):
            target["id"] = delta_id
        delta_type = delta.get("type")
        if isinstance(delta_type, str) and delta_type:
            target["type"] = delta_type
        function = target.setdefault("function", {"name": "", "arguments": ""})
        if not isinstance(function, dict):
            function = {"name": "", "arguments": ""}
            target["function"] = function
        delta_fn = delta.get("function") if isinstance(delta.get("function"), dict) else {}
        name = delta_fn.get("name")
        if isinstance(name, str) and name and not function.get("name"):
            function["name"] = name
        arguments = delta_fn.get("arguments")
        if arguments is None:
            continue
        existing = function.get("arguments")
        if not isinstance(existing, str):
            existing = "" if existing is None else str(existing)
        function["arguments"] = existing + (arguments if isinstance(arguments, str) else str(arguments))


def _sse_payload_line(raw_line: Any) -> str | None:
    if raw_line is None:
        return None
    if isinstance(raw_line, bytes):
        line = raw_line.decode("utf-8", errors="replace")
    else:
        line = str(raw_line)
        try:
            line = line.encode("latin1").decode("utf-8")
        except UnicodeError:
            pass
    line = line.strip().lstrip("\ufeff")
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line[len("data:") :].strip()
    elif line.startswith(("event:", "id:", "retry:")):
        return None
    return line or None


def _exception_result(started: float, timestamp: float, exc: requests.RequestException) -> ChatResult:
    error_type = exc.__class__.__name__
    return ChatResult(
        success=False,
        status_code=None,
        latency_ms=(time.perf_counter() - started) * 1000,
        timestamp=timestamp,
        error_type=error_type,
        failure_classification=error_type,
        raw_text=str(exc),
    )


DeepSeekClient = OpenAICompatibleClient
