from __future__ import annotations

import json

import pytest

from lib.client import ChatResult
from lib.config import (
    get_model_api_form,
    get_model_family,
    get_model_route_profile,
    load_config,
)
from lib.model_profile_catalog import resolve_runtime_parameter_config
from lib.token_audit import TOKEN_AUDIT_SCHEMA_VERSION
from scripts import param_test
from scripts.param_test import _failed_cases, run_identity_probe, run_one_profile


MODEL = "deepseek-v4-flash"
CONTRACT_ID = "deepseek_chat"
PROFILE_ID = "text/deepseek/deepseek/deepseek-v4-flash-0731"

@pytest.mark.parametrize("failure", ["truncated", "injected_input"])
def test_invalid_initial_token_evidence_prevents_tool_continuation(failure):
    class InvalidInitialClient(ToolClient):
        def chat_completion(self, body):
            result = super().chat_completion(body)
            if failure == "truncated":
                result.finish_reason = "length"
                result.response_json["choices"][0]["finish_reason"] = "length"
            else:
                result.usage = {
                    "prompt_tokens": 2048, "completion_tokens": 24, "total_tokens": 2072
                }
            return result

    client = InvalidInitialClient()
    sample = {"id": "weather_shanghai", "prompt": "请查询上海天气。"}
    result = run_one_profile(
        _current_config(), client, "yibu", MODEL, "deepseek", CONTRACT_ID,
        "deepseek", "tool_calls", 1, sample,
    )
    assert sample == {"id": "weather_shanghai", "prompt": "请查询上海天气。"}
    assert client.calls == 1
    assert result["overall_pass"] is False
    assert result["token_validation_pass"] is False
    assert result["followup_request_body"] is None
    exchange = result["token_audit"]["exchanges"][0]
    if failure == "truncated":
        assert exchange["output_completion"]["status"] == "fail"
    else:
        assert exchange["gross_plausibility"]["input"]["status"] == "fail"


def test_successful_identity_report_keeps_exact_request_and_response_evidence():
    client = IdentityProbeClient()
    result = run_identity_probe(
        _current_config(), client, "yibu", MODEL, "deepseek", CONTRACT_ID, "deepseek"
    )
    assert result["request_body"] == client.body
    assert result["response_json"]["choices"][0]["finish_reason"] == "stop"
    assert result["overall_pass"] is True


@pytest.mark.parametrize("evidence", ["usage", "output", "empty_rejection"])
def test_http_error_does_not_exempt_generated_usage_or_output_from_audit(evidence):
    result = IdentityProbeClient().chat_completion({})
    result.status_code = 400
    result.success = False
    if evidence != "output":
        result.response_json = {"error": {"message": "unsupported parameter"}}
    if evidence != "usage":
        result.usage = {}
    audit = param_test._audit_exchange_safely(
        _current_config(), {"messages": [{"role": "user", "content": "Reply OK."}]},
        result, "chat_completions", "initial", provider="yibu", model=MODEL,
    )
    assert audit["usage_required"] is (evidence != "empty_rejection")
    assert audit["validation_pass"] is (evidence == "empty_rejection")


@pytest.mark.parametrize("phase", ["identity", "initial", "followup", "counter"])
def test_sender_and_counter_cannot_redefine_the_declared_parameter_input(phase):
    class MutatingIdentityClient(IdentityProbeClient):
        def chat_completion(self, body):
            if phase == "identity":
                body["messages"].insert(0, {"role": "system", "content": "undeclared input"})
            return super().chat_completion(body)

        def count_tokens(self, transport, model, body):
            if phase == "counter":
                body["messages"].insert(0, {"role": "system", "content": "undeclared count input"})
                return {"tokens": 2000, "evidence_level": "exact"}
            return None

    class MutatingToolClient(ToolClient):
        def chat_completion(self, body):
            should_mutate = (
                phase == "initial" and self.calls == 0
                or phase == "followup" and self.calls == 1
            )
            if should_mutate:
                body["messages"].insert(0, {"role": "system", "content": "undeclared input"})
            return super().chat_completion(body)

    if phase in {"identity", "counter"}:
        client = MutatingIdentityClient()
        result = run_identity_probe(
            _current_config(), client, "yibu", MODEL, "deepseek", CONTRACT_ID, "deepseek"
        )
        assert result["request_body"]["messages"] == [
            {"role": "user", "content": "Reply with OK."}
        ]
    else:
        client = MutatingToolClient()
        result = run_one_profile(
            _current_config(), client, "yibu", MODEL, "deepseek", CONTRACT_ID,
            "deepseek", "tool_calls", 1,
            {"id": "closed", "prompt": "请查询上海天气。"},
        )
        assert client.calls == (1 if phase == "initial" else 2)
        assert result["request_body"]["messages"][0]["role"] == "user"
    assert result["overall_pass"] is False
    audit = result["token_audit"]["exchanges"][-1]
    assert audit["request_integrity"]["status"] == "fail"
    assert audit["request_integrity"]["count_body_changed"] is (phase == "counter")
    assert audit["request_integrity"]["dispatch_body_changed"] is (phase != "counter")


def _current_config() -> dict:
    config = load_config()
    family = get_model_family(config, MODEL, "yibu")
    route = get_model_route_profile(config, MODEL, "yibu")
    api_form = get_model_api_form(
        config,
        MODEL,
        "yibu",
        route_profile=route,
    )
    parameter = resolve_runtime_parameter_config(
        config,
        "yibu",
        MODEL,
        family,
        route,
        api_form,
    )
    assert parameter["contract_id"] == CONTRACT_ID
    assert parameter["profile_id"] == PROFILE_ID
    assert parameter["test_binding"]["parameter_test_enabled"] is True
    assert parameter["test_binding"]["pressure_test_enabled"] is True
    return config


class ToolClient:
    def __init__(self) -> None:
        self.calls = 0
        self.bodies: list[dict] = []

    def chat_completion(self, body: dict) -> ChatResult:
        self.calls += 1
        self.bodies.append(body)
        if self.calls == 1:
            response = {
                "model": MODEL,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_weather",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": json.dumps({"city": "Shanghai"}),
                                    },
                                }
                            ],
                        }
                    }
                ],
            }
        else:
            response = {
                "model": MODEL,
                "choices": [
                    {"message": {"role": "assistant", "content": "上海当前天气晴朗。"}}
                ],
            }
        response["choices"][0]["finish_reason"] = "tool_calls" if self.calls == 1 else "stop"
        return ChatResult(
            success=True,
            status_code=200,
            latency_ms=100.0,
            timestamp=0.0,
            response_json=response,
            usage={
                "prompt_tokens": 100 if self.calls == 1 else 140,
                "completion_tokens": 24 if self.calls == 1 else 10,
                "total_tokens": 124 if self.calls == 1 else 150,
            },
            response_length=100,
            raw_text=json.dumps(response, ensure_ascii=False),
        )


class IdentityProbeClient:
    def __init__(self) -> None:
        self.body: dict | None = None

    def chat_completion(self, body: dict) -> ChatResult:
        self.body = body
        response = {
            "model": MODEL,
            "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        }
        return ChatResult(
            success=True,
            status_code=200,
            latency_ms=10.0,
            timestamp=0.0,
            response_json=response,
            usage={"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
            response_length=2,
            raw_text=json.dumps(response),
        )


class MissingUsageClient:
    def chat_completion(self, body: dict) -> ChatResult:
        response = {
            "model": MODEL,
            "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        }
        return ChatResult(
            success=True,
            status_code=200,
            latency_ms=10.0,
            timestamp=0.0,
            response_json=response,
            usage={},
            response_length=2,
            raw_text=json.dumps(response),
        )


def test_identity_probe_is_non_streaming_tool_free_and_audited() -> None:
    client = IdentityProbeClient()

    result = run_identity_probe(
        _current_config(),
        client,
        "yibu",
        MODEL,
        "deepseek",
        CONTRACT_ID,
        "deepseek",
    )

    assert client.body is not None
    assert client.body["stream"] is False
    assert client.body["max_tokens"] >= 256
    assert "tools" not in client.body
    assert result["identity_probe"] is True
    assert result["minimum_output_tokens"] == 256
    assert result["output_token_limits"] == {"max_tokens": 256}
    assert result["model_identity_audit"]["status"] == "match"
    assert result["token_audit"]["exchanges"][0]["exchange"] == "identity_probe"


def test_identity_probe_rejects_a_configured_floor_below_256_before_send() -> None:
    config = _current_config()
    config["test_cases"]["minimum_output_tokens"] = 255
    client = IdentityProbeClient()

    with pytest.raises(ValueError, match="must be at least 256"):
        run_identity_probe(
            config,
            client,
            "yibu",
            MODEL,
            "deepseek",
            CONTRACT_ID,
            "deepseek",
        )

    assert client.body is None


def test_multi_turn_audit_validates_each_successful_exchange() -> None:
    client = ToolClient()
    result = run_one_profile(
        _current_config(),
        client,
        "yibu",
        MODEL,
        "deepseek",
        CONTRACT_ID,
        "deepseek",
        "tool_calls",
        1,
        {"id": "weather_shanghai", "prompt": "请查询上海天气。"},
    )

    assert result["pass"] is True
    assert result["status"] == "pass"
    assert len(client.bodies) == 2
    assert all(body["model"] == MODEL for body in client.bodies)
    assert all(body["max_tokens"] >= 1024 for body in client.bodies)
    assert client.bodies[0]["messages"] == [
        {"role": "user", "content": "请查询上海天气。"}
    ]
    assert client.bodies[1]["messages"][0] == client.bodies[0]["messages"][0]
    assert result["minimum_output_tokens"] == 256
    assert result["output_token_limits"] == {"max_tokens": 1024}
    assert "tools" in client.bodies[0]
    # The legacy character heuristic is display-only. Without an exact model
    # tokenizer these exchanges stay partial and do not fail compatibility.
    assert result["token_audit"]["status"] == "partial"
    assert result["token_validation_status"] == "pass"
    assert result["token_validation_pass"] is True
    assert result["overall_pass"] is True
    assert all(
        exchange["usage_presence"]["status"] == "pass"
        and exchange["gross_plausibility"]["status"] == "pass"
        for exchange in result["token_audit"]["exchanges"]
    )
    assert [item["exchange"] for item in result["token_audit"]["exchanges"]] == [
        "initial",
        "followup",
    ]


def test_successful_parameter_case_with_missing_usage_is_an_overall_failure() -> None:
    result = run_one_profile(
        _current_config(),
        MissingUsageClient(),
        "yibu",
        MODEL,
        "deepseek",
        CONTRACT_ID,
        "deepseek",
        "deepseek_max_tokens",
        1,
        {"id": "general", "prompt": "Reply with OK."},
    )

    assert result["compatibility_pass"] is True
    assert result["token_validation_pass"] is False
    assert result["overall_status"] == "token_validation_failed"
    assert result["overall_pass"] is False
    failed = _failed_cases([result])
    assert failed[0]["status"] == "token_validation_failed"
    assert failed[0]["token_audit"]["validation_pass"] is False


def test_token_audit_exception_fails_closed_for_successful_exchange(monkeypatch) -> None:
    result = IdentityProbeClient().chat_completion({})

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("counter exploded")

    monkeypatch.setattr(param_test, "audit_exchange", fail_audit)
    audit = param_test._audit_exchange_safely(
        _current_config(),
        {"messages": [{"role": "user", "content": "hello"}]},
        result,
        "chat_completions",
        "initial",
        provider="yibu",
        model=MODEL,
    )

    assert audit["schema_version"] == TOKEN_AUDIT_SCHEMA_VERSION
    assert audit["usage_required"] is True
    assert audit["validation_status"] == "fail"
    assert audit["validation_pass"] is False
    assert audit["validation_failures"][0] == "token audit error: RuntimeError"
