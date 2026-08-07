from __future__ import annotations

import json

from lib.client import ChatResult
from lib.config import load_config
from scripts.param_test import run_identity_probe, run_one_profile


class ToolClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat_completion(self, _body: dict) -> ChatResult:
        self.calls += 1
        if self.calls == 1:
            response = {
                "model": "deepseek-v4-pro",
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
                "model": "deepseek-v4-pro",
                "choices": [
                    {"message": {"role": "assistant", "content": "上海当前天气晴朗。"}}
                ],
            }
        return ChatResult(
            success=True,
            status_code=200,
            latency_ms=100.0,
            timestamp=0.0,
            response_json=response,
            usage={"prompt_tokens": 100, "completion_tokens": 500},
            response_length=100,
            raw_text=json.dumps(response, ensure_ascii=False),
        )


class IdentityProbeClient:
    def __init__(self) -> None:
        self.body: dict | None = None

    def chat_completion(self, body: dict) -> ChatResult:
        self.body = body
        response = {
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
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


def test_identity_probe_is_non_streaming_tool_free_and_audited() -> None:
    client = IdentityProbeClient()

    result = run_identity_probe(
        load_config(),
        client,
        "yibu",
        "deepseek-v4-pro",
        "deepseek",
        "deepseek_chat",
        "deepseek",
    )

    assert client.body is not None
    assert client.body["stream"] is False
    assert "tools" not in client.body
    assert result["identity_probe"] is True
    assert result["model_identity_audit"]["status"] == "match"
    assert result["token_audit"]["exchanges"][0]["exchange"] == "identity_probe"


def test_multi_turn_audit_is_per_exchange_and_does_not_change_param_verdict() -> None:
    result = run_one_profile(
        load_config(),
        ToolClient(),
        "yibu",
        "deepseek-v4-pro",
        "deepseek",
        "deepseek_chat",
        "deepseek",
        "tool_calls",
        1,
        {"id": "weather_shanghai", "prompt": "请查询上海天气。"},
    )

    assert result["pass"] is True
    assert result["status"] == "pass"
    # The legacy character heuristic is display-only. Without an exact model
    # tokenizer these exchanges stay partial and do not fail compatibility.
    assert result["token_audit"]["status"] == "partial"
    assert [item["exchange"] for item in result["token_audit"]["exchanges"]] == [
        "initial",
        "followup",
    ]
