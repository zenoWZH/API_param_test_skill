from __future__ import annotations

import copy
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
    extract_gemini_interactions_function_calls,
    extract_gemini_interactions_text,
    extract_openai_responses_function_calls,
    extract_openai_responses_text,
    extract_reasoning_content,
    extract_tool_calls,
    extract_usage,
)
from .metrics import classify_failure
from .gemini_api_version import build_gemini_api_url, is_ai_studio_origin


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
            "gemini_interactions": {
                "base_url": _gemini_api_origin(self.base_url),
                "path": "/v1beta/interactions"
                if is_ai_studio_origin(self.base_url) else "/v1/interactions",
                "auth": "google_api_key",
            },
            "openai_responses": {
                "base_url": self.base_url,
                "path": "/responses",
                "auth": "bearer",
            },
            "fim_completions": {
                "base_url": self.base_url,
                "path": "/beta/completions",
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

    def chat_completion(
        self, body: dict[str, Any], *, cache_affinity_key: str | None = None,
    ) -> ChatResult:
        handler = self._chat_completion_stream if body.get("stream") else self._chat_completion_json
        return handler(body) if cache_affinity_key is None else handler(body, cache_affinity_key=cache_affinity_key)

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
        counted_body = copy.deepcopy(body)
        model_field = interface.get("request_model_field")
        if model_field:
            counted_body[str(model_field)] = "models/" + model.removeprefix("models/")
        payload = {str(wrapper): counted_body} if wrapper else counted_body
        count_snapshot = copy.deepcopy(payload)
        try:
            response = self.session.post(
                url,
                json=payload,
                headers=self._auth_headers("token_count", url),
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
        except Exception:
            if payload != count_snapshot:
                return {"tokens": None, "evidence_level": "unavailable", "kind": "provider_count",
                        "covers_full_input": False, "request_integrity": "fail",
                        "note": "token-count request changed during failed dispatch"}
            return None
        if payload != count_snapshot:
            return {"tokens": None, "evidence_level": "unavailable", "kind": "provider_count",
                    "covers_full_input": False, "request_integrity": "fail",
                    "note": "token-count request changed during dispatch"}
        if not 200 <= response.status_code <= 299:
            return None
        parsed = _safe_json(response)
        field = str(interface.get("response_field") or "totalTokens")
        value: Any = parsed
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        tokens = value
        return {
            "tokens": tokens,
            "evidence_level": "official_count",
            "covers_full_input": True,
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

    def gemini_interactions(self, body: dict[str, Any]) -> ChatResult:
        """Call the configured Gemini Interactions JSON or SSE transport."""
        if body.get("stream") is True:
            return self._gemini_interactions_stream(body)
        return self._gemini_interactions_json(body)


    def _gemini_interactions_json(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        try:
            url = self._transport_url("gemini_interactions")
            response = self.session.post(
                url,
                json=body,
                headers=self._auth_headers("gemini_interactions", url),
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            payload = _safe_json(response)
            status = str(payload.get("status") or "")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            status_success = 200 <= response.status_code <= 299
            interaction_error = (
                f"interaction_status:{status}"
                if status_success and status not in {"completed", "requires_action"}
                else None
            )
            failure = classify_failure(
                response.status_code,
                status or None,
                interaction_error,
            )
            return ChatResult(
                success=status_success and failure is None,
                status_code=response.status_code,
                latency_ms=latency_ms,
                timestamp=timestamp,
                response_json=payload,
                text=extract_gemini_interactions_text(payload),
                tool_calls=extract_gemini_interactions_function_calls(payload),
                finish_reason=status or None,
                usage=usage,
                response_length=len(response.content or b""),
                headers=_headers(response),
                cache_headers=_cache_headers(response),
                error_type=interaction_error if interaction_error else (None if status_success else "http_error"),
                failure_classification=failure,
                raw_text=response.text,
            )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)


    def _gemini_interactions_stream(self, body: dict[str, Any]) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        raw_lines: list[str] = []
        steps_by_index: dict[int, dict[str, Any]] = {}
        argument_fragments: dict[int, str] = {}
        interaction: dict[str, Any] = {}
        status: str | None = None
        usage: dict[str, Any] = {}
        pending_json = ""
        ttft_ms: float | None = None
        error_type: str | None = None
        saw_done = False
        from .gemini_interactions_stream import InteractionStream, strict_json, stateless_text_stream_scope, terminal_steps_match
        protocol = InteractionStream(body.get("model"))

        try:
            url = self._transport_url("gemini_interactions")
            protocol = InteractionStream(body.get("model"), allow_empty_resource_ids=stateless_text_stream_scope(body, url))
            with self.session.post(
                url,
                json=body,
                headers=self._auth_headers("gemini_interactions", url),
                timeout=self.timeout_sec,
                stream=True,
                allow_redirects=False,
            ) as response:
                response.encoding = "utf-8"
                for line in protocol.payloads(response.iter_lines(decode_unicode=False)):
                    raw_lines.append(line)
                    if line == "[DONE]":
                        saw_done = True
                        continue
                    if pending_json:
                        pending_json += line
                    elif line.lstrip().startswith("{"):
                        pending_json = line
                    else:
                        continue
                    try:
                        event = strict_json(pending_json)
                        pending_json = ""
                    except (ValueError, TypeError, RecursionError):
                        if len(pending_json) > 65536:
                            error_type = "stream_json_parse"
                            pending_json = ""
                        continue

                    event_type = str(event.get("event_type") or event.get("type") or "")
                    if event_type == "interaction.created":
                        created = event.get("interaction")
                        if isinstance(created, dict):
                            interaction.update(created)
                            status = str(created.get("status") or status or "") or None
                    elif event_type in {"interaction.status_update", "interaction.in_progress"}:
                        status = str(event.get("status") or status or "") or None
                    elif event_type == "step.start":
                        index = _interaction_step_index(event)
                        step = event.get("step")
                        if index is not None and isinstance(step, dict):
                            steps_by_index[index] = dict(step)
                            if step.get("type") == "function_call" and ttft_ms is None:
                                ttft_ms = (time.perf_counter() - started) * 1000
                    elif event_type == "step.delta":
                        index = _interaction_step_index(event)
                        delta = event.get("delta")
                        if index is not None and isinstance(delta, dict):
                            step = steps_by_index.setdefault(index, {"type": "model_output"})
                            delta_type = str(delta.get("type") or "")
                            if delta_type == "text" and isinstance(delta.get("text"), str):
                                if ttft_ms is None:
                                    ttft_ms = (time.perf_counter() - started) * 1000
                                _append_interaction_text(step, delta["text"])
                            elif delta_type in {"arguments", "arguments_delta"}:
                                fragment = delta.get("arguments")
                                if isinstance(fragment, str):
                                    argument_fragments[index] = argument_fragments.get(index, "") + fragment
                                elif isinstance(fragment, dict):
                                    step["arguments"] = fragment
                            elif delta_type == "thought_signature" and delta.get("signature"):
                                step["signature"] = delta["signature"]
                            elif delta_type in {"thought_summary", "thought"}:
                                _append_interaction_thought_summary(step, delta)
                    elif event_type == "step.stop":
                        index = _interaction_step_index(event)
                        if index is not None and argument_fragments.get(index):
                            fragment = argument_fragments[index]
                            try:
                                parsed_arguments = strict_json(fragment)
                            except (ValueError, TypeError, RecursionError):
                                error_type = error_type or "stream_tool_arguments_parse"
                            else:
                                if isinstance(parsed_arguments, dict):
                                    steps_by_index.setdefault(index, {})["arguments"] = parsed_arguments
                                else:
                                    error_type = error_type or "stream_tool_arguments_parse"
                    elif event_type in {
                        "interaction.completed",
                        "interaction.failed",
                        "interaction.cancelled",
                        "interaction.incomplete",
                    }:
                        completed = event.get("interaction")
                        if isinstance(completed, dict):
                            if "steps" in completed and not terminal_steps_match(
                                completed["steps"], [steps_by_index[index] for index in sorted(steps_by_index)]
                            ):
                                protocol.fail("interaction_stream_terminal_content_mismatch")
                            interaction.update(completed)
                            status = str(
                                completed.get("status")
                                or status
                                or event_type.removeprefix("interaction.")
                            )
                            if isinstance(completed.get("usage"), dict):
                                usage = completed["usage"]
                        if event_type != "interaction.completed":
                            error_type = error_type or event_type.replace(".", "_")
                    elif event_type == "error":
                        error_type = "interaction_stream_error"

                if protocol.errors:
                    error_type = error_type or protocol.errors[0]
                latency_ms = (time.perf_counter() - started) * 1000
                if pending_json and status is None:
                    error_type = error_type or "stream_json_parse"
                if not saw_done:
                    error_type = error_type or "stream_missing_done"
                if not usage and isinstance(interaction.get("usage"), dict):
                    usage = interaction["usage"]
                interaction["status"] = status
                terminal_steps = interaction.get("steps")
                if not isinstance(terminal_steps, list) or not terminal_steps:
                    if steps_by_index:
                        interaction["steps"] = [
                            steps_by_index[index] for index in sorted(steps_by_index)
                        ]
                    elif not isinstance(terminal_steps, list):
                        interaction["steps"] = []
                interaction["usage"] = usage
                interaction["_stream_validation"] = protocol.diagnostics()
                status_success = 200 <= response.status_code <= 299
                if status_success and status not in {"completed", "requires_action"}:
                    error_type = error_type or f"interaction_status:{status or 'missing'}"
                failure = classify_failure(response.status_code, status, error_type)
                return ChatResult(
                    success=status_success and failure is None,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    timestamp=timestamp,
                    response_json=interaction,
                    text=extract_gemini_interactions_text(interaction),
                    tool_calls=extract_gemini_interactions_function_calls(interaction),
                    finish_reason=status,
                    usage=usage,
                    ttft_ms=ttft_ms,
                    response_length=len("\n".join(protocol.raw_lines).encode("utf-8")),
                    headers=_headers(response),
                    cache_headers=_cache_headers(response),
                    error_type=error_type if error_type else (None if status_success else "http_error"),
                    failure_classification=failure,
                    raw_text="\n".join(protocol.raw_lines),
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

    def fim_completion(self, body: dict[str, Any]) -> ChatResult:
        """Call an explicitly configured OpenAI-style FIM completions route."""
        if body.get("stream"):
            return self._fim_completion_stream(body)
        started = time.perf_counter()
        timestamp = time.time()
        try:
            url = self._transport_url("fim_completions")
            response = self.session.post(
                url,
                json=body,
                headers=self._auth_headers("fim_completions", url),
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            payload = _safe_json(response)
            choices = payload.get("choices") or []
            choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            finish_reason = choice.get("finish_reason")
            status_success = 200 <= response.status_code <= 299
            failure = classify_failure(
                response.status_code,
                str(finish_reason) if finish_reason else None,
            )
            return ChatResult(
                success=status_success and failure is None,
                status_code=response.status_code,
                latency_ms=latency_ms,
                timestamp=timestamp,
                response_json=payload,
                text=str(choice.get("text") or ""),
                finish_reason=str(finish_reason) if finish_reason else None,
                usage=payload.get("usage")
                if isinstance(payload.get("usage"), dict)
                else {},
                response_length=len(response.content or b""),
                headers=_headers(response),
                cache_headers=_cache_headers(response),
                error_type=None if status_success else "http_error",
                failure_classification=failure,
                raw_text=response.text,
            )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def _fim_completion_stream(self, body: dict[str, Any]) -> ChatResult:
        """Parse the data-only SSE shape used by the FIM completions API."""
        started = time.perf_counter()
        timestamp = time.time()
        content_parts: list[str] = []
        final_usage: dict[str, Any] = {}
        finish_reason: str | None = None
        response_id: str | None = None
        object_name: str | None = None
        model_name: str | None = None
        created: Any = None
        raw_lines: list[str] = []
        pending_json = ""
        ttft_ms: float | None = None
        error_type: str | None = None
        saw_done = False
        provider_error: dict[str, Any] | None = None

        try:
            url = self._transport_url("fim_completions")
            with self.session.post(
                url,
                json=body,
                headers=self._auth_headers("fim_completions", url),
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
                        saw_done = True
                        break
                    if pending_json:
                        pending_json += line
                    elif line.lstrip().startswith("{"):
                        pending_json = line
                    else:
                        error_type = error_type or "stream_protocol_error"
                        continue
                    try:
                        chunk = json.loads(pending_json)
                        pending_json = ""
                    except json.JSONDecodeError:
                        if len(pending_json) > 65536:
                            error_type = "stream_json_parse"
                            pending_json = ""
                        continue

                    if not isinstance(chunk, dict):
                        error_type = error_type or "stream_protocol_error"
                        continue
                    raw_error = chunk.get("error")
                    if raw_error is not None:
                        provider_error = (
                            raw_error
                            if isinstance(raw_error, dict)
                            else {"type": "stream_error"}
                        )
                        error_type = error_type or "fim_stream_error"
                        continue

                    if isinstance(chunk.get("usage"), dict):
                        final_usage = chunk["usage"]
                    if chunk.get("id") is not None:
                        response_id = str(chunk["id"])
                    if chunk.get("object") is not None:
                        object_name = str(chunk["object"])
                    if chunk.get("model") is not None:
                        model_name = str(chunk["model"])
                    if chunk.get("created") is not None:
                        created = chunk["created"]

                    raw_choices = chunk.get("choices")
                    choices = raw_choices if isinstance(raw_choices, list) else []
                    if finish_reason is not None:
                        is_terminal_usage = (
                            isinstance(raw_choices, list)
                            and not raw_choices
                            and isinstance(chunk.get("usage"), dict)
                        )
                        if not is_terminal_usage:
                            error_type = error_type or "stream_data_after_finish"
                        continue
                    if raw_choices is not None and not isinstance(raw_choices, list):
                        error_type = error_type or "stream_protocol_error"
                        continue
                    if not choices:
                        if not isinstance(chunk.get("usage"), dict):
                            error_type = error_type or "stream_protocol_error"
                        continue
                    if not isinstance(choices[0], dict):
                        error_type = error_type or "stream_protocol_error"
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    text_part = choice.get("text")
                    if isinstance(text_part, str) and text_part:
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - started) * 1000
                        content_parts.append(text_part)

                latency_ms = (time.perf_counter() - started) * 1000
                if pending_json:
                    error_type = error_type or "stream_json_parse"
                if not saw_done:
                    error_type = error_type or "stream_missing_done"
                if finish_reason is None:
                    error_type = error_type or "stream_finish_reason_missing"
                status_success = 200 <= response.status_code <= 299
                failure = classify_failure(
                    response.status_code,
                    str(finish_reason) if finish_reason else None,
                    error_type,
                )
                text = "".join(content_parts)
                payload = {
                    "id": response_id,
                    "object": object_name or "text_completion",
                    "created": created,
                    "model": model_name,
                    "choices": [
                        {
                            "text": text,
                            "index": 0,
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": final_usage,
                }
                if provider_error is not None:
                    payload["error"] = provider_error
                return ChatResult(
                    success=status_success and failure is None,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    timestamp=timestamp,
                    response_json=payload,
                    text=text,
                    finish_reason=str(finish_reason) if finish_reason else None,
                    usage=final_usage,
                    ttft_ms=ttft_ms,
                    response_length=len("\n".join(raw_lines).encode("utf-8")),
                    headers=_headers(response),
                    cache_headers=_cache_headers(response),
                    error_type=error_type
                    if error_type
                    else (None if status_success else "http_error"),
                    failure_classification=failure,
                    raw_text="\n".join(raw_lines),
                )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

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
        terminal_seen = False
        terminal_error: Any = None
        incomplete_details: Any = None

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
                    if line == "[DONE]":
                        continue
                    if terminal_seen:
                        error_type = error_type or "stream_data_after_terminal"
                        continue
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
                    if terminal_seen:
                        error_type = error_type or "stream_data_after_terminal"
                        continue
                    if event_type == "error":
                        error_type = error_type or "stream_error_event"
                        continue
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
                    elif event_type in {
                        "response.completed",
                        "response.incomplete",
                        "response.failed",
                    }:
                        terminal_seen = True
                        # Official Responses SSE puts the full Response object
                        # (including usage) on completed, incomplete, and failed
                        # terminal events. Hitting max_output_tokens emits
                        # response.incomplete rather than response.completed.
                        completed = event.get("response") or {}
                        if isinstance(completed, dict):
                            terminal_error = completed.get("error")
                            incomplete_details = completed.get("incomplete_details")
                            response_id = completed.get("id") or response_id
                            model_name = completed.get("model") or model_name
                            status = str(
                                completed.get("status")
                                or status
                                or event_type.rsplit(".", 1)[-1]
                            )
                            if isinstance(completed.get("usage"), dict):
                                usage = completed["usage"]
                            if isinstance(completed.get("output"), list):
                                output_items = [
                                    item for item in completed["output"] if isinstance(item, dict)
                                ]
                            if event_type == "response.failed" or terminal_error:
                                error_type = error_type or "request_failed"

                latency_ms = (time.perf_counter() - started) * 1000
                if pending_json:
                    error_type = error_type or "stream_json_parse"
                if not terminal_seen:
                    error_type = error_type or "stream_terminal_missing"
                payload = {
                    "id": response_id,
                    "object": "response",
                    "model": model_name,
                    "status": status,
                    "output": output_items,
                    "usage": usage,
                    "error": terminal_error,
                    "incomplete_details": incomplete_details,
                }
                streamed_text = "".join(content_parts)
                terminal_text = extract_openai_responses_text(payload)
                # The audit reads terminal output; never expose a conflicting
                # partial delta as a successful response body.
                if content_parts and terminal_seen and streamed_text != terminal_text:
                    error_type = error_type or "stream_output_mismatch"
                text = terminal_text or streamed_text
                status_success = 200 <= response.status_code <= 299
                failure = classify_failure(response.status_code, status, error_type)
                success = status_success and failure is None
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

    def _chat_completion_json(self, body: dict[str, Any], *, cache_affinity_key: str | None = None) -> ChatResult:
        started = time.perf_counter()
        timestamp = time.time()
        try:
            url = self._url("/chat/completions")
            response = self.session.post(
                url,
                json=body,
                headers=self._chat_request_headers(url, cache_affinity_key),
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

    def _chat_completion_stream(self, body: dict[str, Any], *, cache_affinity_key: str | None = None) -> ChatResult:
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
        done_seen = False
        choice_states: dict[int, dict[str, Any]] = {}

        try:
            url = self._url("/chat/completions")
            with self.session.post(
                url,
                json=body,
                headers=self._chat_request_headers(url, cache_affinity_key),
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
                        done_seen = True
                        continue
                    if done_seen:
                        error_type = error_type or "stream_data_after_terminal"
                        continue
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

                    if not isinstance(chunk, dict):
                        error_type = error_type or "stream_chunk_invalid"
                        continue
                    if chunk.get("error"):
                        error_type = error_type or "stream_error_event"
                    if chunk.get("usage"):
                        final_usage = chunk["usage"]
                    if chunk.get("model"):
                        model_name = str(chunk["model"])
                    if chunk.get("system_fingerprint"):
                        system_fingerprint = str(chunk["system_fingerprint"])

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    for choice in choices:
                        if not isinstance(choice, dict):
                            error_type = error_type or "stream_choice_invalid"
                            continue
                        index = choice.get("index", 0)
                        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                            error_type = error_type or "stream_choice_invalid"
                            continue
                        state = choice_states.setdefault(index, {
                            "content": [], "reasoning": [], "tools": [], "finish_reason": None,
                        })
                        if state["finish_reason"] is not None:
                            error_type = error_type or "stream_data_after_finish"
                            continue
                        state["finish_reason"] = choice.get("finish_reason")
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, dict):
                            error_type = error_type or "stream_delta_invalid"
                            continue
                        if any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls")):
                            if ttft_ms is None:
                                ttft_ms = (time.perf_counter() - started) * 1000
                        if delta.get("content"):
                            state["content"].append(delta["content"])
                        if delta.get("reasoning_content"):
                            state["reasoning"].append(delta["reasoning_content"])
                        if delta.get("tool_calls"):
                            if _is_openai_tool_stream_body(body):
                                _merge_openai_stream_tool_calls(state["tools"], delta["tool_calls"])
                            else:
                                state["tools"].extend(delta["tool_calls"])

                latency_ms = (time.perf_counter() - started) * 1000
                if pending_json:
                    error_type = error_type or "stream_json_parse"
                if not done_seen:
                    error_type = error_type or "stream_terminal_missing"
                if not choice_states or any(
                    state["finish_reason"] is None for state in choice_states.values()
                ):
                    error_type = error_type or "stream_finish_reason_missing"
                requested_choices = body.get("n", 1)
                if isinstance(requested_choices, int) and set(choice_states) != set(range(requested_choices)):
                    error_type = error_type or "stream_choice_count_mismatch"
                first_state = choice_states.get(0) or {}
                content_parts = first_state.get("content", [])
                reasoning_parts = first_state.get("reasoning", [])
                tool_calls = first_state.get("tools", [])
                finish_reason = first_state.get("finish_reason")
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
                            "index": index,
                            "message": {
                                "content": "".join(state["content"]),
                                "reasoning_content": "".join(state["reasoning"]),
                                "tool_calls": state["tools"],
                            },
                            "finish_reason": state["finish_reason"],
                        }
                        for index, state in sorted(choice_states.items())
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
        from .anthropic_message_stream import AnthropicMessageStream

        started = time.perf_counter()
        timestamp = time.time()
        accumulator = AnthropicMessageStream()
        raw_lines: list[str] = []
        ttft_ms: float | None = None

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
                for raw_line in response.iter_lines(decode_unicode=False):
                    raw_lines.append(raw_line.decode("utf-8", errors="replace")
                                     if isinstance(raw_line, bytes) else raw_line)
                    accumulator.feed_line(raw_line)
                    if accumulator.output_observed and ttft_ms is None:
                        ttft_ms = (time.perf_counter() - started) * 1000
                    if accumulator.error_type:
                        break

                latency_ms = (time.perf_counter() - started) * 1000
                error_type = accumulator.finish()
                payload = accumulator.message
                stop_reason = payload.get("stop_reason")
                usage = payload.get("usage", {})
                # iter_lines removes delimiters. Restore each consumed line,
                # including an observed blank frame terminator, for replay.
                raw_stream_text = "".join(line + "\n" for line in raw_lines)
                status_success = 200 <= response.status_code <= 299
                failure = classify_failure(response.status_code, stop_reason, error_type)
                success = status_success and failure is None
                return ChatResult(
                    success=success,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    timestamp=timestamp,
                    response_json=payload,
                    text=accumulator.text,
                    reasoning_content=accumulator.reasoning,
                    tool_calls=extract_claude_tool_uses(payload),
                    finish_reason=stop_reason,
                    usage=usage,
                    ttft_ms=ttft_ms,
                    response_length=len(raw_stream_text.encode("utf-8")),
                    headers=_headers(response),
                    cache_headers=_cache_headers(response),
                    error_type=error_type if error_type else (None if status_success else "http_error"),
                    failure_classification=failure,
                    raw_text=raw_stream_text,
                )
        except requests.RequestException as exc:
            return _exception_result(started, timestamp, exc)

    def _url(self, endpoint: str) -> str:
        if endpoint == "/chat/completions":
            return self._transport_url("chat_completions")
        return build_gemini_api_url(self.base_url, endpoint)

    def _models_url(self) -> str:
        interface = self.api_interfaces.get("chat_completions") or {}
        base_url = str(interface.get("base_url") or self.base_url).rstrip("/")
        return build_gemini_api_url(
            base_url, "/models",
            api_version=interface.get("api_version") or interface.get("default_api_version"),
        )

    def _transport_url(self, transport: str, model: str | None = None) -> str:
        interface = self.api_interfaces.get(transport)
        if not isinstance(interface, dict):
            raise ValueError(f"Provider {self.provider!r} has no {transport} interface.")
        base_url = str(interface.get("base_url") or self.base_url).rstrip("/")
        path = str(interface.get("path") or "")
        if model is not None:
            path = path.format(model=quote(model, safe=""))
        return build_gemini_api_url(
            base_url, path,
            api_version=interface.get("api_version") or interface.get("default_api_version"),
        )

    def _gemini_native_url(self, model: str) -> str:
        return self._transport_url("gemini_generate_content", model)

    def _chat_request_headers(self, url: str, cache_affinity_key: str | None) -> dict[str, str]:
        extras = {}
        if cache_affinity_key is not None:
            interface = self.api_interfaces.get("chat_completions") or {}
            if (self.provider != "xai_official" or url != "https://api.x.ai/v1/chat/completions"
                    or str(interface.get("auth") or "bearer") != "bearer"):
                raise ValueError("cache_affinity_key requires the official xAI Chat endpoint and provider.")
            if (type(cache_affinity_key) is not str or not cache_affinity_key
                    or any(ord(char) < 33 or ord(char) > 126 for char in cache_affinity_key)):
                raise ValueError("cache_affinity_key must be a non-empty visible ASCII identifier.")
            extras["x-grok-conv-id"] = cache_affinity_key
        return {**self._auth_headers("chat_completions", url), **extras}

    def _auth_headers(self, transport: str, url: str) -> dict[str, str]:
        interface = self.api_interfaces.get(transport) or {}
        auth = str(interface.get("auth") or "bearer")
        headers = self._credential.auth_headers(url=url, auth_mode=auth)
        version = str(interface.get("anthropic_version") or "").strip()
        if version:
            headers["anthropic-version"] = version
        return headers


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


def _gemini_api_origin(base_url: str) -> str:
    normalized = str(base_url).rstrip("/")
    for suffix in ("/v1beta/openai", "/v1/openai", "/openai"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _interaction_step_index(event: dict[str, Any]) -> int | None:
    value = event.get("index")
    if isinstance(value, bool):
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _append_interaction_text(step: dict[str, Any], text: str) -> None:
    content = step.setdefault("content", [])
    if not isinstance(content, list):
        content = []
        step["content"] = content
    if content and isinstance(content[-1], dict) and content[-1].get("type") == "text":
        content[-1]["text"] = str(content[-1].get("text") or "") + text
    else:
        content.append({"type": "text", "text": text})


def _append_interaction_thought_summary(
    step: dict[str, Any], delta: dict[str, Any]
) -> None:
    step["type"] = "thought"
    content = delta.get("content")
    if not isinstance(content, dict):
        content = {"type": "text", "text": str(delta.get("text") or "")}
    if content.get("type") is None:
        content["type"] = "text"
    summaries = step.setdefault("summary", [])
    if not isinstance(summaries, list):
        summaries = []
        step["summary"] = summaries
    text = content.get("text")
    if (
        isinstance(text, str)
        and summaries
        and isinstance(summaries[-1], dict)
        and summaries[-1].get("type") == "text"
    ):
        summaries[-1]["text"] = str(summaries[-1].get("text") or "") + text
    else:
        summaries.append(dict(content))


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
