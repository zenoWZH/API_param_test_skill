from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from lib.client import ChatResult, OpenAICompatibleClient
from lib.config import (
    get_model_api_form,
    get_model_transport,
    get_provider_interface,
    load_config,
)
from lib.deepseek_params import apply_request_mode, build_request
from lib.model_profile_catalog import resolve_runtime_parameter_config
from lib.reference_specs import (
    test_profiles_for_reference as reference_test_profiles,
)
from lib.token_audit import TOKEN_AUDIT_SCHEMA_VERSION
from scripts.param_test import (
    _input_group_for_profile,
    _validate_deepseek0813_fim_response,
    run_identity_probe,
    run_one_profile,
)
from scripts.web_console import _preflight_job


CONTRACT_ID = "deepseek_v4_pro_0813_fim_beta"
API_FORM = "openai_fim_completions_beta"
TRANSPORT = "fim_completions"
PROVIDER = "deepseek_official"
MODEL = "deepseek-v4-pro"
ROUTE = "vendor_direct"


class _JsonResponse:
    status_code = 200
    headers: dict[str, str] = {"content-type": "application/json"}

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode("utf-8")

    def json(self) -> dict[str, Any]:
        return self._payload


class _StreamResponse:
    status_code = 200
    encoding = "utf-8"
    headers: dict[str, str] = {"content-type": "text/event-stream"}
    content = b""
    text = ""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def iter_lines(self, decode_unicode: bool = True):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _result(*, usage: bool = True) -> ChatResult:
    token_usage = (
        {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}
        if usage
        else {}
    )
    payload = {
        "model": MODEL,
        "choices": [{"text": " a + b", "finish_reason": "stop"}],
        "usage": token_usage,
    }
    return ChatResult(
        success=True,
        status_code=200,
        latency_ms=5.0,
        timestamp=0.0,
        response_json=payload,
        text=" a + b",
        usage=token_usage,
        finish_reason="stop",
        raw_text=json.dumps(payload),
    )


class _FimOnlyClient:
    def __init__(self, *, usage: bool = True) -> None:
        self.usage = usage
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def fim_completion(self, body: dict[str, Any]) -> ChatResult:
        self.calls.append((TRANSPORT, body))
        return _result(usage=self.usage)

    def chat_completion(self, body: dict[str, Any]) -> ChatResult:
        raise AssertionError("FIM must never fall back to Chat Completions")

    def count_tokens(
        self, transport: str, model: str, body: dict[str, Any]
    ) -> None:
        return None


def test_app_preflight_resolves_active_mpdb_fim_binding() -> None:
    config = load_config()
    api_form = get_model_api_form(
        config, MODEL, PROVIDER, route_profile=ROUTE
    )

    assert api_form == API_FORM
    assert (
        get_model_transport(
            config,
            MODEL,
            PROVIDER,
            route_profile=ROUTE,
            api_form=api_form,
        )
        == TRANSPORT
    )
    interface = get_provider_interface(config, TRANSPORT, PROVIDER)
    assert interface["base_url"] == "https://api.deepseek.com/beta"
    assert interface["path"] == "/completions"

    parameter = resolve_runtime_parameter_config(
        config,
        PROVIDER,
        MODEL,
        "deepseek",
        ROUTE,
        API_FORM,
        modality="text",
    )
    assert parameter["contract_id"] == CONTRACT_ID
    assert len(parameter["test_cases"]) == 15
    snapshot = parameter["model_profile_database"]
    assert snapshot["interface"]["enabled"] is True
    assert snapshot["interface"]["executable"] is True

    _preflight_job(
        config,
        PROVIDER,
        MODEL,
        "param_test",
        "throughput_rpm",
        CONTRACT_ID,
        API_FORM,
        ROUTE,
    )


def test_every_mpdb_fim_profile_builds_only_the_fim_transport() -> None:
    config = load_config()
    profiles = reference_test_profiles(CONTRACT_ID)
    assert len(profiles) == 15

    for profile in profiles:
        built = build_request(
            config,
            "compatibility_profiles",
            profile,
            overrides={"model": MODEL, "prompt": "def add(a, b):\n    return"},
            model_family_override="deepseek",
            api_form_override=API_FORM,
            route_profile_override=ROUTE,
            reference_source=CONTRACT_ID,
            enforce_model_capabilities=False,
        )
        assert built.metadata["transport"] == TRANSPORT
        assert built.metadata["request_endpoint"] == "/beta/completions"
        assert built.body["model"] == MODEL
        assert "messages" not in built.body
        assert "preserve_rejected_params" not in built.body
        assert _input_group_for_profile(config, profile) == "fim"


def test_fim_unique_mode_changes_prefix_without_touching_suffix() -> None:
    body = {"prompt": "left", "suffix": "right"}

    assert apply_request_mode(body, TRANSPORT, "unique", nonce="n1") is True
    assert body == {"prompt": "load-request-n1|left", "suffix": "right"}


def test_fim_json_client_uses_beta_endpoint_and_extracts_usage() -> None:
    payload = {
        "model": MODEL,
        "choices": [{"text": "middle", "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    client = OpenAICompatibleClient("https://api.deepseek.com", "secret")
    client.session.post = Mock(return_value=_JsonResponse(payload))

    result = client.fim_completion(
        {"model": MODEL, "prompt": "left", "suffix": "right", "stream": False}
    )

    assert result.success is True
    assert result.text == "middle"
    assert result.usage["total_tokens"] == 3
    call = client.session.post.call_args
    assert call.args[0] == "https://api.deepseek.com/beta/completions"
    assert call.kwargs["allow_redirects"] is False


def test_fim_stream_client_keeps_terminal_usage_and_done_evidence() -> None:
    chunks = [
        {
            "id": "cmpl_fim",
            "object": "text_completion",
            "created": 1,
            "model": MODEL,
            "choices": [{"text": " a", "finish_reason": None}],
        },
        {
            "id": "cmpl_fim",
            "object": "text_completion",
            "created": 1,
            "model": MODEL,
            "choices": [{"text": " + b", "finish_reason": "stop"}],
        },
        {
            "id": "cmpl_fim",
            "object": "text_completion",
            "created": 1,
            "model": MODEL,
            "choices": [],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            },
        },
    ]
    lines = ["data: " + json.dumps(chunk) for chunk in chunks]
    lines.append("data: [DONE]")
    client = OpenAICompatibleClient("https://api.deepseek.com", "secret")
    client.session.post = Mock(return_value=_StreamResponse(lines))

    result = client.fim_completion(
        {
            "model": MODEL,
            "prompt": "def add(a, b):\n    return",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    )

    assert result.success is True
    assert result.text == " a + b"
    assert result.finish_reason == "stop"
    assert result.usage == {
        "prompt_tokens": 8,
        "completion_tokens": 2,
        "total_tokens": 10,
    }
    assert result.response_json["model"] == MODEL
    assert result.raw_text.endswith("[DONE]")


def test_fim_identity_and_profile_runner_never_dispatch_chat() -> None:
    config = load_config()
    client = _FimOnlyClient()

    identity = run_identity_probe(
        config,
        client,
        PROVIDER,
        MODEL,
        "deepseek",
        CONTRACT_ID,
        "deepseek",
    )
    case = run_one_profile(
        config,
        client,
        PROVIDER,
        MODEL,
        "deepseek",
        CONTRACT_ID,
        "deepseek",
        "deepseek0813_fim_stream_usage",
        1,
        {"id": "offline", "prompt": "def add(a, b):\n    return"},
        expectation="supported",
    )

    assert [transport for transport, _body in client.calls] == [
        TRANSPORT,
        TRANSPORT,
    ]
    assert identity["transport"] == TRANSPORT
    assert identity["overall_pass"] is True
    assert case["transport"] == TRANSPORT
    assert case["pass"] is True
    assert case["overall_pass"] is True
    assert case["token_validation_pass"] is True
    exchange = case["token_audit"]["exchanges"][0]
    assert exchange["schema_version"] == TOKEN_AUDIT_SCHEMA_VERSION == 4
    assert exchange["usage_accounting"]["input_tokens"] == 8
    assert exchange["usage_accounting"]["output_tokens"] == 2
    assert exchange["usage_accounting"]["total_tokens"] == 10
    assert client.calls[1][1]["stream_options"] == {"include_usage": True}


def test_fim_success_without_usage_fails_token_schema3_gate() -> None:
    case = run_one_profile(
        load_config(),
        _FimOnlyClient(usage=False),
        PROVIDER,
        MODEL,
        "deepseek",
        CONTRACT_ID,
        "deepseek",
        "deepseek0813_fim_basic",
        1,
        {"id": "offline", "prompt": "def add(a, b):\n    return"},
        expectation="supported",
    )

    assert case["compatibility_pass"] is True
    assert case["token_validation_pass"] is False
    assert case["overall_pass"] is False
    assert case["overall_status"] == "token_validation_failed"
    assert case["token_audit"]["exchanges"][0]["schema_version"] == TOKEN_AUDIT_SCHEMA_VERSION
    assert any(
        "missing authoritative input_tokens, output_tokens" in failure
        for failure in case["token_audit"]["validation_failures"]
    )


def test_fim_response_validation_rejects_chat_shaped_payload() -> None:
    malformed = _result()
    malformed.response_json = {
        "model": MODEL,
        "choices": [{"message": {"content": "chat fallback"}}],
    }

    assert (
        _validate_deepseek0813_fim_response(
            "deepseek0813_fim_basic", malformed.response_json, malformed
        )
        == "fim_completion_missing"
    )


def test_identity_probe_rejects_unknown_api_form_without_chat_fallback() -> None:
    client = _FimOnlyClient()
    with patch(
        "scripts.param_test.get_model_api_form",
        return_value="unknown_parameter_api",
    ):
        with pytest.raises(ValueError, match="Unsupported identity-probe API form"):
            run_identity_probe(
                load_config(),
                client,
                PROVIDER,
                MODEL,
                "deepseek",
                CONTRACT_ID,
                "deepseek",
            )
    assert client.calls == []


def test_profile_runner_rejects_unknown_transport_without_chat_fallback() -> None:
    client = _FimOnlyClient()
    invalid_built = SimpleNamespace(
        body={"model": MODEL, "prompt": "prefix"},
        metadata={
            "transport": "unknown_parameter_transport",
            "request_endpoint": "/unknown",
        },
        warnings=[],
    )
    with patch("scripts.param_test.build_request", return_value=invalid_built):
        case = run_one_profile(
            load_config(),
            client,
            PROVIDER,
            MODEL,
            "deepseek",
            CONTRACT_ID,
            "deepseek",
            "deepseek0813_fim_basic",
            1,
            {"id": "offline", "prompt": "prefix"},
            expectation="supported",
        )

    assert client.calls == []
    assert case["overall_pass"] is False
    assert "Unsupported parameter-test transport" in case["message"]
